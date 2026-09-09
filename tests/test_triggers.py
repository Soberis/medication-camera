#!/usr/bin/env python3
"""
test_triggers.py - 触发器子系统抽象层单元测试
"""
import json
import os
from pathlib import Path
import sys
import time
import urllib.request
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.triggers import (
    TriggerManager,
    BaseTrigger,
    LoopTrigger,
    GpioTrigger,
    UsbHidTrigger,
    HttpTrigger
)


class TestTriggers(unittest.TestCase):
    def test_01_trigger_manager_factory(self):
        """测试 TriggerManager 工厂根据配置正确实例化"""
        t_loop = TriggerManager.create({"trigger": {"mode": "loop", "interval_seconds": 10}})
        self.assertIsInstance(t_loop, LoopTrigger)

        t_gpio = TriggerManager.create({"trigger": {"mode": "gpio", "pin": 12}})
        self.assertIsInstance(t_gpio, GpioTrigger)

        t_hid = TriggerManager.create({"trigger": {"mode": "usb_hid"}})
        self.assertIsInstance(t_hid, UsbHidTrigger)

        t_http = TriggerManager.create({"trigger": {"mode": "http", "port": 5055}})
        self.assertIsInstance(t_http, HttpTrigger)

    def test_02_gpio_trigger_press_and_debounce(self):
        """测试 GpioTrigger 按键翻转与 50ms 软件防抖"""
        trigger = GpioTrigger({"debounce_ms": 50, "max_duration_seconds": 10})
        events = []

        trigger.start_listening(
            on_start=lambda ev: events.append(("START", ev.metadata)),
            on_stop=lambda ev: events.append(("STOP", ev.metadata))
        )

        try:
            # 1. 首次按下 -> 启动
            trigger.trigger_button_press()
            self.assertTrue(trigger.is_recording)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][0], "START")

            # 2. 抖动按下 (<50ms) -> 应被防抖过滤
            trigger.trigger_button_press()
            self.assertEqual(len(events), 1)

            # 3. 超过 50ms 后再次按下 -> 停止
            time.sleep(0.06)
            trigger.trigger_button_press()
            self.assertFalse(trigger.is_recording)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[1][0], "STOP")
            self.assertEqual(events[1][1]["reason"], "BUTTON_PRESSED")
        finally:
            trigger.stop_listening()

    def test_03_usb_hid_trigger(self):
        """测试 UsbHidTrigger 脚踏板/按键触发"""
        trigger = UsbHidTrigger({"debounce_ms": 50})
        events = []

        trigger.start_listening(
            on_start=lambda ev: events.append(("START", ev)),
            on_stop=lambda ev: events.append(("STOP", ev))
        )

        try:
            trigger.trigger_key_press(57)
            self.assertTrue(trigger.is_recording)

            time.sleep(0.06)
            trigger.trigger_key_press(57)
            self.assertFalse(trigger.is_recording)
            self.assertEqual(len(events), 2)
        finally:
            trigger.stop_listening()

    def test_04_http_trigger_rest_api(self):
        """测试 HttpTrigger REST API 远程控制"""
        trigger = HttpTrigger({"port": 0, "host": "127.0.0.1"})
        events = []

        trigger.start_listening(
            on_start=lambda ev: events.append(("START", ev)),
            on_stop=lambda ev: events.append(("STOP", ev))
        )

        try:
            time.sleep(0.1)  # 等待端口监听就绪
            port = trigger.port

            # 1. 查询状态 (初始 IDLE)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/trigger/status", timeout=2) as req:
                status = json.loads(req.read().decode())
                self.assertFalse(status["is_recording"])

            # 2. 触发开始录像
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/trigger/start?prescription_id=RX999", timeout=2) as req:
                resp = json.loads(req.read().decode())
                self.assertEqual(resp["status"], "OK")
                self.assertTrue(trigger.is_recording)
                self.assertEqual(len(events), 1)

            # 3. 再次查询状态 (RECORDING)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/trigger/status", timeout=2) as req:
                status = json.loads(req.read().decode())
                self.assertTrue(status["is_recording"])

            # 4. 触发停止录像
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/trigger/stop", timeout=2) as req:
                resp = json.loads(req.read().decode())
                self.assertEqual(resp["status"], "OK")
                self.assertFalse(trigger.is_recording)
                self.assertEqual(len(events), 2)
        finally:
            trigger.stop_listening()

    def test_05_safety_timeout_protection(self):
        """测试最大安全保护时长防呆自动切断"""
        trigger = GpioTrigger({"debounce_ms": 10, "max_duration_seconds": 0.2})
        events = []

        trigger.start_listening(
            on_start=lambda ev: events.append(("START", ev)),
            on_stop=lambda ev: events.append(("STOP", ev))
        )

        try:
            trigger.trigger_button_press()
            self.assertTrue(trigger.is_recording)

            # 等待 0.3s (超过 0.2s 阈值)
            time.sleep(0.3)
            self.assertFalse(trigger.is_recording)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[1][1].metadata["reason"], "SAFETY_TIMEOUT")
        finally:
            trigger.stop_listening()


if __name__ == "__main__":
    unittest.main()
