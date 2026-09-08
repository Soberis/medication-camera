"""Opt-in real platform test. Generates synthetic media; never opens a camera."""
import math
import os
import subprocess
import time
import uuid

from test_edge import ROOT, config, edge


def main():
    if os.environ.get("RUN_CAMERA_UPLOAD_E2E") != "1":
        raise SystemExit("Set RUN_CAMERA_UPLOAD_E2E=1 for an isolated synthetic platform environment")
    value = config()
    value["platform_base_url"] = os.environ.get("CAMERA_TEST_BASE_URL", "http://localhost:28080")
    value["project_id"] = os.environ.get("CAMERA_TEST_PROJECT_ID", "00000000-0000-0000-0000-000000000001")
    group_key = str(uuid.uuid4())
    prefix = "synthetic-camera-" + group_key
    value["device_id"] = prefix
    value["source_system"] = "synthetic-camera-uploader-test"
    value["camera_set_key"] = prefix
    for camera in value["cameras"]:
        camera["serial"] = f"SYNTHETIC{camera['camera_number']}"
        camera["provisioning_key"] = prefix + f".camera-{camera['camera_number']}"
    directory = ROOT / ".runtime" / prefix
    recording = directory / "recording"
    recording.mkdir(parents=True)
    config_path = directory / "config.json"
    edge.atomic_json(config_path, value)
    edge.provision(config_path)
    value = edge.read_json(config_path)
    timestamp = edge.now()
    edge.atomic_json(directory / "task.json", {"config": value, "capture_group_key": group_key,
                     "capture_session_id": str(uuid.uuid4()), "captured_at": timestamp})
    videos = []
    for camera in value["cameras"]:
        name = f"camera_{camera['camera_number']}.raw.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=1920x1080:rate=30", "-t", "2", "-c:v", "mpeg4", str(recording / name)], check=True)
        videos.append({"camera_number": camera["camera_number"], "serial": camera["serial"],
                       "file_name": name, "record_start_at": timestamp, "frames": 60})
    edge.atomic_json(recording / "capture-result.json", {"schema_version": "camera-recording-v1", "videos": videos})
    prepared = edge.prepare(directory)

    class InterruptedPlatform(edge.Platform):
        def __init__(self):
            super().__init__(value, os.environ.get("PLATFORM_TOKEN"))
            self.puts = 0

        def put_part(self, url, data):
            super().put_part(url, data)
            self.puts += 1
            if self.puts == 1:
                raise edge.ApiError("Intentional interruption after object PUT, before confirmation")

    api = InterruptedPlatform()
    try:
        edge.upload(directory, api)
    except edge.ApiError:
        if api.puts != 1:
            raise
    else:
        raise AssertionError("Expected first-upload interruption")
    resumed = edge.upload(directory, api)
    repeated = edge.upload(directory, api)
    assert repeated["batch_id"] == resumed["batch_id"]
    assert api.puts == sum(math.ceil(f["size_bytes"] / f["part_size_bytes"]) for f in prepared["files"])
    deadline = time.monotonic() + 120
    while not edge.status(directory, api):
        if time.monotonic() >= deadline:
            raise AssertionError("Media processing did not reach INGESTED within 120 seconds")
        time.sleep(2)
    print("PASS: real V2 dual-video ingest, interrupted upload recovery, idempotent replay, and Media Probe")
    print(f"Synthetic evidence: {directory}")


if __name__ == "__main__":
    main()
