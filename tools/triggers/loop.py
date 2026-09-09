import threading
import time
import uuid
from typing import Callable, Dict, Any, Optional

from .base import BaseTrigger, TriggerEvent


class LoopTrigger(BaseTrigger):
    """循环切片定时触发器 (对应连续录制模式)"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.interval_seconds = self.config.get("interval_seconds", 180)
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start_listening(
        self,
        on_start: Callable[[TriggerEvent], None],
        on_stop: Callable[[TriggerEvent], None]
    ) -> None:
        super().start_listening(on_start, on_stop)
        self._running = True
        self._thread = threading.Thread(target=self._loop_worker, daemon=True)
        self._thread.start()

    def stop_listening(self) -> None:
        super().stop_listening()
        self._running = False
        if self.is_recording:
            self.notify_stop(reason="SHUTDOWN")
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _loop_worker(self) -> None:
        while self._running:
            session_id = f"segment_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
            self.notify_start(session_id, {"mode": "loop", "duration": self.interval_seconds})

            # 等待设定的切片时长，支持提前退出
            sleep_chunks = int(self.interval_seconds * 10)
            for _ in range(sleep_chunks):
                if not self._running:
                    break
                time.sleep(0.1)

            if self._running:
                self.notify_stop(reason="INTERVAL_COMPLETED")
