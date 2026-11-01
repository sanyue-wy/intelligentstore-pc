"""
STM32 智能分拣系统 - 串口自动发现 (Serial Port Auto-Discovery)
================================================================
本模块解决"上位机换到 Linux (RDK X5 / Ubuntu) 后硬编码 'COM3' 必然打不开"的问题。

设计要点:
    1. 平台优先:  Linux 扫描 /dev/ttyACM*, /dev/ttyUSB*, /dev/ttyAMA*;
                 Windows 用 pyserial 枚举 COMx, 并按 STM32 VID 优先排序。
    2. 环境覆盖:  显式指定的端口 / STM_PORT 环境变量 永远优先于自动扫描,
                 方便现场手工固定。
    3. 可解释:   discover_serial_ports() 返回带 score 与 reason 的候选列表,
                 resolve_port() 返回 (port, source, detail), 便于日志与排障。
    4. 不打扰硬件: 默认只做 os.access 权限检查, 不打开设备。
                 (STM32 USB-CDC 打开瞬间会拉 DTR 复位下位机, 探测需谨慎,
                  需要时显式传 probe=True。)

本模块不依赖 communication 包内其他文件, 可单独使用。
"""

from __future__ import annotations

import os
import sys
import glob
import time
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Iterable

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")
IS_DARWIN = sys.platform == "darwin"

#: 环境变量名 —— 现场固定端口用, 例如 STM_PORT=/dev/ttyACM0
ENV_PORT_KEY = "STM_PORT"

#: STM32 官方 USB VID (STMicroelectronics). USB-CDC 虚拟串口常见 VID。
STM32_VIDS = {"0483", "1a86", "10c4", "0403"}  # ST / CH340 / CP210x / FTDI (统一小写匹配)

#: 描述里出现这些关键字视为"疑似下位机", 提高排序优先级
_STM_HINTS = ("stm", "st-link", "stmicro", "cdc", "acm", "usb serial", "usb-serial",
              "ch340", "ch341", "cp210", "ft232", "uart", "串口")


@dataclass
class SerialCandidate:
    """一个候选串口及其排序依据。"""
    device: str
    description: str = ""
    hwid: str = ""
    score: int = 0
    reasons: List[str] = field(default_factory=list)
    accessible: Optional[bool] = None

    def __str__(self) -> str:
        return f"{self.device} ({self.description or '未知设备'}, score={self.score})"


def _import_serial():
    try:
        import serial  # noqa: F401
        import serial.tools.list_ports as list_ports
        return serial, list_ports
    except Exception as e:  # pyserial 未安装时不应让整个程序崩溃
        logger.debug("pyserial 不可用: %s", e)
        return None, None


def _score_candidate(device: str, description: str, hwid: str) -> Tuple[int, List[str]]:
    """给一个候选端口打分; 分数越高越可能是我们要的 STM32 下位机。"""
    score = 0
    reasons: List[str] = []
    low_desc = f"{description} {hwid}".lower()
    base = os.path.basename(device)
    low_dev = device.lower()

    # 设备类型优先级 (Linux)
    if low_dev.startswith("/dev/ttyacm"):
        score += 100
        reasons.append("ttyACM (USB CDC 设备类, STM32 虚拟串口典型)")
    elif low_dev.startswith("/dev/ttyusb"):
        score += 80
        reasons.append("ttyUSB (USB 转串口芯片)")
    elif low_dev.startswith("/dev/ttyama") or low_dev.startswith("/dev/ttyps") or \
            low_dev.startswith("/dev/ttyths"):
        score += 40
        reasons.append("SoC 板载 UART")
    elif low_dev.startswith("/dev/tty"):
        score += 10
        reasons.append("通用 tty")
    elif base.upper().startswith("COM"):
        score += 70
        reasons.append("Windows COM 口")

    # VID 命中 (统一转换为小写比对)
    for vid in STM32_VIDS:
        vid_low = vid.lower()
        if (f"vid:pid={vid_low}:" in low_desc or f"vid_{vid_low}" in low_desc or
                f"usb vid:pid={vid_low}" in low_desc or f"vid:{vid_low}" in low_desc or
                f":{vid_low}:" in low_desc or f"vid_{vid_low}" in low_desc):
            score += 50
            reasons.append(f"USB VID {vid} (疑似 STM32/串口芯片)")
            break

    # 描述关键字
    for hint in _STM_HINTS:
        if hint in low_desc:
            score += 20
            reasons.append(f"描述含 {hint!r}")
            break

    # 明确不像下位机的: 蓝牙 / 调试串口
    if "bluetooth" in low_desc or "rfcomm" in low_dev:
        score -= 100
        reasons.append("蓝牙串口, 降权")
    if "console" in low_desc:
        score -= 60
        reasons.append("疑似控制台串口, 降权")

    return score, reasons


