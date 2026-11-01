"""
STM32 智能分拣系统 - 上位机通讯模块

接替本模块请先阅读 interfaces.py —— 那是本模块与上层系统之间的唯一契约。
"""

from .interfaces import (
    CommCapabilities,
    CommLinkBase,
    SortingClientBase,
    CommBackend,
    DEFAULT_BACKEND,
    register_comm_backend,
    list_comm_backends,
    get_comm_backend,
    create_comm_client,
)
from .protocol import (
    HOST_FRAME_LEN,
    HOST_SOF,
    HOST_VER,
    HOST_TYPE_CMD,
    HOST_TYPE_STATUS,
    CmdFlags,
    GripCmd,
    ZCmd,
    TrayCmd,
    StorageCmd,
    StatusFlags,
    SysState,
    GripperState,
    ZState,
    TrayState,
    ResFlags,
    ResStage,
    ErrorMask,
    HostCmd,
    HostStatus,
    StreamFramer,
    pack_cmd,
    unpack_status,
    build_move_cmd,
    build_permit_cmd,
    build_estop_cmd,
    build_clear_cmd,
    build_cancel_cmd,
    build_grip_cmd,
    build_z_cmd,
    build_tray_cmd,
    build_storage_cmd,
    build_poll_cmd,
)
from .client import IntelligentSortingClient, SortingTaskResult, DEFAULT_PORT
from .mock_device import MockSTM32
from .serial_discovery import (
    SerialCandidate,
    ENV_PORT_KEY,
    discover_serial_ports,
    resolve_port,
    open_serial,
    describe_scan,
)

# 兼容 HOST_COMM_GUIDE.md 命名
IntelligentSortingHost = IntelligentSortingClient

__all__ = [
    # ---- 接口契约 (接替方只需关注这一组) ----
    "CommCapabilities",
    "CommLinkBase",
    "SortingClientBase",
    "CommBackend",
    "DEFAULT_BACKEND",
    "register_comm_backend",
    "list_comm_backends",
    "get_comm_backend",
    "create_comm_client",

    # ---- 数据契约 ----
    "HOST_FRAME_LEN",
    "HOST_SOF",
    "HOST_VER",
    "HOST_TYPE_CMD",
    "HOST_TYPE_STATUS",
    "CmdFlags",
    "GripCmd",
    "StatusFlags",
    "SysState",
    "GripperState",
    "ResFlags",
    "ResStage",
    "ErrorMask",
    "HostCmd",
    "HostStatus",
    "StreamFramer",
    "pack_cmd",
    "unpack_status",
    "build_move_cmd",
    "build_permit_cmd",
    "build_estop_cmd",
    "build_clear_cmd",
    "build_cancel_cmd",
    "build_grip_cmd",
    "build_poll_cmd",

    # ---- 内置实现 ----
    "IntelligentSortingClient",
    "IntelligentSortingHost",
    "SortingTaskResult",
    "MockSTM32",

    # ---- 串口自动发现 (Linux /dev/ttyACM*、/dev/ttyUSB*; Windows COMx) ----
    "SerialCandidate",
    "ENV_PORT_KEY",
    "DEFAULT_PORT",
    "discover_serial_ports",
    "resolve_port",
    "open_serial",
    "describe_scan",
]
