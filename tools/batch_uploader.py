#!/usr/bin/env python3
"""
batch_uploader.py - RK3588 定量/定时批量上传守护进程 (无第三方依赖)

功能：
  1. 持续轮询本地暂存池 (spool/ready)。
  2. 多维度阈值触发批量上传：
     - 数量阈值：积攒满 N 个切片（默认 5 段，约 15 分钟视频）。
     - 容量阈值：积攒满 X MB（默认 500 MB）。
     - 超时保底：首个就绪切片等待时间 >= Y 秒（默认 1800 秒 / 30 分钟）。
  3. 原子移入 spool/uploading 进行批量上传，保障事务独立性。
  4. 支持断网自动重试与退避机制，网络故障绝不丢失本地数据。
  5. 自动维护磁盘安全：定期释放 spool/completed 历史文件，空间不足自动紧急淘汰。
"""
import argparse
import datetime as dt
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

RUNNING = True
PART_SIZE = 8 * 1024 * 1024  # 8MB


def signal_handler(signum, frame):
    global RUNNING
    sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
    print(f"\n[UPLOADER] 接收到停机信号 ({sig_name})，准备安全退出...")
    RUNNING = False


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso_time(s):
    try:
        # e.g. 2026-09-09T03:00:00.000Z
        clean = s.replace("Z", "+00:00")
        return dt.datetime.fromisoformat(clean)
    except Exception:
        return dt.datetime.now(dt.timezone.utc)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(PART_SIZE):
            h.update(chunk)
    return h.hexdigest()


class PlatformClient:
    def __init__(self, base_url, username="admin", password="admin12345678", token=None):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.token = token
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self.csrf_token = None

    def login(self):
        if self.token:
            return
        url = f"{self.base_url}/api/v1/auth/login"
        data = json.dumps({"username": self.username, "password": self.password}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST"
        )
        try:
            with self.opener.open(req, timeout=15) as resp:
                for cookie in self.cookie_jar:
                    if cookie.name == "XSRF-TOKEN":
                        self.csrf_token = cookie.value
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"登录平台失败 (HTTP {e.code}): {raw}") from None

    def request(self, method, path, body=None, idempotency_key=None):
        url = f"{self.base_url}{path}" if path.startswith("/api") else f"{self.base_url}/api/v1{path}"
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.csrf_token:
            headers["X-XSRF-TOKEN"] = self.csrf_token

        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=40) as resp:
                content = resp.read()
                return json.loads(content.decode("utf-8")) if content else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"平台接口调用失败 ({method} {path} HTTP {e.code}): {raw}") from None

    def put_part_to_s3(self, upload_url, data):
        parsed_s3 = urllib.parse.urlsplit(upload_url)
        parsed_base = urllib.parse.urlsplit(self.base_url)
        headers = {"Content-Length": str(len(data))}
        target_url = upload_url

        if parsed_s3.hostname in ("localhost", "127.0.0.1"):
            headers["Host"] = parsed_s3.netloc
            if parsed_base.hostname not in ("localhost", "127.0.0.1"):
                # 预签名 URL 的主机为 localhost，但客户端从远端 IP 访问，需将目标重写为服务器 IP 与局域网代理端口 19001
                s3_port = 19001 if (parsed_s3.port in (19000, 9000) or not parsed_s3.port) else parsed_s3.port
                target_netloc = f"{parsed_base.hostname}:{s3_port}"
                target_url = urllib.parse.urlunsplit(
                    (parsed_s3.scheme, target_netloc, parsed_s3.path, parsed_s3.query, parsed_s3.fragment)
                )
            else:
                # 本地访问直接走 127.0.0.1 避免 Windows 上 localhost 尝试 IPv6 产生 2~5 秒连接延迟
                port_str = f":{parsed_s3.port}" if parsed_s3.port else ""
                target_url = urllib.parse.urlunsplit(
                    (parsed_s3.scheme, f"127.0.0.1{port_str}", parsed_s3.path, parsed_s3.query, parsed_s3.fragment)
                )

        req = urllib.request.Request(target_url, data=data, headers=headers, method="PUT")
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"直传 S3 失败 (HTTP {resp.status})")


