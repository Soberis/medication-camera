import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("edge", ROOT / "tools" / "edge.py")
edge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(edge)


def config():
    value = edge.read_json(ROOT / "config" / "edge.example.json")
    value["project_id"] = str(uuid.uuid4())
    value["camera_set_id"] = str(uuid.uuid4())
    value["platform_base_url"] = "http://localhost:8080"
    for camera in value["cameras"]:
        camera["camera_channel_id"] = str(uuid.uuid4())
    return value


def upload_fixture(directory):
    files = []
    # More than one full part, plus a tail: verifies actual resume across part boundaries.
    for index, data in enumerate([b"a" * (edge.PART_SIZE + 37), b"second-video", b"{}"]):
        name = f"synthetic-{index}.dat"
        (directory / name).write_bytes(data)
        kind = ["VIDEO", "VIDEO", "METADATA"][index]
        content_type = ["video/mp4", "video/mp4", "application/json"][index]
        files.append(edge.file_entry(directory, name, kind, content_type))
    state = {"schema_version": "camera-upload-state-v1", "config": config(), "files": files,
             "external_manifest": {"capture_group": {"capture_group_key": str(uuid.uuid4())}},
             "manifest_sha256": edge.manifest_hash(files), "init_key": "test-" + str(uuid.uuid4()), "status": "READY"}
    edge.atomic_json(directory / "upload-state.json", state)
    return state


