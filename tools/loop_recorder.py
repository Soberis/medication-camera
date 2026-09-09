#!/usr/bin/env python3
"""
loop_recorder.py - RK3588 持续切片录制守护进程 (无第三方依赖)

功能：
  1. 开机后无限循环录制定长切片（默认 180 秒 / 3分钟）。
  2. 支持真实摄像头（调用 medication_camera 二进制）或合成模式 (--synthetic)。
  3. 自动将原始 raw 视频高质量转码为兼容 Web 播放的 H.264 / yuv420p MP4。
  4. 严格采用原子目录移动 (spool/recording -> spool/ready)，避免与上传进程并发冲突。
  5. 响应 SIGINT/SIGTERM 优雅停机，保障已录制切片的完整性。
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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


def signal_handler(signum, frame):
    global RUNNING
    sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
    print(f"\n[RECORDER] 接收到停机信号 ({sig_name})，准备安全退出...")
    RUNNING = False


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def calc_sha256(path, chunk_size=8 * 1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def transcode_video(raw_path, output_path, ffmpeg_bin="ffmpeg"):
    """使用 FFmpeg 将 raw 视频转码为 H.264 / yuv420p 标准 Web MP4"""
    cmd = [
        ffmpeg_bin, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(raw_path),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output_path)
    ]
    subprocess.run(cmd, check=True)


def record_one_segment(duration, segment_dir, config_path, recorder_bin, ffmpeg_bin, synthetic=False):
    """录制单个切片并转码，写入 segment_meta.json"""
    segment_dir = Path(segment_dir)
    segment_dir.mkdir(parents=True, exist_ok=True)
    rec_work_dir = segment_dir / "raw_capture"
    # 确保录像工作目录在录制前不存在（medication_camera 会自身使用 create_directory 创建）
    if rec_work_dir.exists():
        shutil.rmtree(rec_work_dir, ignore_errors=True)

    captured_at = utc_now()
    raw_files = []

    if synthetic:
        # 合成模式：生成一个标准测试画面 MP4 (时长 duration 秒)
        rec_work_dir.mkdir(parents=True, exist_ok=True)
        raw_target = rec_work_dir / "camera_1.raw.mp4"
        cmd = [
            ffmpeg_bin, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=1920x1080:rate=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(raw_target)
        ]
        subprocess.run(cmd, check=True)
        raw_files = [raw_target]
    else:
        # 调用 C++ medication_camera 录像
        rec_bin = Path(recorder_bin).resolve()
        if not rec_bin.exists():
            candidate = shutil.which(recorder_bin)
            if candidate:
                rec_bin = Path(candidate)
            else:
                raise FileNotFoundError(f"未找到录像可执行程序: {recorder_bin}，请先编译 build/medication_camera")

        # 生成规范的 OpenCV 录像配置 (medication_camera 仅需要 duration_seconds 与 cameras)
        cfg_cameras = [{"camera_number": 1, "serial": "auto"}]
        if config_path and Path(config_path).exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                    if "cameras" in cdata and isinstance(cdata["cameras"], list) and cdata["cameras"]:
                        cfg_cameras = [
                            {"camera_number": int(c.get("camera_number", 1)), "serial": str(c.get("serial", "auto"))}
                            for c in cdata["cameras"]
                        ]
            except Exception:
                pass

        cfg_file = segment_dir / "recording-config.json"
        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump({
                "duration_seconds": int(duration),
                "cameras": cfg_cameras
            }, f, indent=2)

        # 注意：medication_camera 内部使用 std::filesystem::create_directory(argv[2]) 创建输出目录
        # 因此 rec_work_dir 在调用前绝不能预先存在！
        cmd = [str(rec_bin), str(cfg_file), str(rec_work_dir)]
        subprocess.run(cmd, check=True, timeout=duration + 60)
        raw_files = sorted(list(rec_work_dir.glob("camera_*.raw.mp4")))
        if not raw_files:
            raise RuntimeError("录像程序退出，但未在工作目录产生 camera_*.raw.mp4")

    # 转码所有摄像头原始视频
    video_entries = []
    for raw in raw_files:
        cam_num = 1
        name = raw.stem.split(".")[0]  # e.g. camera_1
        if "_" in name and name.split("_")[-1].isdigit():
            cam_num = int(name.split("_")[-1])

        transcoded_name = f"camera_{cam_num}.mp4"
        final_video_path = segment_dir / transcoded_name
        transcode_video(raw, final_video_path, ffmpeg_bin=ffmpeg_bin)

        size_bytes = final_video_path.stat().st_size
        sha256 = calc_sha256(final_video_path)
        video_entries.append({
            "camera_number": cam_num,
            "file_name": transcoded_name,
            "size_bytes": size_bytes,
            "sha256": sha256
        })

    # 清理 raw 临时目录以节省磁盘空间
    shutil.rmtree(rec_work_dir, ignore_errors=True)

    meta = {
        "schema_version": "segment-recording-v1",
        "segment_id": segment_dir.name,
        "captured_at": captured_at,
        "duration_seconds": duration,
        "videos": video_entries
    }
    meta_path = segment_dir / "segment_meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    return meta


def main():
    global RUNNING
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(description="RK3588 持续切片录制守护进程")
    parser.add_argument("--duration", type=int, default=180, help="切片录制时长(秒)，默认 180 秒 (3分钟)")
    parser.add_argument("--spool", default="/data/spool", help="缓冲池根目录，默认 /data/spool")
    parser.add_argument("--config", default="config/single_camera.json", help="录制配置文件路径")
    parser.add_argument("--recorder", default="build/medication_camera", help="C++ 录像程序路径")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg 可执行路径")
    parser.add_argument("--synthetic", action="store_true", help="使用 FFmpeg 合成画面模式(调试用，无需相机硬件)")
    parser.add_argument("--trigger-mode", choices=["loop", "gpio", "usb_hid", "http"], help="触发模式: loop (持续循环) | gpio (引脚按键) | usb_hid (USB脚踏) | http (REST接口)")
    parser.add_argument("--max-duration", type=int, help="单次录制最大时长限制(秒)")
    parser.add_argument("--max-segments", type=int, default=0, help="最多录制切片数量(0 表示无限循环)")

    args = parser.parse_args()

    # 读取配置文件
    cfg_file = Path(args.config) if args.config else None
    cfg = {}
    if cfg_file and cfg_file.exists():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass

    trigger_mode = args.trigger_mode or cfg.get("trigger", {}).get("mode", "loop")
    duration = args.duration or cfg.get("duration_seconds", 180)

    spool_dir = Path(args.spool).resolve()
    recording_dir = spool_dir / "recording"
    ready_dir = spool_dir / "ready"
    recording_dir.mkdir(parents=True, exist_ok=True)
    ready_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 65)
    print(" [RECORDER] RK3588 切片录像守护进程启动")
    print(f"   * 触发模式:     {trigger_mode}")
    print(f"   * 切片时长:     {duration} 秒 ({duration / 60:.1f} 分钟)")
    print(f"   * 暂存就绪目录: {ready_dir}")
    print(f"   * 录像后端:     {'FFmpeg 合成模拟' if args.synthetic else args.recorder}")
    print("=" * 65)

    segments_recorded = 0

    if trigger_mode == "loop":
        # 持续循环录制模式
        while RUNNING:
            if args.max_segments > 0 and segments_recorded >= args.max_segments:
                print(f"[RECORDER] 已达到最大切片数限制 ({args.max_segments})，结束录制。")
                break

            timestamp_str = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
            segment_id = f"segment_{timestamp_str}_{uuid.uuid4().hex[:6]}"
            working_segment_dir = recording_dir / segment_id
            target_ready_dir = ready_dir / segment_id

            print(f"\n[RECORDER] 开始录制切片 #{segments_recorded + 1}: {segment_id} ({duration}s)...")
            start_t = time.time()

            try:
                meta = record_one_segment(
                    duration=duration,
                    segment_dir=working_segment_dir,
                    config_path=args.config,
                    recorder_bin=args.recorder,
                    ffmpeg_bin=args.ffmpeg,
                    synthetic=args.synthetic
                )
                elapsed = time.time() - start_t

                os.replace(working_segment_dir, target_ready_dir)
                segments_recorded += 1

                total_size_mb = sum(v["size_bytes"] for v in meta["videos"]) / (1024 * 1024)
                print(f"[RECORDER] [OK] 切片就绪: {segment_id} (耗时: {elapsed:.1f}s, 大小: {total_size_mb:.2f} MB)")
                print(f"           移动至 -> {target_ready_dir}")

            except subprocess.TimeoutExpired:
                print(f"[RECORDER] [WARN] 录像超时异常，清理临时切片: {working_segment_dir}", file=sys.stderr)
                shutil.rmtree(working_segment_dir, ignore_errors=True)
                time.sleep(2)
            except Exception as e:
                print(f"[RECORDER] [ERROR] 录制失败: {e}", file=sys.stderr)
                shutil.rmtree(working_segment_dir, ignore_errors=True)
                if not RUNNING:
                    break
                time.sleep(3)
    else:
        # 事件驱动触发模式 (GPIO / USB 脚踏板 / HTTP)
        from tools.triggers import TriggerManager

        trigger_cfg = {
            "duration_seconds": duration,
            "trigger": {
                "mode": trigger_mode,
                "max_duration_seconds": args.max_duration or cfg.get("trigger", {}).get("max_duration_seconds", 300),
                **cfg.get("trigger", {})
            }
        }
        trigger = TriggerManager.create(trigger_cfg)

        active_session = {}

        def handle_start(event):
            nonlocal segments_recorded
            session_id = event.metadata.get("session_id") or f"session_{uuid.uuid4().hex[:8]}"
            working_dir = recording_dir / session_id
            print(f"\n[RECORDER] [TRIGGER-EVENT] 开始录像会话: {session_id}")
            active_session["dir"] = working_dir
            active_session["id"] = session_id
            active_session["meta"] = event.metadata
            active_session["start_time"] = time.time()

        def handle_stop(event):
            nonlocal segments_recorded
            if "dir" not in active_session:
                return
            working_dir = active_session.pop("dir")
            session_id = active_session.pop("id")
            meta_info = active_session.pop("meta", {})
            elapsed = time.time() - active_session.pop("start_time", time.time())
            record_seconds = max(1, int(elapsed))

            target_ready_dir = ready_dir / session_id
            print(f"[RECORDER] [TRIGGER-EVENT] 正在完成会话并转码: {session_id} (时长: {record_seconds}s)...")
            try:
                meta = record_one_segment(
                    duration=record_seconds,
                    segment_dir=working_dir,
                    config_path=args.config,
                    recorder_bin=args.recorder,
                    ffmpeg_bin=args.ffmpeg,
                    synthetic=args.synthetic
                )
                meta["trigger_metadata"] = meta_info
                # 更新元数据文件
                with open(working_dir / "segment_meta.json", "w", encoding="utf-8") as f:
                    json.dump(meta, f, indent=2, ensure_ascii=False)

                os.replace(working_dir, target_ready_dir)
                segments_recorded += 1
                total_mb = sum(v["size_bytes"] for v in meta["videos"]) / (1024 * 1024)
                print(f"[RECORDER] [OK] 事件录制切片已就绪: {session_id} ({total_mb:.2f} MB)")
                print(f"           移动至就绪池 -> {target_ready_dir}")
            except Exception as e:
                print(f"[RECORDER] [ERROR] 处理会话失败: {e}", file=sys.stderr)
                shutil.rmtree(working_dir, ignore_errors=True)

        trigger.start_listening(on_start=handle_start, on_stop=handle_stop)

        try:
            while RUNNING:
                if args.max_segments > 0 and segments_recorded >= args.max_segments:
                    print(f"[RECORDER] 已达到最大切片数限制 ({args.max_segments})，结束退出。")
                    break
                time.sleep(0.2)
        finally:
            trigger.stop_listening()

    print("[RECORDER] 录像守护进程已安全停止。")


if __name__ == "__main__":
    main()