def ensure_uuid(val):
    if val:
        try:
            return str(uuid.UUID(str(val)))
        except Exception:
            pass
    return str(uuid.uuid4())


def ensure_iso_utc(ts_str):
    if not ts_str:
        return utc_now()
    try:
        clean = ts_str.replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(clean)
        return d.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except Exception:
        return utc_now()


CAMERA_SET_CACHE = {}


def ensure_camera_set(client, project_id, device_id, camera_count=1, layout_version="default-layout-v1"):
    """确保项目内存在对应设备的 CameraSet 配置并返回 (camera_set_id, channel_id_map)"""
    key = f"{device_id}-set"
    version = 1
    channel_map = {}
    members = []
    for cam_num in range(1, camera_count + 1):
        prov_key = f"{device_id}.camera-{cam_num}"
        ch_resp = client.request("PUT", f"/projects/{project_id}/camera-channels/{prov_key}")
        ch_id = ch_resp["camera_channel_id"]
        channel_map[cam_num] = ch_id
        members.append({
            "camera_channel_id": ch_id,
            "camera_index": cam_num - 1,
            "camera_role": "OVERVIEW" if cam_num == 1 else "HANDS",
            "mount_position": f"camera-{cam_num}",
            "enabled": True,
        })
    set_resp = client.request(
        "PUT",
        f"/projects/{project_id}/camera-sets/{key}/versions/{version}",
        {"layout_version": layout_version, "members": members}
    )
    return set_resp["camera_set_id"], channel_map


def is_valid_uuid(val):
    if not val:
        return False
    try:
        return str(uuid.UUID(str(val))).lower() == str(val).lower()
    except Exception:
        return False


def get_or_create_camera_set(client, project_id, device_id, camera_count, layout_version):
    cache_key = (project_id, device_id, camera_count, layout_version)
    if cache_key in CAMERA_SET_CACHE:
        return CAMERA_SET_CACHE[cache_key]
    set_id, channel_map = ensure_camera_set(client, project_id, device_id, camera_count, layout_version)
    CAMERA_SET_CACHE[cache_key] = (set_id, channel_map)
    return set_id, channel_map


