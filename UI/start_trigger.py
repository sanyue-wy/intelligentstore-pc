# -*- coding: utf-8 -*-
"""
一键启动触发器 —— 赛规: 裁判下达开始指令后"一键启动", 运行中不能触碰笔记本、不能遥控。

任一来源触发即开始 (START_SOURCES 逗号分隔, 默认 web,key,gpio,enter):
    web    执行页 http://<X5>:8088 的「开始」按钮 (run_page 调用 trigger.fire("web"))
    key    USB 单键按钮 / 小键盘 (Linux evdev, 直接读 /dev/input/event*, 无第三方依赖)
           START_KEY_DEVICE=/dev/input/eventX 指定设备; 缺省自动找所有带按键的输入设备
           START_KEY_CODES=28,57,96 只认这些键 (回车/空格/小键盘回车); 缺省任意键
    gpio   X5 40pin 按键 (Hobot.GPIO, BOARD 编号), START_GPIO_PIN=37, 按下接地 (内部上拉)
    enter  有终端时在终端按回车 (调试用)
    auto   不等待, START_DELAY_S 秒后自动开始 (仿真联调用)

设计要点
    * 等待前先清空积压事件, 只认 arm() 之后的新按键, 防止调试时按过的键误触发;
    * 一次 wait() 只触发一次, 运行中再按无效 (避免误按导致重启);
    * 任何来源初始化失败只记日志, 不影响其它来源。
"""

from __future__ import annotations

import glob
import logging
import os
import select
import struct
import sys
import threading
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

_EV_FMT = "llHHi"                       # struct input_event (64 位: timeval 16B + type + code + value)
_EV_SIZE = struct.calcsize(_EV_FMT)
EV_KEY = 0x01


def _keyboard_devices() -> List[str]:
    """从 /proc/bus/input/devices 找出带 EV_KEY 且像键盘/按钮的设备。"""
    devs = []
    try:
        blocks = open("/proc/bus/input/devices", encoding="utf-8", errors="ignore").read().split("\n\n")
    except OSError:
        return sorted(glob.glob("/dev/input/event*"))
    for b in blocks:
        handlers = name = ""
        ev = 0
        for line in b.splitlines():
            if line.startswith("N: Name="):
                name = line.split("=", 1)[1].strip('"').lower()
            elif line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1]
            elif line.startswith("B: EV="):
                try:
                    ev = int(line.split("=", 1)[1], 16)
                except ValueError:
                    ev = 0
        if not (ev & (1 << EV_KEY)):
            continue
        if any(s in name for s in ("power", "hdmi", "gpio-keys-polled", "touch", "mouse")):
            continue
        for h in handlers.split():
            if h.startswith("event"):
                devs.append("/dev/input/" + h)
    return devs


