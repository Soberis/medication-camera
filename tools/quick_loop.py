#!/usr/bin/env python3
"""
quick_loop.py - 最小闭环工具：RK3588 录制与向 medication-video-platform 一键上传 (无第三方依赖)

用法：
  # 1. 录制 10 秒测试视频 (输出到 output/camera_1.mp4)
  python3 tools/quick_loop.py record --duration 10

  # 2. 一键上传至平台并入库
  python3 tools/quick_loop.py upload output/camera_1.mp4 --server http://192.168.x.x:8080
"""
import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

PART_SIZE = 8 * 1024 * 1024  # 8MB


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
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        try:
            with self.opener.open(req, timeout=10) as resp:
                # check cookies for XSRF-TOKEN
                for cookie in self.cookie_jar:
                    if cookie.name == "XSRF-TOKEN":
                        self.csrf_token = cookie.value
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"登录平台失败 (HTTP {e.code}): {e.read().decode('utf-8', errors='ignore')}") from None

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
            with self.opener.open(req, timeout=30) as resp:
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

        if parsed_s3.hostname in ("localhost", "127.0.0.1") and parsed_base.hostname not in ("localhost", "127.0.0.1"):
            # 预签名 URL 的主机为 localhost，但客户端从远端 IP 访问，需将目标重写为服务器 IP 与局域网代理端口 19001
            headers["Host"] = parsed_s3.netloc
            s3_port = 19001 if (parsed_s3.port in (19000, 9000) or not parsed_s3.port) else parsed_s3.port
            target_netloc = f"{parsed_base.hostname}:{s3_port}"
            target_url = urllib.parse.urlunsplit((parsed_s3.scheme, target_netloc, parsed_s3.path, parsed_s3.query, parsed_s3.fragment))

        req = urllib.request.Request(
            target_url,
            data=data,
            headers=headers,
            method="PUT"
        )
        # S3 预签名直传不带平台鉴权 Header
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"分片直传对象存储失败: HTTP {resp.status}")


def do_record(duration, output_path, config_path, synthetic):
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = output_path.parent / f".rec_tmp_{uuid.uuid4().hex[:8]}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/3] 开始录制视频 (时长: {duration}秒)...")
    raw_video = temp_dir / "camera_1.raw.mp4"

    if synthetic:
        print("  -> 使用 FFmpeg 生成合成测试视频 (未连接相机模式)...")
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"testsrc=duration={duration}:size=1920x1080:rate=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(raw_video)
        ]
        subprocess.run(cmd, check=True)
    else:
        recorder_bin = Path("build/medication_camera").resolve()
        if not recorder_bin.exists():
            recorder_bin = shutil.which("medication_camera")
            if recorder_bin:
                recorder_bin = Path(recorder_bin)

        if not recorder_bin or not recorder_bin.exists():
            print(f"  [提示] 未在 build/ 找到编译产物 medication_camera，尝试调用合成录像...")
            cmd = [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", f"testsrc=duration={duration}:size=1920x1080:rate=30",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(raw_video)
            ]
            subprocess.run(cmd, check=True)
        else:
            cfg = Path(config_path).resolve()
            if not cfg.exists():
                cfg = temp_dir / "record_cfg.json"
                cfg.write_text(json.dumps({
                    "duration_seconds": duration,
                    "cameras": [{"camera_number": 1, "serial": "auto"}]
                }), encoding="utf-8")
            rec_dir = temp_dir / "recording"
            cmd = [str(recorder_bin), str(cfg), str(rec_dir)]
            print(f"  -> 执行底层录像: {' '.join(cmd)}")
            subprocess.run(cmd, check=True, timeout=duration + 30)
            raw_candidates = list(rec_dir.glob("camera_*.raw.mp4"))
            if not raw_candidates:
                raise RuntimeError("录像程序退出但未找到 camera_*.raw.mp4 文件")
            raw_video = raw_candidates[0]

    print(f"[2/3] 转码为 Web 播放兼容格式 (H.264 / yuv420p)...")
    transcode_cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(raw_video),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output_path)
    ]
    subprocess.run(transcode_cmd, check=True)

    # 清理临时文件
    shutil.rmtree(temp_dir, ignore_errors=True)
    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"[3/3] 录制与转码完成: {output_path} ({size_mb:.2f} MB)")
    print(f"  验证命令: ffprobe -hide_banner {output_path}")