def upload_segment(client, segment_dir, project_id, device_id, camera_set_id=None, layout_version=None):
    """将切片目录（含多机位视频）组装为单一 V2 Manifest 批次并完整入库 CaptureGroup"""
    segment_dir = Path(segment_dir)
    meta_path = segment_dir / "segment_meta.json"
    if not meta_path.exists():
        raise ValueError(f"切片目录缺少元数据文件: {meta_path}")

    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    videos_meta = meta.get("videos", [])
    if not videos_meta:
        raise ValueError(f"切片元数据中无有效视频项: {meta_path}")

    captured_at = ensure_iso_utc(meta.get("captured_at"))
    group_key = meta.get("segment_id") or f"seg_{uuid.uuid4().hex[:12]}"
    session_id = str(uuid.uuid4())
    layout = layout_version or "default-layout-v1"

    if camera_set_id and is_valid_uuid(camera_set_id):
        cam_set_id = str(uuid.UUID(str(camera_set_id)))
        channel_map = {}
    else:
        cam_set_id, channel_map = get_or_create_camera_set(client, project_id, device_id, len(videos_meta), layout)

    # 1. 整理视频文件及机位流信息
    video_files = []
    streams = []
    declarations = []

    for v in videos_meta:
        cam_num = int(v.get("camera_number", 1))
        file_name = v.get("file_name", f"camera_{cam_num}.mp4")
        video_path = segment_dir / file_name
        if not video_path.exists():
            raise FileNotFoundError(f"切片视频文件丢失: {video_path}")

        size_bytes = video_path.stat().st_size
        file_hash = v.get("sha256") or file_sha256(video_path)
        client_file_id = str(uuid.uuid4())
        channel_id = channel_map.get(cam_num) or str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{device_id}.channel.{cam_num}"))

        video_files.append({
            "client_file_id": client_file_id,
            "path": video_path,
            "file_name": file_name,
            "file_kind": "VIDEO",
            "content_type": "video/mp4",
            "size_bytes": size_bytes,
            "sha256": file_hash,
            "part_size_bytes": PART_SIZE,
        })
        streams.append({
            "client_file_id": client_file_id,
            "camera_channel_id": channel_id,
            "camera_index": cam_num - 1,
            "enabled": True,
            "record_start_utc": captured_at,
            "time_offset_ms": 0,
            "sync_status": "UNKNOWN",
        })
        declarations.append({
            "client_file_id": client_file_id,
            "camera_number": cam_num,
            "file_name": file_name,
            "declared_size_bytes": size_bytes,
            "sha256": file_hash,
            "record_start_at": captured_at,
        })

    # 2. 生成 metadata.json (保留机位布局与流绑定，绝不生成已废弃的 event-log.ndjson)
    metadata_obj = {
        "schema_version": "capture-session-metadata-v1",
        "capture_session_id": session_id,
        "device_id": device_id,
        "camera_set_id": cam_set_id,
        "expected_camera_count": len(videos_meta),
        "camera_layout_version": layout_version or "default-layout-v1",
        "sync_group_id": group_key,
        "device_time_utc": captured_at,
        "device_timezone": "Asia/Shanghai",
        "streams": streams,
    }
    meta_path_json = segment_dir / "metadata.json"
    with open(meta_path_json, "w", encoding="utf-8") as f:
        json.dump(metadata_obj, f, indent=2, ensure_ascii=False)

    meta_size = meta_path_json.stat().st_size
    meta_sha = file_sha256(meta_path_json)
    meta_client_file_id = str(uuid.uuid4())
    metadata_file = {
        "client_file_id": meta_client_file_id,
        "path": meta_path_json,
        "file_name": "metadata.json",
        "file_kind": "METADATA",
        "content_type": "application/json",
        "size_bytes": meta_size,
        "sha256": meta_sha,
        "part_size_bytes": PART_SIZE,
    }

    all_upload_files = video_files + [metadata_file]

    # 3. 计算清单 SHA-256 (排序稳定)
    manifest_lines = ["upload-file-manifest-v1\n"]
    for f_info in sorted(all_upload_files, key=lambda item: item["client_file_id"]):
        manifest_lines.append("\t".join(str(f_info[key]) for key in (
            "client_file_id", "file_kind", "content_type", "size_bytes", "sha256", "part_size_bytes"
        )) + "\n")
    manifest_sha256 = hashlib.sha256("".join(manifest_lines).encode("utf-8")).hexdigest()

    # 4. 提交 V2 External Ingest Manifest
    external_manifest = {
        "schema_version": "external-video-ingest-v2",
        "source_system": "medication-camera",
        "source_device_id": device_id,
        "source_batch_id": group_key,
        "capture_group": {
            "capture_group_key": group_key,
            "key_source": "UPSTREAM_MANIFEST_KEY",
            "captured_at": captured_at,
            "timezone": "Asia/Shanghai",
            "expected_camera_numbers": sorted([v["camera_number"] for v in declarations]),
        },
        "videos": declarations,
        "upload_set_finalized": False,
    }
    manifest_resp = client.request("POST", f"/projects/{project_id}/external-video-ingest-manifests/v2", external_manifest)
    manifest_id = manifest_resp.get("manifest_id")

    # 5. 初始化上传批次 (HOSPITAL_REMOTE 真实多机位来源)
    total_bytes = sum(f["size_bytes"] for f in all_upload_files)
    init_body = {
        "project_id": project_id,
        "device_id": device_id,
        "upload_source_type": "HOSPITAL_REMOTE",
        "file_count": len(all_upload_files),
        "total_size_bytes": total_bytes,
        "manifest_sha256": manifest_sha256,
        "external_manifest_id": manifest_id,
    }
    batch = client.request("POST", "/upload/init", init_body, idempotency_key=f"batch-init-{group_key}")
    batch_id = batch["batch_id"]

    # 6a. 注册所有声明的文件 (平台约束：所有文件必须在分片开始前完成注册)
    for f_info in all_upload_files:
        fid = f_info["client_file_id"]
        client.request(
            "PUT",
            f"/upload/{batch_id}/files/{fid}",
            {
                "file_kind": f_info["file_kind"],
                "content_type": f_info["content_type"],
                "size_bytes": f_info["size_bytes"],
                "sha256": f_info["sha256"],
                "part_size_bytes": f_info["part_size_bytes"],
                "file_name": f_info["file_name"],
            },
            idempotency_key=f"file-{fid}"
        )

    # 6b. 逐个文件执行分片直传并完成
    for f_info in all_upload_files:
        fid = f_info["client_file_id"]
        client.request("PUT", f"/upload/{batch_id}/files/{fid}/multipart")
        f_size = f_info["size_bytes"]
        num_parts = (f_size + PART_SIZE - 1) // PART_SIZE
        with open(f_info["path"], "rb") as fp:
            for part_num in range(1, num_parts + 1):
                part_data = fp.read(PART_SIZE)
                part_hash = hashlib.sha256(part_data).hexdigest()
                auth_resp = client.request(
                    "POST",
                    f"/upload/{batch_id}/files/{fid}/parts/{part_num}/authorization",
                    {"sha256": part_hash}
                )
                client.put_part_to_s3(auth_resp["upload_url"], part_data)
                client.request("PUT", f"/upload/{batch_id}/files/{fid}/parts/{part_num}/confirmation")
        client.request("PUT", f"/upload/{batch_id}/files/{fid}/completion")

    # 7. 完成批次
    completion_resp = client.request("PUT", f"/upload/{batch_id}/completion")
    clip_assets = completion_resp.get("clip_assets", [])

    # 8. 组装 CaptureSession 并获取 CaptureGroup
    session_resp = client.request("PUT", f"/upload/{batch_id}/capture-session")
    capture_session_id = session_resp.get("capture_session_id", session_id)
    group_id = session_resp.get("capture_group_id", capture_session_id)

    # 9. 终结收件回执
    try:
        client.request("POST", f"/projects/{project_id}/capture-groups/{group_id}/receipt/finalize")
    except Exception as e:
        print(f"[UPLOADER] [WARN] 收件回执终结提示: {e}")

    record = {
        "batch_id": batch_id,
        "capture_group_id": group_id,
        "capture_session_id": capture_session_id,
        "clip_asset_count": len(clip_assets),
        "total_size_bytes": total_bytes,
    }

    meta["upload_status"] = "COMPLETED"
    meta["uploaded_at"] = utc_now()
    meta["upload_record"] = record
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    return [record]


