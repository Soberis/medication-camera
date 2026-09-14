#!/usr/bin/env python3
"""RK3588 recorder launcher and resumable platform V2 uploader (stdlib only)."""
import argparse
import contextlib
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

PART_SIZE = 8 * 1024 * 1024
ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def read_json(path):
    with Path(path).open(encoding="utf-8-sig") as source:
        return json.load(source)


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextlib.contextmanager
def task_lock(directory):
    # OS locks are released on process death; the file itself is never a stale lock.
    with (Path(directory) / ".lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("Task is in use by another process") from None
        else:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("Task is in use by another process") from None
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def require_id(value, label):
    if not isinstance(value, str) or not re.fullmatch(ID_PATTERN, value):
        raise ValueError(f"Invalid {label}")
    return value


def require_uuid(value, label):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"Configure a lowercase UUID for {label}") from None
    return value


def validate_config(config, provisioned=True):
    if type(config.get("allow_http", False)) is not bool:
        raise ValueError("allow_http must be a JSON boolean")
    require_id(config["device_id"], "device_id")
    require_id(config["source_system"], "source_system")
    require_id(config["camera_layout_version"], "camera_layout_version")
    require_uuid(config["project_id"], "project_id")
    if config.get("timezone") != "Asia/Shanghai":
        raise ValueError("This deployment requires timezone Asia/Shanghai")
    duration = config["duration_seconds"]
    if type(duration) is not int or not 1 <= duration <= 3600:
        raise ValueError("duration_seconds must be an integer in 1..3600")
    cameras = config["cameras"]
    if not isinstance(cameras, list) or len(cameras) != 2:
        raise ValueError("Configure exactly two cameras")
    numbers, serials, channels = set(), set(), set()
    for camera in cameras:
        number = camera["camera_number"]
        serial = require_id(camera["serial"], "serial")
        if type(number) is not int or not 1 <= number <= 64 or number in numbers or serial in serials:
            raise ValueError("Camera numbers and serials must be unique; numbers must be 1..64")
        numbers.add(number)
        serials.add(serial)
        if provisioned:
            channel = require_uuid(camera["camera_channel_id"], "camera_channel_id")
            if channel in channels:
                raise ValueError("Camera channel IDs must be unique")
            channels.add(channel)
    if provisioned:
        require_uuid(config["camera_set_id"], "camera_set_id")
    validate_url(config["platform_base_url"], config.get("allow_http", False), base=True)
    return config


def validate_url(value, allow_http=False, base=False):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ({"https", "http"} if allow_http else {"https"})
            or not parsed.hostname or parsed.username or parsed.password or parsed.fragment
            or (base and (parsed.query or parsed.path not in ("", "/")))):
        raise ValueError("Invalid URL; use an origin without credentials (HTTP requires allow_http=true)")
    return value.rstrip("/") if base else value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ApiError(RuntimeError):
    pass


