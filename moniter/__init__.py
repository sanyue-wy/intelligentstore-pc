"""
STM32 智能分拣系统 - 程序监控模块

接替本模块请先阅读 interfaces.py —— 那是本模块与上层系统之间的唯一契约。
"""

from .interfaces import (
    HealthWatchdogBase,
    AlertManagerBase,
    MonitorServiceBase,
    MonitorCapabilities,
    MonitorBackend,
    ModuleStatusRecord,
    DEFAULT_BACKEND,
    register_monitor_backend,
    list_monitor_backends,
    get_monitor_backend,
    create_monitor_service,
    # 数据契约 (re-export 自 MessegeCenter.topics, 供接替方直接引用)
    ModuleHeartbeat,
    SystemAlert,
)
from .health import HealthWatchdog
from .alert import AlertManager
from .service import MonitorService

__all__ = [
    # ---- 接口契约 (接替方只需关注这一组) ----
    "HealthWatchdogBase",
    "AlertManagerBase",
    "MonitorServiceBase",
    "MonitorCapabilities",
    "MonitorBackend",
    "ModuleStatusRecord",
    "DEFAULT_BACKEND",
    "register_monitor_backend",
    "list_monitor_backends",
    "get_monitor_backend",
    "create_monitor_service",

    # ---- 数据契约 ----
    "ModuleHeartbeat",
    "SystemAlert",

    # ---- 内置实现 ----
    "HealthWatchdog",
    "AlertManager",
    "MonitorService",
]