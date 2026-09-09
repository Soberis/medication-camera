import abc
import datetime as dt
import threading
from typing import Any, Callable, Dict, Optional


class TriggerEvent:
    START = "START"
    STOP = "STOP"

    def __init__(self, action: str, metadata: Optional[Dict[str, Any]] = None):
        self.action = action
        self.metadata = metadata or {}
        self.timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class BaseTrigger(abc.ABC):
    """触发器抽象基类"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.max_duration_seconds = self.config.get("max_duration_seconds", 300)
        self.is_recording = False
        self.current_session_id = None
        self.session_started_at = None
        self.session_metadata = {}

        self._on_start_cb: Optional[Callable[[TriggerEvent], None]] = None
        self._on_stop_cb: Optional[Callable[[TriggerEvent], None]] = None
        self._safety_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    @abc.abstractmethod
    def start_listening(
        self,
        on_start: Callable[[TriggerEvent], None],
        on_stop: Callable[[TriggerEvent], None]
    ) -> None:
        """启动事件监听"""
        self._on_start_cb = on_start
        self._on_stop_cb = on_stop

    @abc.abstractmethod
    def stop_listening(self) -> None:
        """停止事件监听并释放资源"""
        self._cancel_safety_timer()

    def notify_start(self, session_id: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """触发开始录像事件 (带并发加锁与防呆超时启动)"""
        with self._lock:
            if self.is_recording:
                return False
            self.is_recording = True
            self.current_session_id = session_id
            self.session_started_at = dt.datetime.now(dt.timezone.utc)
            self.session_metadata = metadata or {}

            # 启动防呆最大超时定时器 (例如 5 分钟忘记按停，自动截断保存)
            self._cancel_safety_timer()
            if self.max_duration_seconds > 0:
                self._safety_timer = threading.Timer(
                    self.max_duration_seconds,
                    self._handle_safety_timeout
                )
                self._safety_timer.daemon = True
                self._safety_timer.start()

        event = TriggerEvent(TriggerEvent.START, {
            "session_id": session_id,
            **(metadata or {})
        })
        if self._on_start_cb:
            self._on_start_cb(event)
        return True

    def notify_stop(self, reason: str = "MANUAL") -> bool:
        """触发停止录像事件"""
        with self._lock:
            if not self.is_recording:
                return False
            self.is_recording = False
            self._cancel_safety_timer()
            session_id = self.current_session_id
            started_at = self.session_started_at
            meta = dict(self.session_metadata)
            self.current_session_id = None
            self.session_started_at = None
            self.session_metadata = {}

        elapsed = (dt.datetime.now(dt.timezone.utc) - started_at).total_seconds() if started_at else 0.0
        event = TriggerEvent(TriggerEvent.STOP, {
            "session_id": session_id,
            "reason": reason,
            "elapsed_seconds": elapsed,
            **meta
        })
        if self._on_stop_cb:
            self._on_stop_cb(event)
        return True

    def _handle_safety_timeout(self) -> None:
        """防呆超时强制停止"""
        print(f"\n[TRIGGER] [WARN] 达到最大安全保护时长 ({self.max_duration_seconds}秒)，执行超时自动切断并保存！")
        self.notify_stop(reason="SAFETY_TIMEOUT")

    def _cancel_safety_timer(self) -> None:
        if self._safety_timer:
            self._safety_timer.cancel()
            self._safety_timer = None

    def status(self) -> Dict[str, Any]:
        """返回当前触发器状态"""
        with self._lock:
            elapsed = 0.0
            if self.is_recording and self.session_started_at:
                elapsed = (dt.datetime.now(dt.timezone.utc) - self.session_started_at).total_seconds()
            return {
                "is_recording": self.is_recording,
                "current_session_id": self.current_session_id,
                "elapsed_seconds": round(elapsed, 1),
                "max_duration_seconds": self.max_duration_seconds,
                "metadata": dict(self.session_metadata)
            }