def clean_disk_if_needed(spool_dir, min_disk_free_gb, retention_hours):
    """磁盘空间与过期清理策略"""
    completed_dir = Path(spool_dir) / "completed"
    if not completed_dir.exists():
        return

    # 1. 过期清理 (retention_hours)
    now_utc = dt.datetime.now(dt.timezone.utc)
    for batch_path in list(completed_dir.iterdir()):
        if not batch_path.is_dir():
            continue
        mtime = dt.datetime.fromtimestamp(batch_path.stat().st_mtime, tz=dt.timezone.utc)
        age_hours = (now_utc - mtime).total_seconds() / 3600.0
        if age_hours > retention_hours:
            print(f"[UPLOADER-CLEAN] 清理超过保留期的历史批次: {batch_path.name} (已存放 {age_hours:.1f} 小时)")
            shutil.rmtree(batch_path, ignore_errors=True)

    # 2. 空间不足紧急清理 (FIFO)
    try:
        usage = shutil.disk_usage(spool_dir)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < min_disk_free_gb:
            print(f"[UPLOADER-CLEAN] ⚠️ 磁盘剩余空间仅 {free_gb:.2f} GB (安全阈值: {min_disk_free_gb} GB)，执行 FIFO 紧急清理...")
            batches = sorted(
                [p for p in completed_dir.iterdir() if p.is_dir()],
                key=lambda p: p.stat().st_mtime
            )
            for b in batches:
                print(f"  -> 紧急删除历史已上传批次: {b.name}")
                shutil.rmtree(b, ignore_errors=True)
                usage = shutil.disk_usage(spool_dir)
                if usage.free / (1024 ** 3) >= min_disk_free_gb:
                    break
    except Exception as e:
        print(f"[UPLOADER-CLEAN] 检查磁盘空间出错: {e}", file=sys.stderr)


