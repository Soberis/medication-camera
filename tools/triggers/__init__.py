"""
medication-camera 触发器子系统 (Trigger Subsystem)

提供可插拔的录像触发抽象：
  - LoopTrigger:    定长切片循环触发 (默认 3 分钟)
  - GpioTrigger:    RK3588 物理引脚按键触发 (带防抖与超时自截断)
  - UsbHidTrigger:  USB 医疗脚踏板 / 物理按键触发 (HID 输入事件)
  - HttpTrigger:    本地 REST 控制接口 (支持 curl / 平板 / 扫码枪调用)
"""
from .base import BaseTrigger, TriggerEvent
from .loop import LoopTrigger
from .gpio import GpioTrigger
from .usb_hid import UsbHidTrigger
from .http import HttpTrigger
from .manager import TriggerManager

__all__ = [
    "BaseTrigger",
    "TriggerEvent",
    "LoopTrigger",
    "GpioTrigger",
    "UsbHidTrigger",
    "HttpTrigger",
    "TriggerManager",
]
