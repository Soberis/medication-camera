import glob
import os
import struct
import threading
import time
import uuid
from typing import Callable, Dict, Any, Optional

from .base import BaseTrigger, TriggerEvent


class UsbHidTrigger(BaseTrigger):
    """USB 免驱医疗脚踏板 / 物理大按键触发器 (HID 输入事件)"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.device_path = self.config.get("device_path")
        self.target_key = self.config.get("key_code", 57)  # 57 = KEY_SPACE
        self.debounce_ms = self.config.get("debounce_ms", 100)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_press_time = 0.0

    def start_listening(
        self,
        on_start: Callable[[TriggerEvent], None],
        on_stop: Callable[[TriggerEvent], None]
    ) -> None:
        super().start_listening(on_start, on_stop)
        self._running = True
        self._thread = threading.Thread(target=self._hid_worker, daemon=True)
        self._thread.start()

    def stop_listening(self) -> None:
        super().stop_listening()
        self._running = False
        if self.is_recording:
            self.notify_stop(reason="SHUTDOWN")
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def trigger_key_press(self, key_code: Optional[int] = None) -> None:
        """按键按下事件处理 (带防抖与状态翻转)"""
        now = time.time()
        if (now - self._last_press_time) * 1000 < self.debounce_ms:
            return
        self._last_press_time = now

        if not self.is_recording:
            session_id = f"session_pedal_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
            print(f"\n[TRIGGER-HID] [START] 检测到脚踏板/按键按下 -> 启动录像: {session_id}")
            self.notify_start(session_id, {"source": "usb_hid", "key_code": key_code or self.target_key})
        else:
            print(f"\n[TRIGGER-HID] [STOP] 检测到脚踏板/按键再次按下 -> 停止录像")
            self.notify_stop(reason="PEDAL_PRESSED")

    def _find_input_device(self) -> Optional[str]:
        if self.device_path and os.path.exists(self.device_path):
            return self.device_path
        # 自动扫描 /dev/input/by-id/ 或 /dev/input/event*
        by_id = glob.glob("/dev/input/by-id/*kbd*") or glob.glob("/dev/input/by-id/*")
        if by_id:
            return by_id[0]
        events = glob.glob("/dev/input/event*")
        return events[0] if events else None

    def _hid_worker(self) -> None:
        if os.name == "nt":
            # Windows 开发测试模拟
            while self._running:
                time.sleep(0.5)
            return

        dev_node = self._find_input_device()
        if not dev_node:
            print("[TRIGGER-HID] [WARN] 未找到可用 /dev/input 设备，进入模拟监听", flush=True)
            while self._running:
                time.sleep(1)
            return

        print(f"[TRIGGER-HID] [INFO] 正在监听 USB 输入设备: {dev_node}")
        # Linux input_event struct 格式: time(16B) + type(2B) + code(2B) + value(4B) = 24B (64位)
        event_format = "qqHHI"
        event_size = struct.calcsize(event_format)

        try:
            with open(dev_node, "rb") as f:
                while self._running:
                    data = f.read(event_size)
                    if not data or len(data) < event_size:
                        continue
                    tv_sec, tv_usec, ev_type, ev_code, ev_value = struct.unpack(event_format, data)
                    # ev_type == 1 (EV_KEY), ev_value == 1 (按下)
                    if ev_type == 1 and ev_value == 1:
                        if self.target_key is None or ev_code == self.target_key:
                            self.trigger_key_press(ev_code)
        except Exception as e:
            print(f"[TRIGGER-HID] [ERROR] 读取输入设备出错: {e}", flush=True)
