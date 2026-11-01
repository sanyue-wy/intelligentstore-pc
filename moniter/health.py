"""
STM32 智能分拣系统 - 程序监控模块: 模块健康状态与看门狗 (Health & Watchdog)

接口契约见 moniter/interfaces.py 的 HealthWatchdogBase。
"""

from __future__ import annotations
import time
import threading
from typing import Dict, Optional, Any
import psutil

from MessegeCenter.topics import ModuleHeartbeat
# ModuleStatusRecord 的定义在 interfaces.py (契约层), 此处 re-export 保持兼容
from .interfaces import HealthWatchdogBase, ModuleStatusRecord

__all__ = ["HealthWatchdog", "ModuleStatusRecord"]


class HealthWatchdog(HealthWatchdogBase):
    """
    模块健康监控器与软件看门狗
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._modules: Dict[str, ModuleStatusRecord] = {
            "communication": ModuleStatusRecord("communication", timeout_threshold_s=3.0),
            "vision": ModuleStatusRecord("vision", timeout_threshold_s=3.0),
            "message_center": ModuleStatusRecord("message_center", timeout_threshold_s=3.0),
            "coordinator": ModuleStatusRecord("coordinator", timeout_threshold_s=3.0),
        }

    def record_heartbeat(self, hb: ModuleHeartbeat):
        """记录某模块的心跳上报"""
        with self._lock:
            if hb.module_name not in self._modules:
                self._modules[hb.module_name] = ModuleStatusRecord(hb.module_name)

            rec = self._modules[hb.module_name]
            rec.last_heartbeat_time = time.monotonic()
            rec.status = hb.status
            rec.fps = hb.fps
            rec.details = hb.details

    def check_all_modules(self) -> Dict[str, ModuleStatusRecord]:
        """检查所有模块在线状态"""
        now = time.monotonic()
        with self._lock:
            for rec in self._modules.values():
                if rec.last_heartbeat_time > 0 and (now - rec.last_heartbeat_time) > rec.timeout_threshold_s:
                    rec.status = "OFFLINE"
            return {k: ModuleStatusRecord(
                module_name=v.module_name,
                last_heartbeat_time=v.last_heartbeat_time,
                status=v.status,
                fps=v.fps,
                timeout_threshold_s=v.timeout_threshold_s,
                details=dict(v.details)
            ) for k, v in self._modules.items()}

    def get_system_metrics(self) -> Dict[str, Any]:
        """获取主机系统硬件指标 (CPU, 内存, 线程数)"""
        try:
            cpu_pct = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            proc = psutil.Process()
            return {
                "cpu_percent": cpu_pct,
                "memory_percent": mem.percent,
                "memory_used_mb": round(proc.memory_info().rss / (1024 * 1024), 1),
                "threads_count": proc.num_threads(),
                "timestamp": time.time()
            }
        except Exception:
            return {
                "cpu_percent": 0.0,
                "memory_percent": 0.0,
                "memory_used_mb": 0.0,
                "threads_count": 0,
                "timestamp": time.time()
            }