def _glob_linux_nodes() -> List[str]:
    """Linux 下按优先级扫描常见串口设备节点。"""
    patterns = ["/dev/ttyACM*", "/dev/ttyUSB*", "/dev/ttyAMA*", "/dev/ttyCH343USB*"]
    found: List[str] = []
    seen = set()
    for pat in patterns:
        try:
            for p in sorted(glob.glob(pat)):
                if p not in seen:
                    seen.add(p)
                    found.append(p)
        except OSError:
            continue
    return found


def _pyserial_comports() -> List[Tuple[str, str, str]]:
    """通过 pyserial 枚举: [(device, description, hwid), ...]。"""
    _serial, list_ports = _import_serial()
    if list_ports is None:
        return []
    try:
        out = []
        for p in list_ports.comports():
            out.append((p.device, p.description or "", getattr(p, "hwid", "") or ""))
        return out
    except Exception as e:
        logger.debug("pyserial 枚举失败: %s", e)
        return []


def _probe_openable(device: str, timeout: float = 0.2) -> bool:
    """尝试短暂打开端口, 确认真的可用。注意: 可能复位 USB-CDC 下位机。"""
    serial, _ = _import_serial()
    if serial is None:
        return False
    try:
        s = serial.Serial(device, 115200, timeout=timeout)
        try:
            s.close()
        except Exception:
            pass
        return True
    except Exception:
        return False


def discover_serial_ports(include_common: bool = True,
                         probe: bool = False,
                         only_accessible: bool = False) -> List[SerialCandidate]:
    """
    扫描本机串口并按"像 STM32 下位机"的程度降序返回。

    参数:
        include_common   False 时只返回 ttyACM*/ttyUSB* (Linux) 或 COMx (Windows)。
        probe            是否实际打开端口验证 (会拉 DTR, 可能复位下位机)。
        only_accessible  过滤掉当前无读写权限的节点 (容器未映射设备时常见)。

    返回: List[SerialCandidate], 已按 score 降序、同级按设备名排序。
    """
    if not include_common and not (IS_LINUX or IS_WINDOWS or IS_DARWIN):
        return []

    devices: List[Tuple[str, str, str]] = []

    # 1) Linux 设备节点 (即使 pyserial 枚举不到, 节点存在也有意义)
    if IS_LINUX:
        node_set = set(_glob_linux_nodes())
        by_dev = {d: (d, desc, hwid) for d, desc, hwid in _pyserial_comports()}
        for node in sorted(node_set):
            devices.append(by_dev.get(node, (node, "", "")))
        if include_common:
            # 补充 pyserial 能枚举到但不在常见模式里的节点
            for dev, desc, hwid in by_dev.values():
                if dev not in node_set:
                    devices.append((dev, desc, hwid))
    else:
        # 2) Windows / macOS: 依赖 pyserial 枚举
        for dev, desc, hwid in _pyserial_comports():
            if include_common or dev.upper().startswith("COM") or dev.startswith("/dev/"):
                devices.append((dev, desc, hwid))

    # 去重保序
    seen = set()
    uniq: List[Tuple[str, str, str]] = []
    for dev, desc, hwid in devices:
        if dev and dev not in seen:
            seen.add(dev)
            uniq.append((dev, desc, hwid))

    candidates: List[SerialCandidate] = []
    for dev, desc, hwid in uniq:
        score, reasons = _score_candidate(dev, desc, hwid)
        accessible: Optional[bool] = None
        if dev.startswith("/dev/"):
            try:
                accessible = os.access(dev, os.R_OK | os.W_OK)
            except OSError:
                accessible = False
            if accessible:
                score += 20
                reasons.append("当前用户可读写")
            else:
                score -= 80
                reasons.append("当前用户无读写权限 (需 --privileged/映射设备或加 dialout 组)")
                if only_accessible:
                    continue
        if probe:
            if _probe_openable(dev):
                score += 30
                reasons.append("试打开成功")
            else:
                score -= 40
                reasons.append("试打开失败")
                if only_accessible:
                    continue

        candidates.append(SerialCandidate(
            device=dev, description=desc, hwid=hwid,
            score=score, reasons=reasons, accessible=accessible
        ))

    candidates.sort(key=lambda c: (-c.score, c.device))
    return candidates


