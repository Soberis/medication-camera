#!/usr/bin/env python3
"""
test_loop_pipeline.py - 持续切片录制与定量批量上传流水线自动化测试
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON_EXE = sys.executable


class TestLoopPipeline(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_pipeline_")
        self.spool_dir = Path(self.temp_dir) / "spool"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_recorder_generates_ready_segments(self):
        """测试 loop_recorder.py 生成符合规范的就绪切片"""
        recorder_script = PROJECT_ROOT / "tools" / "loop_recorder.py"
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        cmd = [
            PYTHON_EXE, str(recorder_script),
            "--duration", "2",
            "--spool", str(self.spool_dir),
            "--synthetic",
            "--max-segments", "2"
        ]
        result = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", env=env, timeout=30)
        self.assertEqual(result.returncode, 0, f"Recorder failed: {result.stderr}")

        ready_dir = self.spool_dir / "ready"
        self.assertTrue(ready_dir.exists())
        segments = [p for p in ready_dir.iterdir() if p.is_dir()]
        self.assertEqual(len(segments), 2, "应生成恰好 2 个切片")

        for seg in segments:
            meta_file = seg / "segment_meta.json"
            self.assertTrue(meta_file.exists(), f"缺少元数据: {meta_file}")
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
            self.assertEqual(meta["schema_version"], "segment-recording-v1")
            self.assertEqual(len(meta["videos"]), 1)
            video_entry = meta["videos"][0]
            self.assertEqual(video_entry["file_name"], "camera_1.mp4")
            self.assertTrue((seg / "camera_1.mp4").exists())
            self.assertGreater(video_entry["size_bytes"], 0)
            self.assertEqual(len(video_entry["sha256"]), 64)

    def test_02_uploader_threshold_and_upload(self):
        """测试 batch_uploader.py 数量阈值触发并成功将切片入库平台"""
        recorder_script = PROJECT_ROOT / "tools" / "loop_recorder.py"
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        # 1. 产生 2 个切片
        subprocess.run([
            PYTHON_EXE, str(recorder_script),
            "--duration", "1",
            "--spool", str(self.spool_dir),
            "--synthetic",
            "--max-segments", "2"
        ], check=True, capture_output=True, encoding="utf-8", errors="replace", env=env)

        ready_dir = self.spool_dir / "ready"
        self.assertEqual(len(list(ready_dir.iterdir())), 2)

        # 2. 运行 uploader，阈值设为 3 (不满足)，验证不会误上传
        uploader_script = PROJECT_ROOT / "tools" / "batch_uploader.py"
        res = subprocess.run([
            PYTHON_EXE, str(uploader_script),
            "--spool", str(self.spool_dir),
            "--batch-count", "3",
            "--batch-size-mb", "100",
            "--max-delay-seconds", "3600",
            "--server", "http://localhost:8080",
            "--once"
        ], capture_output=True, encoding="utf-8", errors="replace", env=env)
        self.assertEqual(res.returncode, 0)
        # 验证 ready 目录仍然保留 2 个切片
        self.assertEqual(len(list(ready_dir.iterdir())), 2)

        # 3. 运行 uploader，阈值设为 2 (满足)，执行上传并入库
        res = subprocess.run([
            PYTHON_EXE, str(uploader_script),
            "--spool", str(self.spool_dir),
            "--batch-count", "2",
            "--batch-size-mb", "100",
            "--max-delay-seconds", "3600",
            "--server", "http://localhost:8080",
            "--once"
        ], capture_output=True, encoding="utf-8", errors="replace", env=env)
        self.assertEqual(res.returncode, 0, f"Uploader failed: {res.stderr}\nOutput: {res.stdout}")
        self.assertTrue("触发批量上传" in res.stdout or "batch_" in res.stdout, f"Expected batch trigger in: {res.stdout}")
        self.assertTrue("上传全部完成" in res.stdout or "[OK] 批次" in res.stdout, f"Expected upload complete in: {res.stdout}")

        # 4. 验证 ready 目录已清空，completed 目录有已完成批次
        self.assertEqual(len(list(ready_dir.iterdir())), 0)
        completed_dir = self.spool_dir / "completed"
        self.assertTrue(completed_dir.exists())
        completed_batches = list(completed_dir.iterdir())
        self.assertEqual(len(completed_batches), 1)


if __name__ == "__main__":
    unittest.main()