def process_ready_batches(args, client, project_id):
    """扫描就绪切片，满足定量/超时条件则打包上传"""
    spool_dir = Path(args.spool).resolve()
    ready_dir = spool_dir / "ready"
    uploading_dir = spool_dir / "uploading"
    completed_dir = spool_dir / "completed"

    ready_dir.mkdir(parents=True, exist_ok=True)
    uploading_dir.mkdir(parents=True, exist_ok=True)
    completed_dir.mkdir(parents=True, exist_ok=True)

    # 获取所有包含 valid segment_meta.json 的就绪切片
    candidates = []
    for item in ready_dir.iterdir():
        if item.is_dir() and (item / "segment_meta.json").exists():
            try:
                with open(item / "segment_meta.json", "r", encoding="utf-8") as f:
                    meta = json.load(f)
                captured_at = parse_iso_time(meta.get("captured_at", ""))
                size_bytes = sum(v.get("size_bytes", 0) for v in meta.get("videos", []))
                candidates.append({
                    "path": item,
                    "captured_at": captured_at,
                    "size_bytes": size_bytes,
                    "meta": meta
                })
            except Exception:
                continue

    if not candidates:
        return False

    # 按录制起始时间正序排序 (先录的先上传)
    candidates.sort(key=lambda x: x["captured_at"])

    total_count = len(candidates)
    total_size_mb = sum(c["size_bytes"] for c in candidates) / (1024 * 1024)
    now_utc = dt.datetime.now(dt.timezone.utc)
    oldest_age_sec = (now_utc - candidates[0]["captured_at"]).total_seconds()

    # 评估触发策略
    reach_count = total_count >= args.batch_count
    reach_size = total_size_mb >= args.batch_size_mb
    reach_timeout = oldest_age_sec >= args.max_delay_seconds

    if not (reach_count or reach_size or reach_timeout):
        return False

    reason = []
    if reach_count:
        reason.append(f"数量达到阈值 ({total_count}/{args.batch_count} 段)")
    if reach_size:
        reason.append(f"容量达到阈值 ({total_size_mb:.1f}/{args.batch_size_mb} MB)")
    if reach_timeout:
        reason.append(f"最老切片等待超时 ({oldest_age_sec:.0f}/{args.max_delay_seconds} 秒)")

    print("\n" + "=" * 65)
    print(f"[UPLOADER] 触发批量上传！原因: {', '.join(reason)}")
    print(f"           当前积攒切片: {total_count} 段, 总容量: {total_size_mb:.2f} MB")
    print("=" * 65)

    # 选取本批次上传的切片（最多 batch_count 个）
    batch_segments = candidates[:args.batch_count]
    batch_id = f"batch_{now_utc.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    batch_work_dir = uploading_dir / batch_id
    batch_work_dir.mkdir(parents=True, exist_ok=True)

    # 原子移入 uploading 目录
    active_segments = []
    for c in batch_segments:
        target_path = batch_work_dir / c["path"].name
        try:
            os.replace(c["path"], target_path)
            active_segments.append(target_path)
        except Exception as e:
            print(f"[UPLOADER] ⚠️ 移动切片至工作区失败 ({c['path'].name}): {e}")

    if not active_segments:
        shutil.rmtree(batch_work_dir, ignore_errors=True)
        return False

    # 执行上传
    try:
        client.login()
        success_count = 0
        for seg_dir in active_segments:
            print(f"\n -> 正在上传切片: {seg_dir.name} ...")
            records = upload_segment(
                client, seg_dir, project_id,
                device_id=args.device_id,
                camera_set_id=getattr(args, "camera_set_id", None),
                layout_version=getattr(args, "camera_layout_version", None)
            )
            for r in records:
                print(f"    [OK] 采集组入库成功: CaptureGroup={r['capture_group_id']} (批次={r['batch_id']})")
            success_count += 1

        # 全部切片上传完成，移入 completed 归档区
        target_completed_dir = completed_dir / batch_id
        os.replace(batch_work_dir, target_completed_dir)
        print(f"\n[UPLOADER] [OK] 批次 {batch_id} 上传全部完成 (共 {success_count} 个切片)！")
        print(f"           已归档至 -> {target_completed_dir}")

        # 触发磁盘清理
        clean_disk_if_needed(spool_dir, args.min_disk_free_gb, args.retention_hours)
        return True

    except Exception as e:
        print(f"[UPLOADER] [ERROR] 批次上传中断或失败: {e}", file=sys.stderr)
        # 发生异常时，将上传中的切片移回 ready 目录，避免数据丢失
        for seg_dir in active_segments:
            if seg_dir.exists():
                fallback_ready = ready_dir / seg_dir.name
                try:
                    os.replace(seg_dir, fallback_ready)
                    print(f"   [安全回退] 切片已恢复至就绪区: {fallback_ready.name}")
                except Exception:
                    pass
        shutil.rmtree(batch_work_dir, ignore_errors=True)
        raise