def do_upload(video_path, server_url, project_id=None, username="admin", password="admin12345678", token=None):
    video_path = Path(video_path).resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"视频文件不存在: {video_path}")

    size_bytes = video_path.stat().st_size
    print(f"[1/5] 读取视频文件: {video_path.name} ({size_bytes / (1024 * 1024):.2f} MB)")
    print(f"      计算 SHA-256 摘要中...")
    file_hash = file_sha256(video_path)

    client = PlatformClient(server_url, username=username, password=password, token=token)
    print(f"[2/5] 连接平台与身份鉴权: {server_url} ...")
    client.login()

    if not project_id:
        # 获取可用项目列表
        projects_resp = client.request("GET", "/projects")
        projects = projects_resp.get("items", []) or projects_resp.get("projects", [])
        if not projects:
            raise RuntimeError("未在平台找到任何可用项目，请先在平台创建或使用默认项目")
        project_id = projects[0]["project_id"]
        print(f"      自动选择当前项目: {projects[0].get('display_name', project_id)} ({project_id})")

    client_file_id = str(uuid.uuid4())
    content_type = "video/mp4"

    # 计算平台规范的清单摘要
    manifest_lines = [
        "upload-file-manifest-v1\n",
        f"{client_file_id.lower()}\tVIDEO\t{content_type}\t{size_bytes}\t{file_hash}\t{PART_SIZE}\n"
    ]
    manifest_sha256 = hashlib.sha256("".join(manifest_lines).encode("utf-8")).hexdigest()

    print(f"[3/5] 初始化单视频上传批次 (PLATFORM_LOCAL)...")
    init_body = {
        "project_id": project_id,
        "device_id": "PLATFORM_LOCAL",
        "upload_source_type": "PLATFORM_LOCAL",
        "file_count": 1,
        "total_size_bytes": size_bytes,
        "manifest_sha256": manifest_sha256
    }
    batch = client.request("POST", "/upload/init", init_body, idempotency_key=str(uuid.uuid4()))
    batch_id = batch["batch_id"]

    # 登记文件
    client.request(
        "PUT",
        f"/upload/{batch_id}/files/{client_file_id}",
        {
            "file_kind": "VIDEO",
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256": file_hash,
            "part_size_bytes": PART_SIZE,
            "file_name": video_path.name
        },
        idempotency_key=str(uuid.uuid4())
    )

    # 开启分片会话
    client.request("PUT", f"/upload/{batch_id}/files/{client_file_id}/multipart")

    # 分片上传至 MinIO 对象存储
    total_parts = (size_bytes + PART_SIZE - 1) // PART_SIZE
    print(f"[4/5] 直传视频数据到平台对象存储 (共 {total_parts} 个分片)...")
    with open(video_path, "rb") as f:
        for part_number in range(1, total_parts + 1):
            data = f.read(PART_SIZE)
            part_hash = hashlib.sha256(data).hexdigest()
            # 获取预签名直传授权 URL
            auth_resp = client.request(
                "POST",
                f"/upload/{batch_id}/files/{client_file_id}/parts/{part_number}/authorization",
                {"sha256": part_hash}
            )
            upload_url = auth_resp["upload_url"]
            # 上传到 S3
            client.put_part_to_s3(upload_url, data)
            # 确认分片
            client.request("PUT", f"/upload/{batch_id}/files/{client_file_id}/parts/{part_number}/confirmation")
            print(f"      已上传分片 {part_number}/{total_parts} ({len(data)} bytes)")

    # 完成单文件
    client.request("PUT", f"/upload/{batch_id}/files/{client_file_id}/completion")
    # 完成批次并校验
    completion_resp = client.request("PUT", f"/upload/{batch_id}/completion")
    clip_assets = completion_resp.get("clip_assets", [])
    clip_asset_id = clip_assets[0]["clip_asset_id"] if clip_assets else None

    # 自动建立采集会话并入库为 CaptureGroup
    print(f"[5/5] 触发平台数据收件与 CaptureGroup 组装...")
    session_resp = client.request("PUT", f"/upload/{batch_id}/capture-session")
    capture_session_id = session_resp.get("capture_session_id")
    group_id = session_resp.get("capture_group_id", capture_session_id)

    print("\n" + "=" * 60)
    print("[SUCCESS] 最小闭环上传成功！")
    print(f"  * 批次 ID (Batch ID):         {batch_id}")
    print(f"  * 采集组 ID (CaptureGroup ID): {group_id}")
    print(f"  * 切片资产 ID (ClipAsset ID):  {clip_asset_id}")
    print(f"  * 所属项目 ID:                 {project_id}")
    print("\n[HINT] 请在电脑浏览器打开平台网页查看与播放视频：")
    web_host = server_url.split(":")[1].replace("//", "")
    print(f"  http://{web_host}:5173/projects/{project_id}")
    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="medication 最小闭环录制与上传工具")
    subparsers = parser.add_subparsers(dest="action", required=True)

    # 录制命令
    rec_parser = subparsers.add_parser("record", help="录制 MP4 视频")
    rec_parser.add_argument("--duration", type=int, default=10, help="录制时长(秒)，默认10秒")
    rec_parser.add_argument("--output", default="output/camera_1.mp4", help="输出视频路径，默认 output/camera_1.mp4")
    rec_parser.add_argument("--config", default="config/single_camera.json", help="录制配置文件")
    rec_parser.add_argument("--synthetic", action="store_true", help="使用 FFmpeg 生成合成视频(不调用真实相机)")

    # 上传命令
    up_parser = subparsers.add_parser("upload", help="上传 MP4 视频到平台")
    up_parser.add_argument("video", help="待上传的 MP4 视频文件路径")
    up_parser.add_argument("--server", default="http://localhost:8080", help="平台 Core API 地址，例如 http://192.168.1.100:8080")
    up_parser.add_argument("--project-id", help="平台项目 UUID (若省略则自动获取第一个项目)")
    up_parser.add_argument("--username", default="admin", help="平台管理员账号，默认 admin")
    up_parser.add_argument("--password", default="admin12345678", help="平台管理员密码，默认 admin12345678")
    up_parser.add_argument("--token", help="可选 Bearer Token (若指定则跳过账号密码登录)")

    args = parser.parse_args()

    try:
        if args.action == "record":
            do_record(args.duration, args.output, args.config, args.synthetic)
        elif args.action == "upload":
            do_upload(args.video, args.server, args.project_id, args.username, args.password, args.token)
    except Exception as err:
        print(f"\n❌ 执行失败: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
