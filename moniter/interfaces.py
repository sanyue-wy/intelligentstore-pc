"""
STM32 智能分拣系统 - 监控模块接口契约 (Monitor Interfaces)
================================================================
本文件是 **监控模块与上层系统之间的唯一契约**。

监控模块需要被整体接替时 (改用第三方 APM 探针、接入日志平台、
换成更严格的分布式健康检查), 接替方只需实现本文件中的抽象基类并注册后端,
上层 (调试界面 / 综合工作台) 无需任何改动。

------------------------------------------------------------------------------
1. 分层结构
------------------------------------------------------------------------------
    health.py        心跳跟踪 + 超时看门狗 + 主机资源采集
    alert.py         在位告警与历史故障日志的生命周期管理
    service.py       后台巡检守护线程 (定时调用 health + alert 并上报)

    HealthWatchdogBase  心跳/看门狗/资源采集 —— 底层可替换件
    AlertManagerBase     告警生命周期管理 —— 底层可替换件
    MonitorServiceBase   监控后台服务 —— **上层唯一依赖的门面**

------------------------------------------------------------------------------
2. 数据契约 (re-export 自 MessegeCenter.topics, 不重复定义)
------------------------------------------------------------------------------
    ModuleHeartbeat  各模块心跳包
    SystemAlert      告警信息
    ModuleStatusRecord  单模块在线快照 (在本文件定义, health.py re-export)

    ModuleStatusRecord 定义在 interfaces.py 而非 health.py, 是为了让本文件
    成为自包含的契约而不依赖实现层 (health.py 需要继承本文件中的抽象基类)。
    原有 `from moniter.health import ModuleStatusRecord` 仍然可用。

------------------------------------------------------------------------------
3. 消息总线约定 (接替方必须遵守)
------------------------------------------------------------------------------
    订阅:
        TOPIC_MONITOR_HEARTBEAT -> 收到任意模块心跳, 记入看门狗
                                  (module_name 首次出现时自动建档)

    发布:
        TOPIC_MONITOR_ALERT     -> SystemAlert   (检出模块离线时)
        TOPIC_MONITOR_METRICS   -> Dict[str, Any] (主机 CPU/内存/线程)
        TOPIC_MONITOR_HEARTBEAT -> ModuleHeartbeat(module_name="monitor", ...)

    **注意**: monitor 自身的 module_name 必须保持 "monitor"。
    若改名, moniter/health.py 的预置模块表 (communication/vision/
    message_center/coordinator) 之外会多出一条记录, 综合看板的模块列表
    将与预期不符。

------------------------------------------------------------------------------
4. 超时阈值
------------------------------------------------------------------------------
    各模块默认 3.0 秒未收到心跳即判定 OFFLINE 并触发 code=1001 告警。
    这是 vision / communication 等模块的心跳发布频率 (1Hz / 20Hz) 的三倍余量,
    接替方若改变任一侧的心跳频率, 须同步调整 timeout_threshold_s。
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# 数据契约的单一来源 —— 消息中心已定义, 此处仅 re-export。
from MessegeCenter.topics import (
    TOPIC_MONITOR_ALERT,
    TOPIC_MONITOR_HEARTBEAT,
    TOPIC_MONITOR_METRICS,
    ModuleHeartbeat,
    SystemAlert,
)


# ==============================================================================
# 1. 数据契约: 模块健康快照
# ==============================================================================

@dataclass
class ModuleStatusRecord:
    """
    单个模块的在线健康快照 —— 调试看板直接消费本对象。

    注意: status 字段由看门狗判定, 可能被覆写为 OFFLINE;
    调用方若要区分"上报的状态"与"判定结果", 请自行记录原始 hb.status。
    """
    module_name: str
    last_heartbeat_time: float = 0.0
    status: str = "OFFLINE"       # OK, WARNING, ERROR, OFFLINE
    fps: float = 0.0
    timeout_threshold_s: float = 3.0
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_alive(self) -> bool:
        if self.last_heartbeat_time <= 0:
            return False
        return (time.monotonic() - self.last_heartbeat_time) <= self.timeout_threshold_s

    @property
    def elapsed_since_last(self) -> float:
        if self.last_heartbeat_time <= 0:
            return 999.0
        return time.monotonic() - self.last_heartbeat_time


# ==============================================================================
# 2. 能力声明
# ==============================================================================

@dataclass
class MonitorCapabilities:
    """
    监控后端能力声明 —— 调试界面据此决定显示哪些面板。
    """
    supports_host_metrics: bool = True      # 是否采集主机 CPU/内存/线程
    supports_alert_history: bool = True     # 是否保留历史故障日志
    supports_fault_injection: bool = True   # 是否支持模拟心跳注入以做故障演练
    extra: Dict[str, Any] = field(default_factory=dict)


# ==============================================================================
# 2. 抽象基类
# ==============================================================================

class HealthWatchdogBase(ABC):
    """
    模块心跳跟踪与软件看门狗。
    """

    @abstractmethod
    def record_heartbeat(self, hb: ModuleHeartbeat):
        """记录某模块的心跳上报。首次出现的 module_name 应自动建档。"""

    @abstractmethod
    def check_all_modules(self) -> Dict[str, ModuleStatusRecord]:
        """
        检查所有模块在线状态, 返回 {模块名: 快照} 的**副本**。

        快照须反映超时判定 (超过 timeout_threshold_s 未上报则 status=OFFLINE)。
        """

    def get_system_metrics(self) -> Dict[str, Any]:
        """
        主机硬件指标。

        返回字典至少包含: cpu_percent, memory_percent, memory_used_mb,
        threads_count, timestamp。采集失败时返回全零值而不要抛异常。
        """
        return {
            "cpu_percent": 0.0,
            "memory_percent": 0.0,
            "memory_used_mb": 0.0,
            "threads_count": 0,
            "timestamp": 0.0,
        }


class AlertManagerBase(ABC):
    """
    告警生命周期与历史日志管理。
    """

    @abstractmethod
    def raise_alert(self, level: str, source: str, code: int, message: str) -> SystemAlert:
        """产生一条告警 (同 source+code 视为同一告警, 覆盖更新)。"""

    @abstractmethod
    def resolve_alert(self, source: str, code: int) -> bool:
        """解除指定告警; 不存在时返回 False。"""

    @abstractmethod
    def get_active_alerts(self) -> List[SystemAlert]:
        """当前在位告警列表。"""

    @abstractmethod
    def get_history(self, limit: int = 50) -> List[SystemAlert]:
        """近期历史告警列表 (按时间倒序, 最多 limit 条)。"""

    def clear_all(self):
        """清空所有在位告警。"""
        raise NotImplementedError


class MonitorServiceBase(ABC):
    """
    监控后台服务 —— **上层系统唯一依赖的监控接口**。

    生命周期:
        service = create_monitor_service(bus)   # 或 MonitorService(bus)
        service.start(interval_s=1.0)
        ...
        service.stop()
    """

    def __init__(self, bus: Optional[Any] = None):
        self.bus = bus
        self._lock = threading.Lock()

    @property
    def capabilities(self) -> MonitorCapabilities:
        """声明本实现支持哪些能力。"""
        return MonitorCapabilities()

    @abstractmethod
    def start(self, interval_s: float = 1.0):
        """启动巡检线程。失败时应记录日志, 不向外抛异常。"""

    @abstractmethod
    def stop(self):
        """停止巡检线程, 须可重复调用。"""

    @abstractmethod
    def check_all_modules(self) -> Dict[str, ModuleStatusRecord]:
        """取一次模块健康快照 (上层界面按需轮询)。"""

    def get_system_metrics(self) -> Dict[str, Any]:
        """取一次主机指标快照。"""
        return {}

    def get_active_alerts(self) -> List[SystemAlert]:
        """取一次在位告警列表。"""
        return []

    def clear_all_alerts(self):
        """清除全部在位告警。"""
        raise NotImplementedError


# ==============================================================================
# 3. 后端注册表
# ==============================================================================

@dataclass
class MonitorBackend:
    """
    一个可插拔的监控实现。

    service_factory   必填: 无参或接受 bus 的可调用对象, 返回 MonitorServiceBase
                      (约定签名: service_factory(bus) -> MonitorServiceBase)
    watchdog_factory  选填: 返回 HealthWatchdogBase
    alert_factory     选填: 返回 AlertManagerBase
    """
    name: str
    display_name: str
    service_factory: Callable[..., MonitorServiceBase]
    watchdog_factory: Optional[Callable[..., HealthWatchdogBase]] = None
    alert_factory: Optional[Callable[..., AlertManagerBase]] = None


DEFAULT_BACKEND = "heartbeat_watchdog"

_BACKENDS: Dict[str, MonitorBackend] = {}
_BACKENDS_LOCK = threading.Lock()


def register_monitor_backend(backend: MonitorBackend, *, replace: bool = True):
    """注册 (或替换) 一个监控后端。同名后端默认允许覆盖。"""
    if not backend.name:
        raise ValueError("后端名称不能为空")
    if not callable(backend.service_factory):
        raise TypeError(f"后端 {backend.name!r} 的 service_factory 必须可调用")
    with _BACKENDS_LOCK:
        if backend.name in _BACKENDS and not replace:
            raise ValueError(f"后端 {backend.name!r} 已存在; 传 replace=True 覆盖")
        _BACKENDS[backend.name] = backend


def list_monitor_backends() -> List[str]:
    """列出所有已注册的监控后端名称。"""
    with _BACKENDS_LOCK:
        return list(_BACKENDS.keys())


def get_monitor_backend(name: Optional[str] = None) -> MonitorBackend:
    """按名称取后端; name 为 None 时返回默认后端。"""
    key = name or DEFAULT_BACKEND
    with _BACKENDS_LOCK:
        if key not in _BACKENDS:
            raise KeyError(
                f"未注册名为 {key!r} 的监控后端; 已注册: {list(_BACKENDS.keys())}"
            )
        return _BACKENDS[key]


def create_monitor_service(bus: Optional[Any] = None, backend: Optional[str] = None) -> MonitorServiceBase:
    """
    按后端名称构造监控服务 —— **上层获取监控能力的推荐入口**。

    # 替换整个监控实现时, 上层唯一需要改的就是这里的参数:
    service = create_monitor_service(bus, backend="apm_agent")
    """
    return get_monitor_backend(backend).service_factory(bus)


__all__ = [
    # ---- 数据契约 ----
    "ModuleHeartbeat",
    "SystemAlert",
    "ModuleStatusRecord",
    # ---- 抽象基类 ----
    "HealthWatchdogBase",
    "AlertManagerBase",
    "MonitorServiceBase",
    "MonitorCapabilities",
    # ---- 注册表 ----
    "MonitorBackend",
    "DEFAULT_BACKEND",
    "register_monitor_backend",
    "list_monitor_backends",
    "get_monitor_backend",
    "create_monitor_service",
    # ---- 消息中心常量 ----
    "TOPIC_MONITOR_HEARTBEAT",
    "TOPIC_MONITOR_ALERT",
    "TOPIC_MONITOR_METRICS",
]