class StartTrigger:
    def __init__(self, sources: Optional[str] = None):
        raw = sources or os.environ.get("START_SOURCES", "web,key,gpio,enter")
        self.sources = [s.strip() for s in raw.split(",") if s.strip()]
        self._ev = threading.Event()
        self._armed = False
        self.fired_by = ""
        self.armed_at = 0.0
        self.key_devices: List[str] = []
        self.gpio_pin: Optional[int] = None
        self.status: dict = {}
        self._threads_started = False
        self._key_codes = {int(c) for c in os.environ.get("START_KEY_CODES", "").split(",") if c.strip().isdigit()}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 对外
    def describe(self) -> str:
        parts = []
        if "web" in self.sources:
            parts.append("执行页按钮")
        if "key" in self.sources:
            parts.append(f"USB 按键({len(self.key_devices)} 个设备)" if self.key_devices else "USB 按键(未发现)")
        if "gpio" in self.sources:
            parts.append(f"GPIO{self.gpio_pin}" if self.gpio_pin else "GPIO(未配置)")
        if "enter" in self.sources and sys.stdin and sys.stdin.isatty():
            parts.append("终端回车")
        if "auto" in self.sources:
            parts.append("自动")
        return " / ".join(parts) or "无"

    def has_physical(self) -> bool:
        """是否有装置上的物理按键 (赛规要求一键启动且运行中不碰电脑)。"""
        return bool(self.key_devices) or bool(self.gpio_pin and self.status.get("gpio") == "ok")

    def start(self) -> None:
        """启动后台监听线程 (只调用一次)。"""
        if self._threads_started:
            return
        self._threads_started = True
        if "key" in self.sources:
            dev = os.environ.get("START_KEY_DEVICE")
            self.key_devices = [dev] if dev else _keyboard_devices()
            if self.key_devices:
                threading.Thread(target=self._key_loop, name="StartKey", daemon=True).start()
                self.status["key"] = "ok"
                logger.info("[启动键] USB 按键设备: %s", self.key_devices)
            else:
                self.status["key"] = "未发现输入设备"
        if "gpio" in self.sources and os.environ.get("START_GPIO_PIN"):
            self.gpio_pin = int(os.environ["START_GPIO_PIN"])
            threading.Thread(target=self._gpio_loop, name="StartGPIO", daemon=True).start()
        if "enter" in self.sources and sys.stdin and sys.stdin.isatty():
            threading.Thread(target=self._enter_loop, name="StartEnter", daemon=True).start()

    def arm(self) -> None:
        """进入待机: 之后的第一次触发才有效。"""
        with self._lock:
            self._ev.clear()
            self.fired_by = ""
            self._armed = True
            self.armed_at = time.time()

    def fire(self, source: str) -> bool:
        with self._lock:
            if not self._armed or time.time() - self.armed_at < 0.3:
                return False
            self._armed = False
            self.fired_by = source
            self._ev.set()
        logger.info("[启动键] 触发来源: %s", source)
        return True

    def wait(self, stop: Optional[threading.Event] = None, timeout: Optional[float] = None) -> bool:
        """阻塞到触发 (返回 True) 或 stop 被置位 / 超时 (返回 False)。"""
        if not self._armed and not self._ev.is_set():
            self.arm()
        if "auto" in self.sources:
            time.sleep(float(os.environ.get("START_DELAY_S", 1.0)))
            return self.fire("auto") or self._ev.is_set()
        end = time.time() + timeout if timeout else None
        while not self._ev.wait(0.2):
            if stop is not None and stop.is_set():
                return False
            if end and time.time() > end:
                return False
        return True

    # ------------------------------------------------------------------ 来源
    def _key_loop(self):
        fds = {}
        while True:
            for d in self.key_devices:
                if d not in fds:
                    try:
                        fds[d] = os.open(d, os.O_RDONLY | os.O_NONBLOCK)
                    except OSError as e:
                        self.status["key"] = f"{d} 打不开: {e}"
            if not fds:
                time.sleep(2.0)
                continue
            try:
                r, _, _ = select.select(list(fds.values()), [], [], 1.0)
            except (OSError, ValueError):
                r = []
            for fd in r:
                try:
                    data = os.read(fd, _EV_SIZE * 32)
                except OSError:
                    for k, v in list(fds.items()):     # 设备拔出: 关掉, 稍后重开
                        if v == fd:
                            os.close(fd)
                            del fds[k]
                    continue
                for i in range(0, len(data) - _EV_SIZE + 1, _EV_SIZE):
                    _, _, etype, code, value = struct.unpack(_EV_FMT, data[i:i + _EV_SIZE])
                    if etype == EV_KEY and value == 1 and (not self._key_codes or code in self._key_codes):
                        self.fire(f"key:{code}")

    def _gpio_loop(self):
        try:
            import Hobot.GPIO as GPIO  # type: ignore
        except Exception as e:
            self.status["gpio"] = f"Hobot.GPIO 不可用: {e}"
            logger.warning("[启动键] %s", self.status["gpio"])
            return
        try:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BOARD)
            try:
                GPIO.setup(self.gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            except Exception:
                GPIO.setup(self.gpio_pin, GPIO.IN)
            self.status["gpio"] = "ok"
        except Exception as e:
            self.status["gpio"] = f"GPIO{self.gpio_pin} 初始化失败: {e}"
            logger.warning("[启动键] %s", self.status["gpio"])
            return
        last, low_since = 1, 0.0
        while True:
            v = GPIO.input(self.gpio_pin)
            now = time.time()
            if v == 0 and last == 1:
                low_since = now
            if v == 0 and low_since and now - low_since >= 0.05:   # 50ms 消抖
                self.fire(f"gpio:{self.gpio_pin}")
                low_since = 0.0
            last = v
            time.sleep(0.01)

    def _enter_loop(self):
        while True:
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if line == "":
                return
            self.fire("enter")