class Platform:
    def __init__(self, config, token):
        self.allow_http = config.get("allow_http", False)
        self.base = validate_url(config["platform_base_url"], self.allow_http, base=True)
        if not token or "\n" in token or "\r" in token:
            raise ValueError("Set PLATFORM_TOKEN (or PLATFORM_ADMIN_TOKEN for provision) in the environment")
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, body=None, key=None):
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":")).encode()
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(self.base + "/api/v1" + path, data=data, headers=headers, method=method)
        raw = self._send(request, control=True)
        try:
            return json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            raise ApiError("Platform returned invalid JSON") from None

    def put_part(self, url, data):
        validate_url(url, self.allow_http)
        # Never send the platform bearer token to object storage.
        self._send(urllib.request.Request(url, data=data, method="PUT",
                                         headers={"Content-Length": str(len(data))}), control=False)

    def _send(self, request, control):
        try:
            with self.opener.open(request, timeout=120) as response:
                return response.read(4 * 1024 * 1024) if control else b""
        except urllib.error.HTTPError as error:
            code = "HTTP_ERROR"
            if control:
                try:
                    candidate = json.loads(error.read(8192)).get("error_code", "HTTP_ERROR")
                    if isinstance(candidate, str) and re.fullmatch(r"[A-Z0-9_]{1,100}", candidate):
                        code = candidate
                except (ValueError, UnicodeError, AttributeError):
                    pass
            raise ApiError(f"{'Platform' if control else 'Object storage'} HTTP {error.code}: {code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            # Exception messages can contain signed URLs; do not print them.
            raise ApiError("Network request failed; retain this task and retry upload") from None


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(PART_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_entry(directory, name, kind, content_type):
    path = Path(directory) / name
    size = path.stat().st_size
    if size <= 0:
        raise ValueError(f"Empty file: {name}")
    return {"client_file_id": str(uuid.uuid4()), "file_name": name, "file_kind": kind,
            "content_type": content_type, "size_bytes": size, "sha256": file_hash(path),
            "part_size_bytes": PART_SIZE}


def manifest_hash(files):
    lines = ["upload-file-manifest-v1\n"]
    for file in sorted(files, key=lambda item: item["client_file_id"]):
        lines.append("\t".join(str(file[key]) for key in (
            "client_file_id", "file_kind", "content_type", "size_bytes", "sha256", "part_size_bytes")) + "\n")
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def verify_files(directory, files):
    for file in files:
        name = file["file_name"]
        path = Path(directory) / name
        if Path(name).name != name or path.is_symlink() or not path.is_file():
            raise ValueError("Task contains an unsafe or missing file")
        if path.stat().st_size != file["size_bytes"] or file_hash(path) != file["sha256"]:
            raise ValueError(f"Task file changed; refusing upload: {name}")


def prepare(directory, ffmpeg="ffmpeg"):
    directory = Path(directory)
    state_path = directory / "upload-state.json"
    if state_path.exists():
        state = read_json(state_path)
        verify_files(directory, state["files"])
        return state
    task = read_json(directory / "task.json")
    config = validate_config(task["config"])
    result = read_json(directory / "recording" / "capture-result.json")
    if result["schema_version"] != "camera-recording-v1":
        raise ValueError("Unsupported recording result")
    cameras = sorted(config["cameras"], key=lambda c: c["camera_number"])
    recordings = {item["camera_number"]: item for item in result["videos"]}
    if len(result["videos"]) != len(cameras) or set(recordings) != {c["camera_number"] for c in cameras}:
        raise ValueError("Both configured recordings must succeed before preparing this task")
    videos, streams, declarations = [], [], []
    for camera in cameras:
        number = camera["camera_number"]
        item = recordings[number]
        raw_name = f"camera_{number}.raw.mp4"
        if item["serial"] != camera["serial"] or item["file_name"] != raw_name or item["frames"] <= 0:
            raise ValueError("Recording identity or frame count does not match config")
        name = f"camera_{number}.mp4"
        temporary = directory / f"camera_{number}.encoding.mp4"
        # Preserve raw evidence. H.264/yuv420p output is suitable for browser playback.
        command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-y",
                   "-i", str(directory / "recording" / raw_name), "-map", "0:v:0", "-an",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                   "-movflags", "+faststart", str(temporary)]
        subprocess.run(command, check=True, timeout=max(120, config["duration_seconds"] * 12))
        os.replace(temporary, directory / name)
        file = file_entry(directory, name, "VIDEO", "video/mp4")
        videos.append(file)
        streams.append({"client_file_id": file["client_file_id"], "camera_channel_id": camera["camera_channel_id"],
                        "camera_index": number - 1, "enabled": True, "record_start_utc": item["record_start_at"],
                        "time_offset_ms": 0, "sync_status": "UNKNOWN"})
        declarations.append({"client_file_id": file["client_file_id"], "camera_number": number,
                             "file_name": name, "declared_size_bytes": file["size_bytes"],
                             "sha256": file["sha256"], "record_start_at": item["record_start_at"]})
    group = task["capture_group_key"]
    metadata = {"schema_version": "capture-session-metadata-v1", "capture_session_id": task["capture_session_id"],
                "device_id": config["device_id"], "camera_set_id": config["camera_set_id"],
                "expected_camera_count": len(cameras), "camera_layout_version": config["camera_layout_version"],
                "sync_group_id": group, "device_time_utc": task["captured_at"],
                "device_timezone": config["timezone"], "streams": streams}
    atomic_json(directory / "metadata.json", metadata)
    files = videos + [file_entry(directory, "metadata.json", "METADATA", "application/json")]
    manifest = {"schema_version": "external-video-ingest-v2", "source_system": config["source_system"],
                "source_device_id": config["device_id"], "source_batch_id": group,
                "capture_group": {"capture_group_key": group, "key_source": "UPSTREAM_MANIFEST_KEY",
                                  "captured_at": task["captured_at"], "timezone": config["timezone"],
                                  "expected_camera_numbers": [c["camera_number"] for c in cameras]},
                "videos": declarations, "upload_set_finalized": False}
    atomic_json(directory / "manifest.json", manifest)
    state = {"schema_version": "camera-upload-state-v1", "config": config, "files": files,
             "external_manifest": manifest, "manifest_sha256": manifest_hash(files),
             "init_key": "camera-batch-" + group, "status": "READY"}
    atomic_json(state_path, state)
    return state


def upload(directory, api=None):
    directory = Path(directory)
    path = directory / "upload-state.json"
    state = read_json(path)
    if state["schema_version"] != "camera-upload-state-v1":
        raise ValueError("Unsupported upload state")
    config = validate_config(state["config"])
    verify_files(directory, state["files"])
    if manifest_hash(state["files"]) != state["manifest_sha256"]:
        raise ValueError("File manifest changed")
    api = api or Platform(config, os.environ.get("PLATFORM_TOKEN"))
    project = config["project_id"]

    def save():
        atomic_json(path, state)

    if "manifest_id" not in state:
        accepted = api.request("POST", f"/projects/{project}/external-video-ingest-manifests/v2",
                               state["external_manifest"])
        state["manifest_id"] = require_uuid(accepted["manifest_id"], "manifest_id")
        save()
    if "batch_id" not in state:
        batch = api.request("POST", "/upload/init", {
            "project_id": project, "device_id": config["device_id"], "upload_source_type": "HOSPITAL_REMOTE",
            "file_count": len(state["files"]), "total_size_bytes": sum(f["size_bytes"] for f in state["files"]),
            "manifest_sha256": state["manifest_sha256"], "external_manifest_id": state["manifest_id"]}, state["init_key"])
        state["batch_id"] = require_uuid(batch["batch_id"], "batch_id")
        state["status"] = "UPLOADING"
        save()
    batch_path = "/upload/" + state["batch_id"]
    batch = api.request("GET", batch_path)
    if batch["status"] in {"FAILED", "ABORTED", "EXPIRED"}:
        raise ApiError("Batch is terminal: " + batch["status"] + "; retain files for operator recovery")
    if batch["status"] != "COMPLETED":
        registered = {f["client_file_id"]: f for f in api.request("GET", batch_path + "/files")["files"]}
        for file in state["files"]:
            file_id = file["client_file_id"]
            if file_id not in registered:
                body = {key: file[key] for key in (
                    "file_kind", "content_type", "size_bytes", "sha256", "part_size_bytes", "file_name")}
                registered[file_id] = api.request("PUT", batch_path + "/files/" + file_id, body, "camera-file-" + file_id)
        for file in state["files"]:
            file_path = batch_path + "/files/" + file["client_file_id"]
            remote = registered[file["client_file_id"]]
            if remote["status"] in {"FAILED", "ABORTED", "EXPIRED"}:
                raise ApiError("File is terminal; retain task for operator recovery")
            if remote["status"] not in {"UPLOADED", "COMPLETING"}:
                if remote["status"] == "REGISTERED":
                    api.request("PUT", file_path + "/multipart")
                # The platform only permits reconciliation after the first authorization.
                parts = (api.request("PUT", file_path + "/parts/reconciliation")["parts"]
                         if remote["status"] == "UPLOADING" else [])
                confirmed = {part["part_number"]: part for part in parts if part["status"] == "UPLOADED"}
                with (directory / file["file_name"]).open("rb") as source:
                    part_number = 0
                    for data in iter(lambda: source.read(file["part_size_bytes"]), b""):
                        part_number += 1
                        digest = hashlib.sha256(data).hexdigest()
                        if part_number in confirmed:
                            part = confirmed[part_number]
                            if part["sha256"] != digest or part["expected_size_bytes"] != len(data):
                                raise ValueError("Server part differs from local file")
                            continue
                        authorization = api.request("POST", f"{file_path}/parts/{part_number}/authorization", {"sha256": digest})
                        if authorization["expected_size_bytes"] != len(data) or authorization["sha256"] != digest:
                            raise ValueError("Server part authorization differs from local file")
                        api.put_part(authorization["upload_url"], data)
                        api.request("PUT", f"{file_path}/parts/{part_number}/confirmation")
                        print(f"Uploaded {file['file_name']} part {part_number}", flush=True)
            completed = api.request("PUT", file_path + "/completion")
            if completed["verified_sha256"] != file["sha256"]:
                raise ValueError("Server file digest does not match local file")
    completion = api.request("PUT", batch_path + "/completion")
    if completion["verified_manifest_sha256"] != state["manifest_sha256"]:
        raise ValueError("Server batch manifest digest mismatch")
    expected_files = {file["client_file_id"] for file in state["files"] if file["file_kind"] == "VIDEO"}
    if ({asset["client_file_id"] for asset in completion["clip_assets"]} != expected_files
            or len(completion["clip_assets"]) != len(expected_files)):
        raise ValueError("Platform did not bind exactly the declared videos")
    session = api.request("PUT", batch_path + "/capture-session")
    group_id = require_uuid(session.get("capture_group_id", session["capture_session_id"]), "capture_group_id")
    state["capture_session_id"] = session["capture_session_id"]
    state["capture_group_id"] = group_id
    state["clip_asset_ids"] = [asset["clip_asset_id"] for asset in completion["clip_assets"]]
    state["status"] = "ASSEMBLED"
    save()
    receipt = api.request("POST", f"/projects/{project}/capture-groups/{group_id}/receipt/finalize")
    expected_numbers = sorted(camera["camera_number"] for camera in config["cameras"])
    if (receipt["receipt_state"] != "COMPLETE" or receipt["expected_camera_numbers"] != expected_numbers
            or sorted(receipt["received_camera_numbers"]) != expected_numbers or receipt["missing_camera_numbers"]):
        raise ApiError("Platform receipt is incomplete; retain the task and inspect the CaptureGroup")
    state["status"] = "UPLOADED"
    save()
    print(f"Upload complete: batch={state['batch_id']} capture_group={group_id}")
    print("Media processing/playback must still be checked in the platform.")
    return state


def status(directory, api=None):
    state = read_json(Path(directory) / "upload-state.json")
    config = validate_config(state["config"])
    if "capture_group_id" not in state:
        print(f"Local status: {state['status']}; run upload to create/assemble the CaptureGroup")
        return False
    api = api or Platform(config, os.environ.get("PLATFORM_TOKEN"))
    group = api.request("GET", f"/projects/{config['project_id']}/capture-groups/{state['capture_group_id']}")
    print(f"CaptureGroup {state['capture_group_id']}: receipt={group['receipt_state']}")
    for stream in group["streams"]:
        print(f"Camera {stream['camera_number']}: {stream['stream_status']} clip_asset={stream['clip_asset_id']}")
    return group["receipt_state"] == "COMPLETE" and len(group["streams"]) == len(config["cameras"]) and all(
        stream["stream_status"] == "INGESTED" for stream in group["streams"])


def provision(config_path):
    config = validate_config(read_json(config_path), provisioned=False)
    api = Platform(config, os.environ.get("PLATFORM_ADMIN_TOKEN"))
    project = config["project_id"]
    key = require_id(config["camera_set_key"], "camera_set_key")
    version = config["configuration_version"]
    if type(version) is not int or version < 1:
        raise ValueError("configuration_version must be a positive integer")
    members = []
    for camera in config["cameras"]:
        provisioning_key = require_id(camera["provisioning_key"], "provisioning_key")
        channel = api.request("PUT", f"/projects/{project}/camera-channels/{provisioning_key}")
        camera["camera_channel_id"] = channel["camera_channel_id"]
        members.append({"camera_channel_id": channel["camera_channel_id"], "camera_index": camera["camera_number"] - 1,
                        "camera_role": camera["camera_role"], "mount_position": camera["mount_position"], "enabled": True})
    camera_set = api.request("PUT", f"/projects/{project}/camera-sets/{key}/versions/{version}",
                             {"layout_version": config["camera_layout_version"], "members": members})
    config["camera_set_id"] = camera_set["camera_set_id"]
    validate_config(config)
    atomic_json(config_path, config)
    print("CameraSet configured; generated IDs saved in local config (no credentials saved).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("provision", "record"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        if name == "record":
            command.add_argument("--recorder", default="build/medication_camera", type=Path)
            command.add_argument("--spool", default="spool", type=Path)
            command.add_argument("--ffmpeg", default="ffmpeg")
    for name in ("prepare", "upload", "status"):
        command = commands.add_parser(name)
        command.add_argument("task", type=Path)
        if name == "prepare":
            command.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    try:
        if args.command == "provision":
            provision(args.config)
        elif args.command == "record":
            config = validate_config(read_json(args.config))
            group = str(uuid.uuid4())
            directory = args.spool.resolve() / group
            directory.mkdir(parents=True, exist_ok=False)
            with task_lock(directory):
                atomic_json(directory / "task.json", {"config": config, "capture_group_key": group,
                            "capture_session_id": str(uuid.uuid4()), "captured_at": now()})
                # OpenCV FileStorage only needs numeric/string recording settings.
                atomic_json(directory / "recording-config.json", {
                    "duration_seconds": config["duration_seconds"],
                    "cameras": [{"camera_number": c["camera_number"], "serial": c["serial"]}
                                for c in config["cameras"]]})
                print(f"Task: {directory}", flush=True)
                subprocess.run([str(args.recorder.resolve()), str(directory / "recording-config.json"),
                                str(directory / "recording")], check=True, timeout=config["duration_seconds"] + 120)
                prepare(directory, args.ffmpeg)
                print(f"Ready. Upload with: python3 tools/edge.py upload {directory}")
        else:
            with task_lock(args.task):
                if args.command == "prepare":
                    prepare(args.task, args.ffmpeg)
                    print("Task ready for upload")
                elif args.command == "upload":
                    upload(args.task)
                else:
                    return 0 if status(args.task) else 2
        return 0
    except (ApiError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        # Do not dump a config or transport exception traceback containing credentials.
        print(f"Failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