def main():
    global RUNNING
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(description="RK3588 定量/定时批量上传守护进程")
    parser.add_argument("--config", default="config/edge_continuous.json", help="配置文件路径")
    parser.add_argument("--server", default=None, help="平台服务器地址，例如 http://192.168.5.100:8080")
    parser.add_argument("--project-id", default=None, help="平台项目 UUID")
    parser.add_argument("--device-id", default=None, help="设备标识符")
    parser.add_argument("--spool", default=None, help="缓冲池根目录")
    parser.add_argument("--batch-count", type=int, default=None, help="批量上传数量阈值 (默认 5 段)")
    parser.add_argument("--batch-size-mb", type=float, default=None, help="批量上传容量阈值 (默认 500 MB)")
    parser.add_argument("--max-delay-seconds", type=int, default=None, help="最长等待时间阈值 (默认 1800 秒 / 30分钟)")
    parser.add_argument("--poll-interval", type=int, default=None, help="轮询检查间隔(秒)，默认 5 秒")
    parser.add_argument("--retention-hours", type=float, default=None, help="上传成功文件本地保留时长(小时)，默认 48 小时")
    parser.add_argument("--min-disk-free-gb", type=float, default=None, help="最低安全磁盘剩余空间(GB)，默认 10 GB")
    parser.add_argument("--username", default=None, help="平台管理员账号")
    parser.add_argument("--password", default=None, help="平台管理员密码")
    parser.add_argument("--token", default=None, help="平台身份 Token")
    parser.add_argument("--once", action="store_true", help="单次扫描处理后退出 (调试用)")

    args = parser.parse_args()

    # 读取配置文件
    cfg_file = Path(args.config) if args.config else None
    cfg = {}
    if cfg_file and cfg_file.exists():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"[UPLOADER] [WARN] 读取配置文件出错: {e}", file=sys.stderr)

    # 优先级: CLI 命令行参数 > 配置文件配置 > 代码内置默认值
    args.server = args.server or cfg.get("platform_base_url") or "http://localhost:8080"
    args.project_id = args.project_id or cfg.get("project_id")
    args.device_id = args.device_id or cfg.get("device_id") or "rk3588-01"
    args.camera_set_id = cfg.get("camera_set_id") if is_valid_uuid(cfg.get("camera_set_id")) else None
    args.camera_layout_version = cfg.get("camera_layout_version") or "default-layout-v1"
    args.spool = args.spool or cfg.get("spool_dir") or "/data/spool"
    args.batch_count = args.batch_count if args.batch_count is not None else cfg.get("batch_count", 5)
    args.batch_size_mb = args.batch_size_mb if args.batch_size_mb is not None else cfg.get("batch_size_mb", 500.0)
    args.max_delay_seconds = args.max_delay_seconds if args.max_delay_seconds is not None else cfg.get("max_delay_seconds", 1800)
    args.poll_interval = args.poll_interval if args.poll_interval is not None else cfg.get("poll_interval", 5)
    args.retention_hours = args.retention_hours if args.retention_hours is not None else cfg.get("retention_hours", 48.0)
    args.min_disk_free_gb = args.min_disk_free_gb if args.min_disk_free_gb is not None else cfg.get("min_disk_free_gb", 10.0)
    args.username = args.username or cfg.get("username") or "admin"
    args.password = args.password or cfg.get("password") or "admin12345678"
    args.token = args.token or cfg.get("token")

    client = PlatformClient(args.server, username=args.username, password=args.password, token=args.token)

    # 尝试登录并确认项目
    print("=" * 65)
    print(" [UPLOADER] RK3588 定量批量上传守护进程启动")
    print(f"   * 平台地址:     {args.server}")
    print(f"   * 缓冲池目录:   {Path(args.spool).resolve()}")
    print(f"   * 触发条件:     >= {args.batch_count} 段 OR >= {args.batch_size_mb} MB OR 积攒超时 >= {args.max_delay_seconds} 秒")
    print(f"   * 轮询间隔:     {args.poll_interval} 秒")
    print(f"   * 本地保留期:   {args.retention_hours} 小时")
    print("=" * 65)

    backoff = 5

    while RUNNING:
        try:
            # 解析与验证 project_id
            client.login()
            active_projects = client.request("GET", "/projects").get("items", [])
            if not active_projects:
                raise RuntimeError("平台未建立任何可用项目，请先在平台新建项目")
            active_ids = {p["project_id"] for p in active_projects}
            if not args.project_id or args.project_id not in active_ids:
                if args.project_id:
                    print(f"[UPLOADER] [WARN] 项目 {args.project_id} 不存在或不可用，自动切换至有效项目...")
                args.project_id = active_projects[0]["project_id"]
                print(f"[UPLOADER] 自动绑定平台项目: {active_projects[0].get('display_name', args.project_id)} ({args.project_id})")

            processed = process_ready_batches(args, client, args.project_id)
            backoff = 5  # 成功后重置退避计时

            if args.once:
                break

            if not processed:
                time.sleep(args.poll_interval)

        except (urllib.error.URLError, TimeoutError, OSError) as net_err:
            print(f"[UPLOADER] [WARN] 网络连接平台失败 ({net_err})，将在 {backoff} 秒后重试 (切片已安全留在本地就绪池)...")
            if args.once:
                sys.exit(1)
            time.sleep(backoff)
            backoff = min(60, backoff * 2)
        except Exception as err:
            print(f"[UPLOADER] [ERROR] 循环异常: {err}", file=sys.stderr)
            if args.once:
                sys.exit(1)
            time.sleep(backoff)
            backoff = min(60, backoff * 2)

    print("[UPLOADER] 上传守护进程已安全停止。")


if __name__ == "__main__":
    main()
