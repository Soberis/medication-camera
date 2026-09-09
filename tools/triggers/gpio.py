import os
import sys
import threading
import time
import uuid
from typing import Callable, Dict, Any, Optional

from .base import BaseTrigger, TriggerEvent


class GpioTrigger(BaseTrigger):
    """RK3588 板载 GPIO 引脚物理按键触发器"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.chip_num = self.config.get("chip", 1)
        self.line_num = self.config.get("line", 12)
        self.active_low = self.config.get("active_low", True)
        self.debounce_ms = self.config.get("debounce_ms", 50)
        self.toggle_mode = self.config.get("toggle_mode", True)  # True: 按一下开/按一下关; False: 按住录/松开停

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_edge_time = 0.0
        self._is_mock = (os.name == "nt") or not os.path.exists(f"/dev/gpiochip{self.chip_num}")

    def start_listening(
        self,
        on_start: Callable[[TriggerEvent], None],
        on_stop: Callable[[TriggerEvent], None]
    ) -> None:
        super().start_listening(on_start, on_stop)
        self._running = True

        if self._is_mock:
            print(f"[TRIGGER-GPIO] [INFO] 未检测到物理 /dev/gpiochip{self.chip_num}，启动模拟监听模式。")
            self._thread = threading.Thread(target=self._mock_worker, daemon=True)
        else:
            print(f"[TRIGGER-GPIO] [INFO] 正在监听 RK3588 物理引脚: chip={self.chip_num}, line={self.line_num}")
            self._thread = threading.Thread(target=self._gpio_worker, daemon=True)

        self._thread.start()

    def stop_listening(self) -> None:
        super().stop_listening()
        self._running = False
        if self.is_recording:
            self.notify_stop(reason="SHUTDOWN")
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def trigger_button_press(self) -> None:
        """供模拟或外部调用模拟物理按键按下"""
        now = time.time()
        if (now - self._last_edge_time) * 1000 < self.debounce_ms:
            # 防抖忽略
            return
        self._last_edge_time = now

        if not self.is_recording:
            session_id = f"session_btn_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
            print(f"\n[TRIGGER-GPIO] [START] 检测到按键按下 -> 启动录像: {session_id}")
            self.notify_start(session_id, {"source": "gpio", "pin": self.line_num})
        else:
            print(f"\n[TRIGGER-GPIO] [STOP] 检测到按键再次按下 -> 停止录像")
            self.notify_stop(reason="BUTTON_PRESSED")

    def _mock_worker(self) -> None:
        """模拟轮询空转"""
        while self._running:
            time.sleep(0.5)

    def _gpio_worker(self) -> None:
        """Linux 物理 GPIO 真实监听"""
        try:
            # 优先尝试 libgpiod
            import gpiod
            chip = gpiod.Chip(f"/dev/gpiochip{self.chip_num}")
            line = chip.get_line(self.line_num)
            edge_flag = gpiod.LINE_REQ_EV_BOTH_EDGES if not self.toggle_mode else (
                gpiod.LINE_REQ_EV_FALLING_EDGE if self.active_low else gpiod.LINE_REQ_EV_RISING_EDGE
            )
            line.request(consumer="medication-camera-trigger", type=edge_flag)

            while self._running:
                if line.event_wait(sec=1):
                    line.event_read()
                    self.trigger_button_press()
        except ImportError:
            print("[TRIGGER-GPIO] [WARN] 未找到 python3-libgpiod，尝试退避轮询", file=sys.stderr)
            while self._running:
                time.sleep(1)
        except Exception as e:
            print(f"[TRIGGER-GPIO] [ERROR] GPIO 监听异常: {e}", file=sys.stderr)