class FakePlatform:
    """State machine matching the platform controllers, with response-loss injection."""
    def __init__(self, state, fail_at=None):
        self.state = state
        self.batch_id = str(uuid.uuid4())
        self.manifest_id = str(uuid.uuid4())
        self.group_id = str(uuid.uuid4())
        self.batch_status = "INITIATED"
        self.files = {}
        self.storage = {}
        self.puts = []
        self.keys = []
        self.fail_at = fail_at
        self.did_fail = False
        self.manifest_count = 0
        self.batch_count = 0
        self.finalized = False

    def respond(self, stage, value):
        if stage == self.fail_at and not self.did_fail:
            self.did_fail = True
            raise edge.ApiError("Injected response loss")
        return copy.deepcopy(value)

    def request(self, method, path, body=None, key=None):
        if path.endswith("external-video-ingest-manifests/v2"):
            assert body == self.state["external_manifest"]
            self.manifest_count += 1
            return self.respond("manifest", {"manifest_id": self.manifest_id})
        if path == "/upload/init":
            assert key == self.state["init_key"]
            assert body["manifest_sha256"] == self.state["manifest_sha256"]
            assert body["file_count"] == len(self.state["files"])
            assert body["total_size_bytes"] == sum(f["size_bytes"] for f in self.state["files"])
            self.keys.append(key)
            self.batch_count += 1
            return self.respond("init", {"batch_id": self.batch_id})
        if path.endswith("receipt/finalize"):
            assert method == "POST"
            self.finalized = True
            return self.respond("finalize", {"receipt_state": "COMPLETE", "expected_camera_numbers": [1, 2],
                                             "received_camera_numbers": [1, 2], "missing_camera_numbers": []})
        prefix = "/upload/" + self.batch_id
        assert path.startswith(prefix)
        suffix = path[len(prefix):]
        if suffix == "":
            return {"status": self.batch_status}
        if suffix == "/files":
            return {"files": list(copy.deepcopy(self.files).values())}
        if suffix == "/completion":
            assert all(file["status"] == "UPLOADED" for file in self.files.values())
            self.batch_status = "COMPLETED"
            return self.respond("batch-completion", {
                "verified_manifest_sha256": self.state["manifest_sha256"],
                "clip_assets": [{"clip_asset_id": f["client_file_id"], "client_file_id": f["client_file_id"]}
                                for f in self.state["files"] if f["file_kind"] == "VIDEO"]})
        if suffix == "/capture-session":
            assert self.batch_status == "COMPLETED"
            return self.respond("session", {"capture_session_id": self.group_id, "capture_group_id": self.group_id})
        parts = suffix.split("/")
        file_id = parts[2]
        if len(parts) == 3:
            assert method == "PUT" and self.batch_status != "COMPLETED"
            self.files[file_id] = dict(body, client_file_id=file_id, status="REGISTERED")
            return self.respond("registration", self.files[file_id])
        file = self.files[file_id]
        if parts[3] == "multipart":
            assert len(self.files) == len(self.state["files"])
            assert file["status"] == "REGISTERED"
            file["status"] = "SESSION_CREATED"
            return self.respond("multipart", {"status": file["status"]})
        if parts[3] == "completion":
            if file["status"] != "UPLOADED":
                data = b"".join(self.storage[(file_id, number)] for number in range(
                    1, (file["size_bytes"] + file["part_size_bytes"] - 1) // file["part_size_bytes"] + 1))
                assert hashlib.sha256(data).hexdigest() == file["sha256"]
                file["status"] = "UPLOADED"
            return self.respond("file-completion", {"verified_sha256": file["sha256"]})
        if parts[4] == "reconciliation":
            assert file["status"] == "UPLOADING", "Platform rejects reconciliation before first authorization"
            return {"parts": [{"part_number": number, "status": "UPLOADED", "sha256": hashlib.sha256(data).hexdigest(),
                               "expected_size_bytes": len(data)} for (identity, number), data in self.storage.items() if identity == file_id]}
        number = int(parts[4])
        if parts[5] == "authorization":
            assert file["status"] in {"SESSION_CREATED", "UPLOADING"}
            assert (file_id, number) not in self.storage, "Do not re-upload recovered parts"
            file["status"] = "UPLOADING"
            self.batch_status = "UPLOADING"
            size = min(file["part_size_bytes"], file["size_bytes"] - (number - 1) * file["part_size_bytes"])
            return self.respond("authorization", {"sha256": body["sha256"], "expected_size_bytes": size,
                                                  "upload_url": f"http://storage.invalid/{file_id}/{number}?secret=not-persisted"})
        assert parts[5] == "confirmation" and (file_id, number) in self.storage
        return self.respond("confirmation", {})

    def put_part(self, url, data):
        parts = edge.urllib.parse.urlsplit(url).path.split("/")
        identity = (parts[1], int(parts[2]))
        self.storage[identity] = data
        self.puts.append(identity)
        self.respond("object-put", {})


class UploadTests(unittest.TestCase):
    def test_resume_after_every_uncertain_boundary(self):
        for boundary in (None, "manifest", "init", "registration", "multipart", "authorization", "object-put",
                         "confirmation", "file-completion", "batch-completion", "session", "finalize"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                state = upload_fixture(directory)
                api = FakePlatform(state, boundary)
                with patch("sys.stdout", new=io.StringIO()):
                    if boundary:
                        with self.assertRaises(edge.ApiError):
                            edge.upload(directory, api)
                        self.assertTrue(api.did_fail)
                    result = edge.upload(directory, api)
                    # Re-running a completed task must not transfer bytes again.
                    edge.upload(directory, api)
                self.assertEqual("UPLOADED", result["status"])
                self.assertTrue(api.finalized)
                self.assertEqual(4, len(api.puts))
                self.assertEqual(1, len(set(api.keys)))
                self.assertNotIn("secret=", (directory / "upload-state.json").read_text())
                self.assertEqual(api.batch_id, result["batch_id"])

    def test_changed_file_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state = upload_fixture(directory)
            api = FakePlatform(state)
            (directory / state["files"][0]["file_name"]).write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "changed"):
                edge.upload(directory, api)
            self.assertEqual(0, api.manifest_count)

    def test_expired_task_keeps_identity_and_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state = upload_fixture(directory)
            api = FakePlatform(state)
            api.batch_status = "EXPIRED"
            with self.assertRaisesRegex(edge.ApiError, "EXPIRED"):
                edge.upload(directory, api)
            self.assertEqual(api.batch_id, edge.read_json(directory / "upload-state.json")["batch_id"])
            self.assertTrue((directory / state["files"][0]["file_name"]).exists())

    def test_manifest_hash_matches_platform_canonical_format(self):
        files = [{"client_file_id": "b", "file_kind": "VIDEO", "content_type": "video/mp4", "size_bytes": 7,
                  "sha256": "d" * 64, "part_size_bytes": edge.PART_SIZE},
                 {"client_file_id": "a", "file_kind": "METADATA", "content_type": "application/json", "size_bytes": 2,
                  "sha256": "e" * 64, "part_size_bytes": edge.PART_SIZE}]
        expected = "upload-file-manifest-v1\na\tMETADATA\tapplication/json\t2\t" + "e" * 64 + "\t8388608\nb\tVIDEO\tvideo/mp4\t7\t" + "d" * 64 + "\t8388608\n"
        self.assertEqual(hashlib.sha256(expected.encode()).hexdigest(), edge.manifest_hash(files))

    def test_lock_excludes_second_writer_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            with edge.task_lock(directory):
                with self.assertRaises(ValueError):
                    with edge.task_lock(directory):
                        self.fail("Acquired the same task twice")
            with edge.task_lock(directory):
                pass

    def test_duplicate_camera_identity_rejected(self):
        for field in ("serial", "camera_number", "camera_channel_id"):
            value = config()
            value["cameras"][1][field] = value["cameras"][0][field]
            with self.assertRaises(ValueError):
                edge.validate_config(value)

    def test_http_requires_an_explicit_boolean(self):
        value = config()
        value["allow_http"] = "false"
        with self.assertRaisesRegex(ValueError, "boolean"):
            edge.validate_config(value)

    def test_failed_recorder_never_prepares_an_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config_path = directory / "config.json"
            edge.atomic_json(config_path, config())
            with patch("sys.argv", ["edge.py", "record", "--config", str(config_path), "--spool", str(directory / "spool")]), \
                    patch.object(edge.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "recorder")), \
                    patch.object(edge, "prepare") as prepare, patch("sys.stdout", new=io.StringIO()), patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(1, edge.main())
                prepare.assert_not_called()
            tasks = list((directory / "spool").iterdir())
            self.assertEqual(1, len(tasks))
            self.assertTrue((tasks[0] / "task.json").is_file())
            self.assertFalse((tasks[0] / "upload-state.json").exists())

    def test_transport_does_not_leak_tokens_or_signed_urls(self):
        api = edge.Platform(config(), "private-token")
        with patch.object(api, "_send", return_value=b"") as send:
            api.put_part("http://storage.invalid/part?secret=private-signature", b"part")
            self.assertNotIn("Authorization", send.call_args.args[0].headers)
        error = urllib.error.HTTPError("http://storage.invalid/?secret=private-signature", 403, "secret", {}, io.BytesIO(b"private-signature"))
        with patch.object(api.opener, "open", side_effect=error):
            with self.assertRaises(edge.ApiError) as raised:
                api.put_part("http://storage.invalid/?secret=private-signature", b"part")
        self.assertNotIn("private", str(raised.exception))
        self.assertIsNone(edge.NoRedirect().redirect_request(None, None, 302, "", {}, "http://other.invalid"))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/ffprobe required for media test")
class PreparationTests(unittest.TestCase):
    def test_real_transcode_and_stable_two_camera_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            recording = directory / "recording"
            recording.mkdir()
            value = config()
            timestamp = edge.now()
            group = str(uuid.uuid4())
            edge.atomic_json(directory / "task.json", {"config": value, "capture_group_key": group,
                             "capture_session_id": str(uuid.uuid4()), "captured_at": timestamp})
            videos = []
            for camera in value["cameras"]:
                name = f"camera_{camera['camera_number']}.raw.mp4"
                subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                                "testsrc2=size=320x240:rate=30", "-t", "0.3", "-c:v", "mpeg4", str(recording / name)], check=True)
                videos.append({"camera_number": camera["camera_number"], "serial": camera["serial"],
                               "file_name": name, "record_start_at": timestamp, "frames": 9})
            edge.atomic_json(recording / "capture-result.json", {"schema_version": "camera-recording-v1", "videos": videos})
            first = edge.prepare(directory)
            self.assertEqual(first, edge.prepare(directory))
            self.assertEqual([1, 2], first["external_manifest"]["capture_group"]["expected_camera_numbers"])
            self.assertEqual(3, len(first["files"]))
            self.assertFalse(first["external_manifest"]["upload_set_finalized"])
            metadata = edge.read_json(directory / "metadata.json")
            self.assertEqual([0, 1], [stream["camera_index"] for stream in metadata["streams"]])
            self.assertTrue(all(stream["sync_status"] == "UNKNOWN" for stream in metadata["streams"]))
            probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(directory / "camera_1.mp4")]))
            self.assertEqual("h264", probe["streams"][0]["codec_name"])
            self.assertEqual("yuv420p", probe["streams"][0]["pix_fmt"])

    def test_missing_camera_never_generates_upload_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "recording").mkdir()
            edge.atomic_json(directory / "task.json", {"config": config()})
            edge.atomic_json(directory / "recording" / "capture-result.json", {"schema_version": "camera-recording-v1", "videos": []})
            with self.assertRaisesRegex(ValueError, "Both configured"):
                edge.prepare(directory)
            self.assertFalse((directory / "upload-state.json").exists())


if __name__ == "__main__":
    unittest.main()