def resolve_port(port: Optional[str] = None,
                 env_key: str = ENV_PORT_KEY,
                 allow_auto: bool = True,
                 probe: bool = False) -> Tuple[Optional[str], str, str]:
    """
    决定最终要连接的串口。

    优先级:
        1. 显式传入的 port (非 None / 非空 / 非 "auto")
        2. 环境变量 STM_PORT
        3. 自动扫描结果中得分最高的端口
        4. 都没有 -> (None, "none", 原因文本)

    返回: (port, source, detail)
        source ∈ {"explicit", "env", "auto", "none"}
        port  为 None 表示未找到任何可用串口 (调用方应降级到 mock)
    """
    # 1) 显式传入
    if port is not None:
        p = str(port).strip()
        if p and p.lower() not in ("auto", "none", "mock"):
            return p, "explicit", f"显式指定 {p}"
        if p.lower() == "mock":
            return None, "explicit-mock", "显式要求虚拟仿真模式"

    # 2) 环境变量
    if env_key:
        env_port = (os.environ.get(env_key) or "").strip()
        if env_port and env_port.lower() not in ("auto", "none", "mock"):
            return env_port, "env", f"环境变量 {env_key}={env_port}"

    # 3) 自动扫描
    if not allow_auto:
        return None, "none", "已禁用自动扫描"

    try:
        cands = discover_serial_ports(probe=probe)
    except Exception as e:  # 任何异常都不应让主程序崩溃
        logger.warning("串口自动扫描异常: %s", e)
        return None, "none", f"自动扫描异常: {e}"

    # 只接受得分 > 0 的有效候选, 避免误连到降权或无关 tty
    usable = [c for c in cands if c.score > 0]
    if usable:
        best = usable[0]
        detail = "; ".join(best.reasons) or "自动扫描命中"
        others = ", ".join(c.device for c in usable[1:4])
        if others:
            detail += f" (次选: {others})"
        return best.device, "auto", detail

    if cands:
        return None, "none", (
            "扫描到串口但不可用: " +
            "; ".join(f"{c.device}[{','.join(c.reasons)}]" for c in cands[:3])
        )
    if IS_LINUX:
        return None, "none", "未发现 /dev/ttyACM* 或 /dev/ttyUSB* (下位机未接入?)"
    return None, "none", "未发现任何 COM 串口 (下位机未接入?)"


def open_serial(device: str, baudrate: int = 115200, timeout: float = 0.05):
    """
    打开串口并返回 pyserial.Serial 实例; 失败返回 None (不抛异常)。

    集中处理不同平台的打开差异与异常归一化。
    """
    serial, _ = _import_serial()
    if serial is None:
        logger.error("pyserial 未安装, 无法打开串口")
        return None
    try:
        s = serial.Serial(
            port=device,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
        )
        try:
            s.reset_input_buffer()
            s.reset_output_buffer()
        except Exception:
            pass
        return s
    except Exception as e:
        logger.warning("打开串口 %s 失败: %s", device, e)
        return None


# ---- 供日志/界面使用的一行摘要 ------------------------------------------------

def describe_scan() -> str:
    """返回人类可读的扫描结果摘要, 便于在启动日志中一次性说明环境。"""
    cands = discover_serial_ports()
    if not cands:
        if IS_LINUX:
            return "串口扫描: 未发现 /dev/ttyACM* / /dev/ttyUSB* (下位机未接入)"
        return "串口扫描: 未发现 COM 口"
    return "串口扫描: " + " | ".join(str(c) for c in cands[:5])


__all__ = [
    "SerialCandidate",
    "ENV_PORT_KEY",
    "STM32_VIDS",
    "discover_serial_ports",
    "resolve_port",
    "open_serial",
    "describe_scan",
    "IS_WINDOWS",
    "IS_LINUX",
    "IS_DARWIN",
]
