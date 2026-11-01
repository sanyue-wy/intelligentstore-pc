"""
STM32 智能分拣系统 - 程序监控模块: 系统告警管理器 (Alert Manager)

接口契约见 moniter/interfaces.py 的 AlertManagerBase。
"""

from __future__ import annotations
import time
import threading
from collections import deque
from typing import List, Dict, Optional
from MessegeCenter.topics import SystemAlert
from .interfaces import AlertManagerBase


class AlertManager(AlertManagerBase):
    """
    告警生命周期与历史日志管理器
    """

    def __init__(self, max_history: int = 200):
        self._lock = threading.Lock()
        self._active_alerts: Dict[str, SystemAlert] = {}
        self._history: deque[SystemAlert] = deque(maxlen=max_history)
        self._max_history = max_history

    def raise_alert(self, level: str, source: str, code: int, message: str) -> SystemAlert:
        """产生新的告警"""
        alert = SystemAlert(
            level=level.upper(),
            source=source,
            code=code,
            message=message,
            timestamp=time.time()
        )
        key = f"{source}:{code}"
        with self._lock:
            self._active_alerts[key] = alert
            self._history.append(alert)
        return alert

    def resolve_alert(self, source: str, code: int) -> bool:
        """解除指定的在位告警"""
        key = f"{source}:{code}"
        with self._lock:
            if key in self._active_alerts:
                del self._active_alerts[key]
                return True
        return False

    def clear_all(self):
        """清空所有在位告警"""
        with self._lock:
            self._active_alerts.clear()

    def get_active_alerts(self) -> List[SystemAlert]:
        """获取当前活跃告警列表"""
        with self._lock:
            return list(self._active_alerts.values())

    def get_history(self, limit: int = 50) -> List[SystemAlert]:
        """获取近期历史告警列表 (按时间倒序, 最多 limit 条)"""
        with self._lock:
            if limit <= 0:
                return []
            return list(reversed(self._history))[:limit]
