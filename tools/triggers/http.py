from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import threading
import time
import urllib.parse
import uuid
from typing import Callable, Dict, Any, Optional

from .base import BaseTrigger, TriggerEvent


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


class HttpTrigger(BaseTrigger):
    """本地 REST HTTP 控制触发器 (支持 curl / 平板 / 扫码枪触发)"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.host = self.config.get("host", "0.0.0.0")
        self.port = self.config.get("port", 8090)
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start_listening(
        self,
        on_start: Callable[[TriggerEvent], None],
        on_stop: Callable[[TriggerEvent], None]
    ) -> None:
        super().start_listening(on_start, on_stop)
        trigger_ref = self

        class TriggerHttpHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                # 抑制标准输出刷屏日志
                pass

            def _send_json(self, status_code: int, data: Dict[str, Any]):
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                self.close_connection = True

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path.rstrip("/")

                if path == "/api/trigger/status":
                    self._send_json(200, trigger_ref.status())

                elif path in ("/api/trigger/start", "/start"):
                    params = urllib.parse.parse_qs(parsed.query)
                    meta = {k: v[0] for k, v in params.items()}
                    session_id = meta.pop("session_id", None) or f"session_http_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
                    ok = trigger_ref.notify_start(session_id, meta)
                    if ok:
                        self._send_json(200, {"status": "OK", "session_id": session_id, "state": "RECORDING"})
                    else:
                        self._send_json(409, {"status": "ERROR", "message": "录像已在进行中", "state": "RECORDING"})

                elif path in ("/api/trigger/stop", "/stop"):
                    ok = trigger_ref.notify_stop(reason="HTTP_API")
                    if ok:
                        self._send_json(200, {"status": "OK", "state": "STOPPING"})
                    else:
                        self._send_json(409, {"status": "ERROR", "message": "当前没有录像在进行", "state": "IDLE"})

                else:
                    self._send_json(404, {"error": "Not Found", "endpoints": ["/api/trigger/start", "/api/trigger/stop", "/api/trigger/status"]})

            def do_POST(self):
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path.rstrip("/")
                body = {}
                length = int(self.headers.get("Content-Length", 0))
                if length > 0:
                    try:
                        raw = self.rfile.read(length).decode("utf-8")
                        body = json.loads(raw)
                    except Exception:
                        pass

                if path in ("/api/trigger/start", "/start"):
                    session_id = body.pop("session_id", None) or f"session_http_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
                    ok = trigger_ref.notify_start(session_id, body)
                    if ok:
                        self._send_json(200, {"status": "OK", "session_id": session_id, "state": "RECORDING"})
                    else:
                        self._send_json(409, {"status": "ERROR", "message": "录像已在进行中"})

                elif path in ("/api/trigger/stop", "/stop"):
                    ok = trigger_ref.notify_stop(reason="HTTP_API")
                    if ok:
                        self._send_json(200, {"status": "OK", "state": "STOPPING"})
                    else:
                        self._send_json(409, {"status": "ERROR", "message": "当前没有录像在进行"})

                else:
                    self._send_json(404, {"error": "Not Found"})

        try:
            self._server = ReusableHTTPServer((self.host, self.port), TriggerHttpHandler)
            self.port = self._server.server_port
            print(f"[TRIGGER-HTTP] [INFO] HTTP 控制接口已就绪: http://{self.host}:{self.port}")
            print(f"               * 开始录像: curl http://localhost:{self.port}/api/trigger/start")
            print(f"               * 停止录像: curl http://localhost:{self.port}/api/trigger/stop")
            print(f"               * 状态查询: curl http://localhost:{self.port}/api/trigger/status")
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
        except Exception as e:
            print(f"[TRIGGER-HTTP] [ERROR] 无法启动 HTTP 服务 (端口 {self.port}): {e}")

    def stop_listening(self) -> None:
        super().stop_listening()
        if self._server:
            try:
                server = self._server
                self._server = None
                threading.Thread(target=server.shutdown, daemon=True).start()
                server.server_close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)
