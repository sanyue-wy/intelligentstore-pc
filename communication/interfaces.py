"""
STM32 智能分拣系统 - 通讯模块接口契约 (Communication Interfaces)
================================================================
本文件是 **通讯模块与上层系统之间的唯一契约**。

通讯模块需要被整体接替时 (改用另一套串口/USB 协议、换成 TCP/网络传输、
对接新版固件), 接替方只需实现本文件中的抽象基类并注册后端,
上层 (消息中心 / 调试界面 / 业务闭环) 无需任何改动。

------------------------------------------------------------------------------
1. 分层结构
------------------------------------------------------------------------------
    protocol.py      32 字节定长二进制帧的打包解包与枚举定义 —— **纯数据层, 无 I/O**
                     接替方若更换物理传输方式 (串口/USB/TCP), 帧格式不变则可整段复用
    client.py        线程安全的通讯客户端 (连接管理、ACK 确认、指令发送、任务执行器)

    CommLinkBase     物理链路抽象 (收发字节流)
    SortingClientBase 通讯客户端抽象 —— **上层唯一依赖的门面**

------------------------------------------------------------------------------
2. 数据契约 (全部 re-export 自 protocol.py, 不重复定义)
------------------------------------------------------------------------------
    HostStatus   上行状态帧 —— 机械臂位置、状态、夹爪、错误掩码、任务进度
    HostCmd      下行指令帧
    SortingTaskResult  分拣任务执行结果

    上层调试界面与 MessegeCenter/router.py 直接消费 HostStatus 的属性,
    因此接替方必须保证这些属性齐全且语义一致 (见第 4 节清单)。

------------------------------------------------------------------------------
3. 消息总线约定
------------------------------------------------------------------------------
    通讯模块自身不直接发布消息, 而是由 MessegeCenter/router.py 在
    attach_communication() 中把回调桥接到总线:

        on_status_callbacks   -> TOPIC_COMM_STATUS   (HostStatus)
        on_alarm_callbacks    -> TOPIC_SYS_LOG       (下位机告警文本)
        on_connection_callbacks -> 由调用方自行接管

    总线侧对通讯模块的请求 (router.py 订阅后转调客户端):
        TOPIC_COMM_CMD_SEND   -> send_cmd(...)
        TOPIC_COMM_ESTOP      -> send_estop()
        TOPIC_COMM_CLEAR      -> send_clear()
        TOPIC_TASK_PERMIT     -> send_permit(task_id)

    **因此接替方必须提供同名回调列表与同名下发方法**, 否则总线闭环会静默断开。

------------------------------------------------------------------------------
4. HostStatus 上层依赖的属性清单
------------------------------------------------------------------------------
    位置:   cur_x_mm, cur_y_mm
    状态:   state / state_desc, is_busy, is_alarm, is_estop, is_usb_ok
    夹爪:   gripper / gripper_desc
    错误:   error (16 位掩码), error_desc (文本)
    进度:   progress (0~100)
    任务:   task_id, seq, ack_seq
    阶段:   res_stage / stage_desc
    判定:   is_task_done, is_success, is_pick_ready, is_permit_required,
            is_grip_fail, is_aborted
    附件:   z_state / z_desc, tray_state / tray_desc, storage_slot / storage_desc
    原始:   raw_bytes (可选, 供报文监视器使用)
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# 数据契约的单一来源 —— protocol.py 已定义, 此处仅 re-export。
# (见 communication/protocol.py)
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


# ==============================================================================
# 1. 能力声明
# ==============================================================================

@dataclass
class CommCapabilities:
    """
    通讯后端能力声明 —— 调试界面据此决定显示哪些控件。

    接替方若改用非串口传输 (TCP/UDP/蓝牙), 应把 supports_serial_port_listing
    置为 False, 通讯调试界面的"端口下拉框"会改为手工输入地址。
    """
    supports_serial_port_listing: bool = True
    supports_mock_mode: bool = True          # 是否内置无硬件仿真能力
    supports_raw_frame_injection: bool = True  # 是否支持注入任意 32B 帧
    supports_multi_axis: bool = True           # 是否支持 Z 轴 / 托盘 / 储物盘
    extra: Dict[str, Any] = field(default_factory=dict)


# ==============================================================================
# 2. 抽象基类
# ==============================================================================

class CommLinkBase(ABC):
    """
    物理链路抽象 —— 只负责搬运字节流, 不理解协议语义。

    上层通常不直接使用本类, 它是 CommTransportFactory 产出的可替换件。
    实现者需保证 read() 不长时间阻塞 (建议 read_timeout_s <= 0.1),
    以免接收线程卡死。
    """

    @abstractmethod
    def open(self, **options) -> bool:
        """建立链路。成功返回 True, 失败返回 False (不要抛异常)。"""

    @abstractmethod
    def close(self):
        """释放链路资源, 须可重复调用。"""

    @abstractmethod
    def is_open(self) -> bool:
        """链路是否可用。"""

    @abstractmethod
    def write(self, data: bytes) -> int:
        """发送字节, 返回实际写入长度。"""

    @abstractmethod
    def read(self) -> bytes:
        """读取已到达的字节, 无数据时返回 b"" (不要阻塞等待)。"""

    @property
    def description(self) -> str:
        """链路描述 (用于界面显示), 如 "/dev/ttyACM0 (USB Serial)" 或 "COM3 (USB Serial)"。"""
        return type(self).__name__


class SortingClientBase(ABC):
    """
    通讯客户端抽象 —— **上层系统唯一依赖的通讯接口**。

    生命周期 (跨平台):
        client = create_comm_client()      # 或 IntelligentSortingClient()

        # 推荐: 自动发现串口, 无设备时自动降级到 mock (开机自启/无人值守场景)
        client.connect_auto()

        # 显式端口 (Windows COMx / Linux /dev/ttyACM*、/dev/ttyUSB* 均可)
        client.connect("/dev/ttyACM0", 115200)

        # 也可 connect(None) / connect("auto") 触发自动发现
        # 环境变量 STM_PORT 可强制指定端口, 优先级高于自动扫描
        client.connect_mock()             # 明确使用虚拟仿真
        ...
        client.disconnect()

    线程约定:
        connect() 之后内部自行开接收线程解析状态帧;
        所有查询属性与回调都必须是线程安全的。
    """

    #: 协议常量 (对齐 HOST_COMM_GUIDE.md)
    FRAME_LEN = HOST_FRAME_LEN
    SOF = HOST_SOF

    def __init__(self):
        # ---- 外部回调列表: 约定名, router.py 与调试界面直接依赖 ----
        #: 每次收到状态帧 (约 20Hz)
        self.on_status_callbacks: List[Callable[[HostStatus], None]] = []
        #: 收到原始下行字节 (报文监视器)
        self.on_raw_tx_callbacks: List[Callable[[bytes], None]] = []
        #: 收到原始上行字节 (报文监视器)
        self.on_raw_rx_callbacks: List[Callable[[bytes], None]] = []
        #: 检出报警状态帧时触发
        self.on_alarm_callbacks: List[Callable[[HostStatus], None]] = []
        #: 到达取件点、等待抓取许可时触发
        self.on_pick_ready_callbacks: List[Callable[[HostStatus], None]] = []
        #: 任务了结 (成功/失败/中止) 时触发
        self.on_task_done_callbacks: List[Callable[[HostStatus], None]] = []
        #: 连接状态变化 (connected: bool, message: str)
        self.on_connection_callbacks: List[Callable[[bool, str], None]] = []

        #: 通信统计 (收发帧数/字节数/ACK 超时/坏字节)
        self.stats: Dict[str, int] = {
            "tx_frames": 0,
            "rx_frames": 0,
            "tx_bytes": 0,
            "rx_bytes": 0,
            "ack_timeouts": 0,
            "bad_bytes": 0,
        }

    # -- 能力与状态 ----------------------------------------------------------
    @property
    def capabilities(self) -> CommCapabilities:
        """声明本实现支持哪些能力, 调试界面据此调整界面。"""
        return CommCapabilities()

    @property
    def is_connected(self) -> bool:
        """当前是否已连接。"""
        raise NotImplementedError

    @property
    def is_mock(self) -> bool:
        """是否处于虚拟仿真模式。"""
        raise NotImplementedError

    @property
    def is_link_alive(self) -> bool:
        """通信链路是否活跃 (下位机以 20Hz 上报, 超时未收到帧则视为失联)。"""
        raise NotImplementedError

    @property
    def latest_status(self) -> Optional[HostStatus]:
        """最近一次收到的状态帧快照, 尚未收到时为 None。"""
        raise NotImplementedError

    # -- 连接管理 ------------------------------------------------------------
    @staticmethod
    def list_serial_ports() -> List[Tuple[str, str]]:
        """可用端口列表: [(端口名, 描述), ...]。不支持枚举时返回 []。"""
        return []

    @abstractmethod
    def connect(self, port: str, baudrate: int = 115200, timeout: float = 0.05) -> bool:
        """连接真实硬件。成功返回 True。"""

    def connect_mock(self) -> bool:
        """启动内置虚拟仿真器 (无物理硬件时使用)。未实现时返回 False。"""
        return False

    @abstractmethod
    def disconnect(self):
        """断开连接并清理资源, 须可重复调用。"""

    # -- 指令下发 ------------------------------------------------------------
    def send_cmd(self, *args, **kwargs) -> bool:
        """底层或通用指令下发接口。"""
        return False

    @abstractmethod
    def send_move(self, task_id: int, x_mm: float, y_mm: float,
                  timeout_ms: int = 10000, wait_ack: bool = True) -> bool:
        """下发目标坐标移动指令。"""

    @abstractmethod
    def send_permit(self, task_id: int = 0, wait_ack: bool = True) -> bool:
        """下发抓取许可信号。"""

    @abstractmethod
    def send_estop(self, wait_ack: bool = False) -> bool:
        """下发急停指令 (最优先, 立即刹停并锁存)。"""

    @abstractmethod
    def send_clear(self, wait_ack: bool = True) -> bool:
        """下发清除报警与急停复位指令。"""

    @abstractmethod
    def send_cancel(self, task_id: int = 0, wait_ack: bool = True) -> bool:
        """取消在途任务。"""

    @abstractmethod
    def send_grip_open(self, wait_ack: bool = True) -> bool:
        """夹爪张开 (放件)。"""

    @abstractmethod
    def send_grip_close(self, wait_ack: bool = True) -> bool:
        """夹爪闭合 (夹取)。"""

    # -- 任务执行器 ----------------------------------------------------------
    @abstractmethod
    def execute_sorting_task(
        self,
        task_id: int,
        target_x_mm: float,
        target_y_mm: float,
        timeout_s: float = 15.0,
        progress_callback: Optional[Callable[[str, int], None]] = None,
        cancel_token: Optional[Callable[[], bool]] = None,
        **kwargs,
    ) -> bool:
        """
        执行一次完整的工件分拣闭环流程。

        这是**任务级的同步接口**: 内部负责 移动 -> 等待到位 -> 下发抓取许可 ->
        等待抓取/运送/放置/归位, 并在急停、抓空、超时、取消时提前返回 False。
        router.py 的分拣状态机直接依赖它。
        """


# ==============================================================================
# 3. 后端注册表
# ==============================================================================

@dataclass
class CommBackend:
    """
    一个可插拔的通讯实现。

    client_factory  必填: 无参可调用对象, 返回 SortingClientBase
                     (约定签名: client_factory() -> SortingClientBase)
    link_factory    选填: 返回 CommLinkBase, 供内部或测试构造独立链路
    """
    name: str
    display_name: str
    client_factory: Callable[..., SortingClientBase]
    link_factory: Optional[Callable[..., CommLinkBase]] = None


DEFAULT_BACKEND = "usb_cdc_32b"

_BACKENDS: Dict[str, CommBackend] = {}
_BACKENDS_LOCK = threading.Lock()


def register_comm_backend(backend: CommBackend, *, replace: bool = True):
    """注册 (或替换) 一个通讯后端。同名后端默认允许覆盖。"""
    if not backend.name:
        raise ValueError("后端名称不能为空")
    if not callable(backend.client_factory):
        raise TypeError(f"后端 {backend.name!r} 的 client_factory 必须可调用")
    with _BACKENDS_LOCK:
        if backend.name in _BACKENDS and not replace:
            raise ValueError(f"后端 {backend.name!r} 已存在; 传 replace=True 覆盖")
        _BACKENDS[backend.name] = backend


def list_comm_backends() -> List[str]:
    """列出所有已注册的通讯后端名称。"""
    with _BACKENDS_LOCK:
        return list(_BACKENDS.keys())


def get_comm_backend(name: Optional[str] = None) -> CommBackend:
    """按名称取后端; name 为 None 时返回默认后端。"""
    key = name or DEFAULT_BACKEND
    with _BACKENDS_LOCK:
        if key not in _BACKENDS:
            raise KeyError(
                f"未注册名为 {key!r} 的通讯后端; 已注册: {list(_BACKENDS.keys())}"
            )
        return _BACKENDS[key]


def create_comm_client(backend: Optional[str] = None) -> SortingClientBase:
    """
    按后端名称构造通讯客户端 —— **上层获取通讯能力的推荐入口**。

    # 替换整个通讯实现时, 上层唯一需要改的就是这里的参数:
    client = create_comm_client(backend="tcp_link")
    """
    return get_comm_backend(backend).client_factory()


__all__ = [
    # ---- 数据契约 (re-export 自 protocol.py) ----
    "HostStatus",
    "HostCmd",
    "HOST_FRAME_LEN",
    "HOST_SOF",
    "HOST_VER",
    "HOST_TYPE_CMD",
    "HOST_TYPE_STATUS",
    "CmdFlags", "GripCmd", "ZCmd", "TrayCmd", "StorageCmd",
    "StatusFlags", "SysState", "GripperState", "ZState", "TrayState",
    "ResFlags", "ResStage", "ErrorMask",
    "StreamFramer", "pack_cmd", "unpack_status",
    "build_move_cmd", "build_permit_cmd", "build_estop_cmd", "build_clear_cmd",
    "build_cancel_cmd", "build_grip_cmd", "build_z_cmd", "build_tray_cmd",
    "build_storage_cmd", "build_poll_cmd",

    # ---- 接口契约 ----
    "CommCapabilities",
    "CommLinkBase",
    "SortingClientBase",
    "CommBackend",
    "DEFAULT_BACKEND",
    "register_comm_backend",
    "list_comm_backends",
    "get_comm_backend",
    "create_comm_client",
]