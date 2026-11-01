"""
STM32 智能分拣系统 - 消息处理中心

接替本模块请先阅读 interfaces.py —— 那是本模块与各业务模块之间的唯一契约。

注意: topics.py 中的 Message / VisionTarget / ModuleHeartbeat / SystemAlert /
LogMessage 是全系统唯一定义处, vision 与 moniter 的接口文件都从这里 re-export,
切勿在别处重复定义。
"""

from .interfaces import (
    MessageBusBase,
    MessageRouterBase,
    MessageBusBackend,
    DEFAULT_BACKEND,
    register_message_bus_backend,
    list_message_bus_backends,
    get_message_bus_backend,
    create_message_bus,
)
from .topics import (
    TOPIC_COMM_STATUS,
    TOPIC_COMM_CMD_SEND,
    TOPIC_COMM_ESTOP,
    TOPIC_COMM_CLEAR,
    TOPIC_COMM_CONNECTION,
    TOPIC_VISION_FRAME,
    TOPIC_VISION_TARGET,
    TOPIC_VISION_TRIGGER,
    TOPIC_VISION_CALIB,
    TOPIC_TASK_START,
    TOPIC_TASK_PROGRESS,
    TOPIC_TASK_PERMIT,
    TOPIC_TASK_DONE,
    TOPIC_TASK_CANCEL,
    TOPIC_MONITOR_HEARTBEAT,
    TOPIC_MONITOR_ALERT,
    TOPIC_MONITOR_METRICS,
    TOPIC_SYS_LOG,
    TOPIC_RACE_STATE,
    TOPIC_RACE_ITEM,
    TOPIC_RACE_EVENT,
    TOPIC_RACE_RULE,
    Message,
    RaceState,
    SortRecord,
    RaceRule,
    VisionTarget,
    ModuleHeartbeat,
    SystemAlert,
    LogMessage,
)
from .bus import MessageBus, get_message_bus, reset_message_bus, DEFAULT_QUEUE_SIZE, DEFAULT_HISTORY_SIZE
from .race import RaceLedger, RaceSnapshot


def __getattr__(name):
    # MessageRouter 依赖通讯模块 (pyserial), 延迟导入: 只用总线/视觉/监控时不必装串口库
    if name == "MessageRouter":
        from .router import MessageRouter
        return MessageRouter
    raise AttributeError(name)


__all__ = [
    # ---- 接口契约 ----
    "MessageBusBase",
    "MessageRouterBase",
    "MessageBusBackend",
    "DEFAULT_BACKEND",
    "register_message_bus_backend",
    "list_message_bus_backends",
    "get_message_bus_backend",
    "create_message_bus",

    # ---- 主题常量 ----
    "TOPIC_COMM_STATUS",
    "TOPIC_COMM_CMD_SEND",
    "TOPIC_COMM_ESTOP",
    "TOPIC_COMM_CLEAR",
    "TOPIC_COMM_CONNECTION",
    "TOPIC_VISION_FRAME",
    "TOPIC_VISION_TARGET",
    "TOPIC_VISION_TRIGGER",
    "TOPIC_VISION_CALIB",
    "TOPIC_TASK_START",
    "TOPIC_TASK_PROGRESS",
    "TOPIC_TASK_PERMIT",
    "TOPIC_TASK_DONE",
    "TOPIC_TASK_CANCEL",
    "TOPIC_MONITOR_HEARTBEAT",
    "TOPIC_MONITOR_ALERT",
    "TOPIC_MONITOR_METRICS",
    "TOPIC_SYS_LOG",
    "TOPIC_RACE_STATE",
    "TOPIC_RACE_ITEM",
    "TOPIC_RACE_EVENT",
    "TOPIC_RACE_RULE",
    "RaceState",
    "SortRecord",
    "RaceRule",
    "RaceLedger",
    "RaceSnapshot",

    # ---- 结构化载荷 ----
    "Message",
    "VisionTarget",
    "ModuleHeartbeat",
    "SystemAlert",
    "LogMessage",

    # ---- 内置实现 ----
    "MessageBus",
    "get_message_bus",
    "reset_message_bus",
    "DEFAULT_QUEUE_SIZE",
    "DEFAULT_HISTORY_SIZE",
    "MessageRouter",
]
