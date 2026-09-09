from typing import Dict, Any, Optional

from .base import BaseTrigger
from .loop import LoopTrigger
from .gpio import GpioTrigger
from .usb_hid import UsbHidTrigger
from .http import HttpTrigger


class TriggerManager:
    """触发器工厂与管理器"""

    @staticmethod
    def create(config: Optional[Dict[str, Any]] = None) -> BaseTrigger:
        config = config or {}
        trigger_cfg = config.get("trigger", {})
        mode = trigger_cfg.get("mode", "loop").lower()

        merged_cfg = {**trigger_cfg}
        if "duration_seconds" in config and "interval_seconds" not in merged_cfg:
            merged_cfg["interval_seconds"] = config["duration_seconds"]

        if mode == "loop":
            return LoopTrigger(merged_cfg)
        elif mode == "gpio":
            gpio_opts = {**merged_cfg, **trigger_cfg.get("gpio", {})}
            return GpioTrigger(gpio_opts)
        elif mode in ("usb_hid", "hid", "pedal", "button"):
            hid_opts = {**merged_cfg, **trigger_cfg.get("usb_hid", {})}
            return UsbHidTrigger(hid_opts)
        elif mode in ("http", "rest", "api"):
            http_opts = {**merged_cfg, **trigger_cfg.get("http", {})}
            return HttpTrigger(http_opts)
        else:
            print(f"[TRIGGER] [WARN] 未知触发模式 '{mode}'，回退至 loop 循环模式")
            return LoopTrigger(merged_cfg)
