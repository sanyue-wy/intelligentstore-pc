"""
STM32 智能分拣系统 - 消息中心接口契约 (Message Center Interfaces)
================================================================
本文件是 **消息中心与各业务模块之间的唯一契约**。

本模块是系统的心脏, 通常**不需要被替换**。但如果要用其他消息中间件
(ZeroMQ / Redis Pub-Sub / MQTT / 跨进程 IPC) 替换进程内 EventBus,
接替方只需实现本文件中的抽象基类并注册后端, 所有业务模块无需改动。

------------------------------------------------------------------------------
1. 分层结构
------------------------------------------------------------------------------
    topics.py   主题常量与结构化数据类 —— **全系统共享的数据契约**
    bus.py      线程安全发布/订阅总线 (通配符、历史回溯、吞吐统计)
    router.py   业务路由器: 把通讯/视觉/监控的事件编排成分拣闭环

    MessageBusBase      发布/订阅 —— 上层唯一依赖的门面
    MessageRouterBase   业务编排 —— 高级门面 (可选替换)

------------------------------------------------------------------------------
2. 数据契约 (定义在本文件, 全系统唯一定义点)
------------------------------------------------------------------------------
    Message         消息包装容器
    VisionTarget    视觉识别到的工件目标
    ModuleHeartbeat 模块心跳包
    SystemAlert     监控告警
    LogMessage      全局系统日志

    **本文件是 Message / VisionTarget / ModuleHeartbeat 的唯一定义处。**
    vision/interfaces.py 与 moniter/interfaces.py 都从这里 re-export。
    切勿在别处重复定义: router.py 通过 isinstance(payload, VisionTarget)
    判断任务载荷, 重复定义会导致分拣闭环静默失效。

------------------------------------------------------------------------------
3. 主题命名约定
------------------------------------------------------------------------------
    模块/动作   使用小写 + 斜杠分层, 便于 fnmatch 通配订阅:

        comm/status      下位机 20Hz 遥测状态帧     (HostStatus)
        comm/cmd/send    请求下发下行命令           (HostCmd)
        comm/estop       紧急停机广播
        comm/clear       清除报警/复位
        comm/connection  串口连接状态变化

        vision/frame     实时视频帧采集
        vision/target    识别到的工件目标           (VisionTarget)
        vision/trigger   触发拍照与识别
        vision/calib     标定参数更新通知

        task/start       启动分拣任务               (VisionTarget 或 dict)
        task/progress    任务阶段进度更新           (dict)
        task/permit      抓取许可信号
        task/done        任务完结通知               (dict: task_id/success)
        task/cancel      取消任务

        monitor/heartbeat 模块心跳包                (ModuleHeartbeat)
        monitor/alert     监控告警                  (SystemAlert)
        monitor/metrics   主机性能指标              (dict)

        sys/log          全局日志                  (dict 或 LogMessage)

    订阅端可使用 fnmatch 模式: "*", "vision/*", "comm/*", "task/*"。

------------------------------------------------------------------------------
4. 契约要求 (接替方必须保证)
------------------------------------------------------------------------------
    - publish() 必须在**调用方线程之外**完成分发, 不得让耗时订阅者阻塞发布者。
    - 订阅者抛出的异常必须被吞掉, 不得影响其他订阅者 (隔离故障)。
    - 订阅关系与历史缓冲必须线程安全 (总线跨模块、跨线程共享)。
    - 通配符语义: "x/*" 匹配 "x/y"; 精确主题与模式订阅可并存。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .topics import (
    # ---- 通讯主题 ----
    TOPIC_COMM_STATUS,
    TOPIC_COMM_CMD_SEND,
    TOPIC_COMM_ESTOP,
    TOPIC_COMM_CLEAR,
    TOPIC_COMM_CONNECTION,
    # ---- 视觉主题 ----
    TOPIC_VISION_FRAME,
    TOPIC_VISION_TARGET,
    TOPIC_VISION_TRIGGER,
    TOPIC_VISION_CALIB,
    # ---- 任务调度主题 ----
    TOPIC_TASK_START,
    TOPIC_TASK_PROGRESS,
    TOPIC_TASK_PERMIT,
    TOPIC_TASK_DONE,
    TOPIC_TASK_CANCEL,
    # ---- 监控主题 ----
    TOPIC_MONITOR_HEARTBEAT,
    TOPIC_MONITOR_ALERT,
    TOPIC_MONITOR_METRICS,
    # ---- 通用日志 ----
    TOPIC_SYS_LOG,
    # ---- 结构化载荷 ----
    Message,
    VisionTarget,
    ModuleHeartbeat,
    SystemAlert,
    LogMessage,
)

# Message / VisionTarget / ModuleHeartbeat / SystemAlert / LogMessage
# 全部 re-export 自 topics.py —— 本文件是唯一定义处, 切勿在此重复定义。


# ==============================================================================
# 1. 抽象基类
# ==============================================================================

class MessageBusBase(ABC):
    """
    发布/订阅总线 —— **全系统唯一的数据交换面**。

    典型用法:
        bus = get_message_bus()
        bus.subscribe("vision/target", on_target)
        bus.publish("task/start", {"task_id": 1, "x_mm": 40.0, "y_mm": 20.0})
    """

    @abstractmethod
    def subscribe(self, topic_pattern: str, handler: Callable[[Message], None]):
        """
        订阅主题或通配模式 (如 "vision/*", "*")。

        重复订阅同一 (pattern, handler) 应幂等 (不重复登记)。
        """

    def unsubscribe(self, topic_pattern: str, handler: Callable[[Message], None]):
        """取消订阅; 未订阅时静默忽略。"""
        raise NotImplementedError

    @abstractmethod
    def publish(self, topic: str, payload: Any, sender: str = "system") -> Message:
        """
        发布消息到指定主题, 返回构造好的 Message。

        必须在锁外分发, 且吞掉订阅者异常。
        """

    def get_history(self, topic_pattern: Optional[str] = None, limit: int = 100) -> List[Message]:
        """获取近期历史消息 (故障回溯用)。不支持时返回空列表。"""
        return []

    def clear_history(self):
        """清空历史记录。"""
        raise NotImplementedError

    def get_stats(self) -> Dict[str, Any]:
        """统计指标: total_published / total_delivered / active_patterns / topic_counts。"""
        return {}


class MessageRouterBase(ABC):
    """
    业务路由器 —— 把各模块事件编排成分拣闭环的高级门面。

    attach_communication() 是唯一的关键扩展点: 通讯模块通过回调接入总线,
    bus 侧的分拣指令 (task/start、comm/estop ...) 也会转调通讯模块。
    """

    def __init__(self, bus: Optional[Any] = None):
        self.bus = bus

    @abstractmethod
    def attach_communication(self, client: Any):
        """
        挂载通讯客户端, 打通总线双向消息。

        实现需负责:
          - 订阅 client's on_status_callbacks  -> bus.publish(TOPIC_COMM_STATUS)
          - 订阅 client's on_alarm_callbacks   -> bus.publish(TOPIC_SYS_LOG)
          - 订阅 bus 的 TOPIC_COMM_CMD_SEND / ESTOP / CLEAR -> 转调 client 下发
          - 订阅 bus 的 TOPIC_TASK_START / CANCEL / PERMIT -> 驱动分拣状态机
        """


# ==============================================================================
# 3. 后端注册表
# ==============================================================================

@dataclass
class MessageBusBackend:
    """
    一个可插拔的消息总线实现。

    bus_factory  必填: 无参可调用对象, 返回 MessageBusBase
                 (约定签名: bus_factory() -> MessageBusBase)
    """
    name: str
    display_name: str
    bus_factory: Callable[..., MessageBusBase]
    router_factory: Optional[Callable[..., MessageRouterBase]] = None


DEFAULT_BACKEND = "inproc_eventbus"

_BACKENDS: Dict[str, MessageBusBackend] = {}
_BACKENDS_LOCK = threading.Lock()


def register_message_bus_backend(backend: MessageBusBackend, *, replace: bool = True):
    """注册 (或替换) 一个消息总线后端。"""
    if not backend.name:
        raise ValueError("后端名称不能为空")
    if not callable(backend.bus_factory):
        raise TypeError(f"后端 {backend.name!r} 的 bus_factory 必须可调用")
    with _BACKENDS_LOCK:
        if backend.name in _BACKENDS and not replace:
            raise ValueError(f"后端 {backend.name!r} 已存在; 传 replace=True 覆盖")
        _BACKENDS[backend.name] = backend


def list_message_bus_backends() -> List[str]:
    """列出所有已注册的消息总线后端名称。"""
    with _BACKENDS_LOCK:
        return list(_BACKENDS.keys())


def get_message_bus_backend(name: Optional[str] = None) -> MessageBusBackend:
    """按名称取后端; name 为 None 时返回默认后端。"""
    key = name or DEFAULT_BACKEND
    with _BACKENDS_LOCK:
        if key not in _BACKENDS:
            raise KeyError(
                f"未注册名为 {key!r} 的消息总线后端; 已注册: {list(_BACKENDS.keys())}"
            )
        return _BACKENDS[key]


def create_message_bus(backend: Optional[str] = None) -> MessageBusBase:
    """
    按后端名称构造消息总线 —— **上层获取总线能力的推荐入口**。

    # 替换消息中间件时, 上层唯一需要改的就是这里的参数:
    bus = create_message_bus(backend="zeromq")
    """
    return get_message_bus_backend(backend).bus_factory()


__all__ = [
    # ---- 消息信封 ----
    "Message",
    # ---- 抽象基类 ----
    "MessageBusBase",
    "MessageRouterBase",
    "MessageBusBackend",
    # ---- 注册表 ----
    "DEFAULT_BACKEND",
    "register_message_bus_backend",
    "list_message_bus_backends",
    "get_message_bus_backend",
    "create_message_bus",
    # ---- 主题常量 ----
    "TOPIC_COMM_STATUS", "TOPIC_COMM_CMD_SEND", "TOPIC_COMM_ESTOP",
    "TOPIC_COMM_CLEAR", "TOPIC_COMM_CONNECTION",
    "TOPIC_VISION_FRAME", "TOPIC_VISION_TARGET", "TOPIC_VISION_TRIGGER", "TOPIC_VISION_CALIB",
    "TOPIC_TASK_START", "TOPIC_TASK_PROGRESS", "TOPIC_TASK_PERMIT",
    "TOPIC_TASK_DONE", "TOPIC_TASK_CANCEL",
    "TOPIC_MONITOR_HEARTBEAT", "TOPIC_MONITOR_ALERT", "TOPIC_MONITOR_METRICS",
    "TOPIC_SYS_LOG",
    # ---- 结构化载荷 ----
    "VisionTarget", "ModuleHeartbeat", "SystemAlert", "LogMessage",
]