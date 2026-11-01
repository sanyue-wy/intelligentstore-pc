"""
STM32 智能分拣系统 - 上位机通讯客户端核心接口
基于 pyserial，严格遵循 D:\\stm32\\stm32cubemx\\IntelligentSorting\\User\\Module\\Communication\\HOST_COMM_GUIDE.md

核心功能:
- 物理串口 (USB CDC) / 虚拟仿真串口双模式支持
- 严格遵循 32 字节定长二进制帧规范 (小端)
- 链路活性心跳检测与看门狗 (20Hz 上报状态追踪)
- 后台独立线程组帧与状态解析 (无阻塞平滑接收)
- 下行 SEQ 自动维护与 ACK 确认机制 (重发/超时保护)
- 全套动作便捷指令 (移动、许可、急停、清警、取消、夹爪)
- 标准全流程自动化分拣执行器 (execute_sorting_task / execute_sorting_task_ex)
- 涵盖抓空 (GRIPFAIL)、急停 (ESTOP)、取消 (CANCEL) 的闭环异常响应机制
- 兼容 HOST_COMM_GUIDE.md 第 4 节的简洁调用规范

接口契约见 communication/interfaces.py —— 接替方需实现 SortingClientBase。
"""

from __future__ import annotations
import os
import sys
import time
import logging
import threading
from dataclasses import dataclass
from typing import Optional, Callable, List, Tuple, Dict, Any
import serial
import serial.tools.list_ports

from .serial_discovery import (
    discover_serial_ports, resolve_port, open_serial, describe_scan,
)
from .interfaces import (
    CommBackend, CommCapabilities, SortingClientBase, register_comm_backend
)
from .protocol import (
    HOST_FRAME_LEN, HOST_SOF, HOST_VER, HOST_TYPE_CMD, HOST_TYPE_STATUS,
    CmdFlags, GripCmd, ZCmd, TrayCmd, StorageCmd, StatusFlags, SysState, GripperState,
    ZState, TrayState, ResFlags, ResStage, ErrorMask, HostCmd, HostStatus, StreamFramer,
    build_move_cmd, build_permit_cmd, build_estop_cmd, build_clear_cmd,
    build_cancel_cmd, build_grip_cmd, build_z_cmd, build_tray_cmd, build_storage_cmd,
    build_poll_cmd, pack_cmd
)
from .mock_device import MockSTM32

logger = logging.getLogger(__name__)

#: connect() 未显式给出端口时使用的"自动发现"哨兵值。
#: 传入 None 或 "auto" 都会触发自动扫描 (见 communication/serial_discovery.py)。
DEFAULT_PORT = "auto"


@dataclass
class SortingTaskResult:
    """分拣任务执行结果详细信息"""
    success: bool
    task_id: int
    is_grip_fail: bool = False
    is_aborted: bool = False
    is_estop: bool = False
    error: int = 0
    error_desc: str = ""
    stage_desc: str = ""
    message: str = ""
    status: Optional[HostStatus] = None


class IntelligentSortingClient(SortingClientBase):
    """
    智能分拣系统上位机通讯客户端
    """

    # 协议常量映射 (对齐 HOST_COMM_GUIDE.md 第 4 节规范)
    SOF = HOST_SOF
    VER = HOST_VER
    TYPE_CMD = HOST_TYPE_CMD
    TYPE_STATUS = HOST_TYPE_STATUS

    # 下行控制 FLAGS
    FLAG_MOTION = CmdFlags.MOTION
    FLAG_GRIP   = CmdFlags.GRIP
    FLAG_ESTOP  = CmdFlags.ESTOP
    FLAG_CLEAR  = CmdFlags.CLEAR
    FLAG_CANCEL = CmdFlags.CANCEL
    FLAG_PERMIT = CmdFlags.PERMIT

    # 上行状态与结果
    RES_DONE     = ResFlags.DONE
    RES_OK       = ResFlags.OK
    RES_PICK     = ResFlags.PICK
    RES_ABORT    = ResFlags.ABORT
    ERR_GRIPFAIL = ErrorMask.GRIPFAIL

    def __init__(self, port: Optional[str] = None, baudrate: int = 115200):
        super().__init__()  # 建立 7 个回调列表与 stats 统计
        self._serial: Optional[serial.Serial] = None
        self._mock_dev: Optional[MockSTM32] = None
        self._is_mock: bool = False

        self._lock = threading.Lock()
        self._running: bool = False
        self._rx_thread: Optional[threading.Thread] = None

        # 帧解析器
        self._framer = StreamFramer(on_frame=self._on_frame_parsed)

        # 状态缓存
        self._latest_status: Optional[HostStatus] = None
        self._last_rx_time: float = 0.0

        # 串口自动发现诊断信息 (connect_auto 填充, 供日志/界面/排障使用)
        self.last_port_source: str = ""
        self.last_port_detail: str = ""
        self.port_candidates: List[Any] = []

        # 流水号与 ACK 状态
        self._seq: int = 1
        self._ack_event = threading.Event()
        self._waiting_ack_seq: int = 0
        self._ack_lock = threading.Lock()
        self._pending_acks: Dict[int, threading.Event] = {}

        # 状态边沿检测器 (避免 20Hz 持续触发回调风暴)
        self._was_pick_ready: bool = False
        self._was_task_done: bool = False
        self._last_done_task_id: Optional[int] = None

        # 若初始化传入了端口，则立即尝试连接
        if port is not None:
            self.connect(port, baudrate)

    @property
    def capabilities(self) -> CommCapabilities:
        return CommCapabilities(
            supports_serial_port_listing=True,
            supports_mock_mode=True,
            supports_raw_frame_injection=True,
            supports_multi_axis=True,
        )

    # ==========================================================================
    # 连接与端口管理
    # ==========================================================================

    @staticmethod
    def list_serial_ports() -> List[Tuple[str, str]]:
        """
        获取系统可用串口列表: 返回 [(端口名, 描述信息), ...]

        Linux 下会扫描 /dev/ttyACM* / /dev/ttyUSB* 等设备节点,
        并按"像 STM32 下位机"的程度排序; Windows 兼容 COMx。
        """
        try:
            cands = discover_serial_ports()
        except Exception as e:  # 枚举失败不应影响界面
            logger.warning("串口枚举失败: %s", e)
            return []
        res: List[Tuple[str, str]] = []
        for c in cands:
            desc = c.description or "未知设备"
            res.append((c.device, f"{c.device} ({desc})"))
        return res

    @staticmethod
    def discover_ports(probe: bool = False) -> List[Any]:
        """
        返回带排序依据的候选串口对象列表 (SerialCandidate), 供调试界面/排障使用。
        每个对象含 device / description / score / reasons / accessible。
        """
        try:
            return discover_serial_ports(probe=probe)
        except Exception as e:
            logger.warning("串口扫描失败: %s", e)
            return []

    @staticmethod
    def resolve_serial_port(port: Optional[str] = None,
                            probe: bool = False) -> Tuple[Optional[str], str, str]:
        """
        解析最终要连接的串口: (port, source, detail)。

        优先级: 显式传入 > 环境变量 STM_PORT > 自动扫描。
        port 为 None 表示未找到可用串口, 调用方应降级到 connect_mock()。
        """
        return resolve_port(port, probe=probe)

    @staticmethod
    def environment_report() -> str:
        """
        返回一行串口环境摘要, 便于开机自启日志快速定位"为什么连不上"。

        示例:
            串口扫描: 未发现 /dev/ttyACM* / /dev/ttyUSB* (下位机未接入)
        """
        try:
            return describe_scan()
        except Exception as e:
            return f"串口扫描失败: {e}"

    def connect_auto(self, baudrate: int = 115200,
                     port: Optional[str] = None,
                     timeout: float = 0.05,
                     fallback_mock: bool = True) -> bool:
        """
        自动发现串口并连接 —— **面向无人值守/开机自启场景的推荐入口**。

        行为:
            1. 按 显式 port > STM_PORT 环境变量 > 自动扫描 的顺序确定端口;
            2. 连接成功 -> 返回 True;
            3. 未找到端口或连接失败 -> 若 fallback_mock, 降级到虚拟仿真模式
               并返回 True (保证上层流程不中断); 否则返回 False。

        整个过程不抛异常、不阻塞 (扫描为本地文件枚举 + 至多一次串口打开)。
        """
        resolved, source, detail = self.resolve_serial_port(port)
        self.last_port_source = source
        self.last_port_detail = detail

        if resolved:
            logger.info("串口选择: %s (来源=%s, 依据=%s)", resolved, source, detail)
            if self.connect(resolved, baudrate=baudrate, timeout=timeout):
                # connect() 会把来源重标为 "explicit" (它收到的是已解析的端口名),
                # 这里恢复自动发现的真实来源, 便于日志与界面显示准确原因。
                self.last_port_source = source
                self.last_port_detail = detail
                return True
            logger.warning("连接串口 %s 失败, 尝试降级虚拟仿真模式", resolved)
        else:
            logger.warning("未发现可用串口 (%s), 尝试降级虚拟仿真模式", detail)

        if fallback_mock:
            try:
                return self.connect_mock()
            except Exception as e:
                logger.error("启动虚拟仿真模式失败: %s", e)
                return False
        return False

    @property
    def is_connected(self) -> bool:
        """当前是否已处于连接状态"""
        if self._is_mock:
            return self._mock_dev is not None and self._running
        return self._serial is not None and self._serial.is_open and self._running

    @property
    def is_mock(self) -> bool:
        """是否处于虚拟仿真模式"""
        return self._is_mock

    @property
    def link_description(self) -> str:
        """当前链路的一句话描述 (界面显示/日志用)"""
        if self._is_mock:
            return "虚拟仿真 (MOCK STM32)"
        if self._serial is not None and self._serial.is_open:
            port = getattr(self._serial, "port", "?")
            return f"{port} (USB Serial, {getattr(self._serial, 'baudrate', '?')}bps)"
        return "未连接"

    @property
    def is_link_alive(self) -> bool:
        """通信链路是否活跃 (下位机以 20Hz 主动上报，若 > 1.0 秒未收到帧则视为失联)"""
        if not self.is_connected:
            return False
        if self._last_rx_time <= 0.0:
            return False
        return (time.time() - self._last_rx_time) < 1.0

    @property
    def link_age_s(self) -> float:
        """距离上次接收到状态帧的时间间隔 (秒)"""
        if self._last_rx_time <= 0.0:
            return float("inf")
        return time.time() - self._last_rx_time

    def connect(self, port: Optional[str] = None, baudrate: int = 115200,
                timeout: float = 0.05) -> bool:
        """
        连接真实硬件物理串口 (USB CDC 虚拟串口)。

        port 传 None 或 "auto" 时按平台自动发现 (Linux: /dev/ttyACM* / /dev/ttyUSB*;
        Windows: COMx), 兼容旧代码里硬编码的 "COM3" 等显式端口名。
        找不到端口时返回 False (不抛异常), 由调用方决定是否 connect_mock()。
        """
        self.disconnect()

        resolved_port, source, detail = self.resolve_serial_port(port)
        self.last_port_source = source
        self.last_port_detail = detail

        if not resolved_port:
            self._notify_connection(False, f"未发现可用串口: {detail}")
            logger.warning("connect() 未找到可用串口: %s", detail)
            return False

        ser = open_serial(resolved_port, baudrate=baudrate, timeout=timeout)
        if ser is None:
            self._notify_connection(False, f"打开串口失败: {resolved_port}")
            return False

        try:
            self._serial = ser

            self._is_mock = False
            self._framer.reset()

            # 初始流水号避开固定 1，防止与下位机上次断开时残留的 s_ack_seq (1) 产生去重冲突
            with self._lock:
                if self._seq <= 1:
                    self._seq = (int(time.time() * 1000) & 0x7FFF) or 2

            self._start_rx_worker()
            self._notify_connection(True, f"已连接至串口 {resolved_port} ({baudrate}bps)")
            logger.info("串口已连接: %s (来源=%s)", resolved_port, source)
            return True
        except Exception as e:
            self._notify_connection(False, f"打开串口失败: {e}")
            logger.warning("连接串口 %s 异常: %s", resolved_port, e)
            return False

    def connect_mock(self) -> bool:
        """
        启动内置虚拟仿真器模式 (无物理硬件时使用)
        """
        self.disconnect()
        self._is_mock = True
        self._mock_dev = MockSTM32(tx_callback=self._on_mock_tx)
        self._framer.reset()
        self._mock_dev.start()
        self._running = True
        self._notify_connection(True, "已进入虚拟下位机仿真模式")
        return True

    def disconnect(self):
        """断开连接并清理资源"""
        was_connected = self.is_connected
        self._running = False

        if self._mock_dev is not None:
            self._mock_dev.stop()
            self._mock_dev = None

        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

        if self._rx_thread and self._rx_thread.is_alive():
            self._rx_thread.join(timeout=0.3)
            self._rx_thread = None

        self._is_mock = False
        self._framer.reset()
        with self._lock:
            self._latest_status = None
            self._last_rx_time = 0.0

        if was_connected:
            self._notify_connection(False, "连接已断开")

    def _notify_connection(self, connected: bool, message: str):
        for cb in self.on_connection_callbacks:
            try:
                cb(connected, message)
            except Exception:
                pass

    # ==========================================================================
    # 状态与信息获取
    # ==========================================================================

    @property
    def latest_status(self) -> Optional[HostStatus]:
        """获取下位机最近一次上报的状态快照"""
        with self._lock:
            return self._latest_status

    @property
    def last_rx_time(self) -> float:
        """最近一次收到状态的时间戳"""
        return self._last_rx_time

    def read_status(self, timeout: float = 0.1) -> Optional[HostStatus]:
        """
        读取最近一次收到的状态帧 (兼容 HOST_COMM_GUIDE.md 第 4 节 read_status)
        若尚未收到状态，最多等待 timeout 秒
        """
        if self._latest_status is not None and self.is_link_alive:
            return self._latest_status

        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._latest_status is not None:
                    return self._latest_status
            time.sleep(0.01)

        return self.latest_status

    # ==========================================================================
    # 指令发送与 ACK 处理
    # ==========================================================================

    def _next_seq(self) -> int:
        """生成循环流水号 1..65535 (对齐 HOST_COMM_GUIDE.md)"""
        with self._lock:
            seq = self._seq
            self._seq = (self._seq + 1) & 0xFFFF
            if self._seq == 0:
                self._seq = 1
            return seq

    def send_raw_frame(self, frame_bytes: bytes) -> bool:
        """发送 32 字节原始二进制数据帧"""
        if len(frame_bytes) != HOST_FRAME_LEN:
            return False

        if not self.is_connected:
            return False

        try:
            if self._is_mock and self._mock_dev:
                self._mock_dev.receive_cmd_frame(frame_bytes)
            elif self._serial and self._serial.is_open:
                self._serial.write(frame_bytes)
                self._serial.flush()

            self.stats["tx_frames"] += 1
            self.stats["tx_bytes"] += len(frame_bytes)

            for cb in self.on_raw_tx_callbacks:
                try:
                    cb(frame_bytes)
                except Exception:
                    pass
            return True
        except Exception:
            return False

    def send_cmd(
        self,
        flags: int,
        task_id: int = 0,
        x_mm: float = 0.0,
        y_mm: float = 0.0,
        grip_cmd: int = GripCmd.HOLD,
        timeout_ms: int = 0,
        wait_ack: bool = True,
        ack_timeout_s: float = 0.5,
        retries: int = 2,
        z_cmd: int = ZCmd.HOLD,
        tray_cmd: int = TrayCmd.HOLD,
        storage_cmd: int = StorageCmd.HOLD,
        storage_param: int = 0,
    ) -> bool:
        """
        发送通用下行指令，支持 ACK 确认与自动超时重试
        每次尝试均分配推进序列号，避免因单次丢包导致下位机静默去重死锁
        """
        for _ in range(retries + 1):
            seq = self._next_seq()
            cmd = HostCmd(
                flags=flags,
                seq=seq,
                task_id=task_id,
                grip_cmd=grip_cmd,
                timeout_ms=timeout_ms,
                z_cmd=z_cmd,
                tray_cmd=tray_cmd,
                storage_cmd=storage_cmd,
                storage_param=storage_param
            )
            cmd.target_x_mm = x_mm
            cmd.target_y_mm = y_mm
            frame = pack_cmd(cmd)

            ack_ev = threading.Event()
            if wait_ack:
                with self._ack_lock:
                    self._pending_acks[seq] = ack_ev
                self._waiting_ack_seq = seq
                self._ack_event.clear()

            ok = self.send_raw_frame(frame)
            if not ok:
                if wait_ack:
                    with self._ack_lock:
                        self._pending_acks.pop(seq, None)
                return False

            if not wait_ack:
                return True

            got_ack = ack_ev.wait(timeout=ack_timeout_s)
            with self._ack_lock:
                self._pending_acks.pop(seq, None)

            if got_ack:
                return True

            self.stats["ack_timeouts"] += 1

        return False

    # ==========================================================================
    # 高层业务指令
    # ==========================================================================

    def send_move(self, task_id: int, x_mm: float, y_mm: float, timeout_ms: int = 10000, wait_ack: bool = True) -> bool:
        """下发目标坐标移动指令 (FLAG_MOTION)"""
        return self.send_cmd(
            flags=CmdFlags.MOTION,
            task_id=task_id,
            x_mm=x_mm,
            y_mm=y_mm,
            timeout_ms=timeout_ms,
            wait_ack=wait_ack
        )

    def send_permit(self, task_id: int = 0, wait_ack: bool = True) -> bool:
        """下发抓取许可信号 (FLAG_PERMIT)"""
        return self.send_cmd(
            flags=CmdFlags.PERMIT,
            task_id=task_id,
            wait_ack=wait_ack
        )

    def send_estop(self, wait_ack: bool = False) -> bool:
        """下发急停指令 (FLAG_ESTOP) - 最优先，立即刹停并锁存"""
        return self.send_cmd(
            flags=CmdFlags.ESTOP,
            task_id=0,
            wait_ack=wait_ack
        )

    def send_clear(self, wait_ack: bool = True) -> bool:
        """下发清除报警与急停复位指令 (FLAG_CLEAR)"""
        return self.send_cmd(
            flags=CmdFlags.CLEAR,
            task_id=0,
            wait_ack=wait_ack
        )

    def send_cancel(self, task_id: int = 0, wait_ack: bool = True) -> bool:
        """下发取消在途任务指令 (FLAG_CANCEL)"""
        return self.send_cmd(
            flags=CmdFlags.CANCEL,
            task_id=task_id,
            wait_ack=wait_ack
        )

    def send_grip_open(self, wait_ack: bool = True) -> bool:
        """控制夹爪张开 (放件)"""
        return self.send_cmd(
            flags=CmdFlags.GRIP,
            grip_cmd=GripCmd.OPEN,
            wait_ack=wait_ack
        )

    def send_grip_close(self, wait_ack: bool = True) -> bool:
        """控制夹爪闭合 (夹取)"""
        return self.send_cmd(
            flags=CmdFlags.GRIP,
            grip_cmd=GripCmd.CLOSE,
            wait_ack=wait_ack
        )

    def send_z_up(self, wait_ack: bool = True) -> bool:
        """控制 Z 轴抬起至安全高度"""
        return self.send_cmd(
            flags=CmdFlags.Z_MOTION,
            z_cmd=ZCmd.UP,
            wait_ack=wait_ack
        )

    def send_z_down(self, wait_ack: bool = True) -> bool:
        """控制 Z 轴下潜至取件/放置高度"""
        return self.send_cmd(
            flags=CmdFlags.Z_MOTION,
            z_cmd=ZCmd.DOWN,
            wait_ack=wait_ack
        )

    def send_tray_shake(self, wait_ack: bool = True) -> bool:
        """控制托盘震动/翻转抖料"""
        return self.send_cmd(
            flags=CmdFlags.AUX,
            tray_cmd=TrayCmd.SHAKE,
            wait_ack=wait_ack
        )

    def send_tray_home(self, wait_ack: bool = True) -> bool:
        """控制托盘复位至原点 (0°)"""
        return self.send_cmd(
            flags=CmdFlags.AUX,
            tray_cmd=TrayCmd.HOME,
            wait_ack=wait_ack
        )

    def send_storage_step(self, ccw: bool = True, wait_ack: bool = True) -> bool:
        """
        控制旋转储物盘每次转动 60 度 (单格分度)
        :param ccw: True = 逆时针为正 (+60°), False = 顺时针 (-60°)
        """
        return self.send_cmd(
            flags=CmdFlags.AUX,
            storage_cmd=StorageCmd.STEP_CCW if ccw else StorageCmd.STEP_CW,
            wait_ack=wait_ack
        )

    def send_storage_slot(self, slot: int, wait_ack: bool = True) -> bool:
        """
        控制旋转储物盘定位到指定料格槽位 (0~5)
        """
        return self.send_cmd(
            flags=CmdFlags.AUX,
            storage_cmd=StorageCmd.GOTO_SLOT,
            storage_param=slot % 6,
            wait_ack=wait_ack
        )

    def send_poll(self, wait_ack: bool = False) -> bool:
        """发送心跳/空查询指令 (FLAGS=0)"""
        return self.send_cmd(
            flags=CmdFlags.NONE,
            task_id=0,
            wait_ack=wait_ack
        )

    # ==========================================================================
    # 接收线程与事件分发
    # ==========================================================================

    def _start_rx_worker(self):
        self._running = True
        self._rx_thread = threading.Thread(target=self._rx_loop, name="HostRxWorker", daemon=True)
        self._rx_thread.start()

    def _rx_loop(self):
        while self._running and self._serial and self._serial.is_open:
            try:
                waiting = self._serial.in_waiting
                to_read = max(waiting, 1)
                data = self._serial.read(to_read)
                if not data:
                    continue

                self.stats["rx_bytes"] += len(data)

                for cb in self.on_raw_rx_callbacks:
                    try:
                        cb(data)
                    except Exception:
                        pass

                self._framer.feed(data)
            except (serial.SerialException, OSError) as e:
                # 物理串口拔出或异常断开
                if self._running:
                    self._notify_connection(False, f"串口通信异常断开: {e}")
                break
            except Exception:
                if self._running:
                    time.sleep(0.01)

    def _on_mock_tx(self, data: bytes):
        """虚拟仿真器上行回调"""
        self.stats["rx_bytes"] += len(data)
        for cb in self.on_raw_rx_callbacks:
            try:
                cb(data)
            except Exception:
                pass
        self._framer.feed(data)

    def _on_frame_parsed(self, status: HostStatus):
        """组帧器解析到完整的上行 32 字节状态帧"""
        now = time.time()
        with self._lock:
            self._latest_status = status
            self._last_rx_time = now
            self.stats["rx_frames"] += 1
            self.stats["bad_bytes"] = self._framer.rx_bad_bytes

        # 检查 ACK 匹配 (并发支持与流水号累加对齐)
        ack = status.ack_seq
        with self._ack_lock:
            matched = []
            for s, ev in self._pending_acks.items():
                if ack == s or (ack > s and (ack - s < 500)):
                    ev.set()
                    matched.append(s)
            for s in matched:
                self._pending_acks.pop(s, None)

        if self._waiting_ack_seq > 0:
            if ack == self._waiting_ack_seq or (
                ack > self._waiting_ack_seq and (ack - self._waiting_ack_seq < 500)
            ):
                self._ack_event.set()

        # 事件回调触发
        for cb in self.on_status_callbacks:
            try:
                cb(status)
            except Exception:
                pass

        # 报警触发
        if status.is_alarm:
            for cb in self.on_alarm_callbacks:
                try:
                    cb(status)
                except Exception:
                    pass

        # 到达取件点 (等待许可) - 边沿触发, 避免 20Hz 状态循环高频重入
        if status.is_pick_ready:
            if not self._was_pick_ready:
                self._was_pick_ready = True
                for cb in self.on_pick_ready_callbacks:
                    try:
                        cb(status)
                    except Exception:
                        pass
        else:
            self._was_pick_ready = False

        # 任务完成 - 边沿触发 (任务刚完结或收到新的 task_id 完成)
        if status.is_task_done:
            if not self._was_task_done or (self._last_done_task_id != status.task_id):
                self._was_task_done = True
                self._last_done_task_id = status.task_id
                for cb in self.on_task_done_callbacks:
                    try:
                        cb(status)
                    except Exception:
                        pass
        else:
            self._was_task_done = False

    # ==========================================================================
    # 标准全自动化业务工作流 (对齐 HOST_COMM_GUIDE.md 第 3 & 4 节)
    # ==========================================================================

    def execute_sorting_task(
        self,
        task_id: int,
        target_x_mm: float,
        target_y_mm: float,
        timeout_s: float = 15.0,
        permit_delay_s: float = 0.1,
        progress_callback: Optional[Callable[[str, int], None]] = None,
        cancel_token: Optional[Callable[[], bool]] = None,
        permit_callback: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """
        完整自动化执行一次工件分拣闭环流程 (便捷布尔返回值)
        """
        res = self.execute_sorting_task_ex(
            task_id=task_id,
            target_x_mm=target_x_mm,
            target_y_mm=target_y_mm,
            timeout_s=timeout_s,
            permit_delay_s=permit_delay_s,
            progress_callback=progress_callback,
            cancel_token=cancel_token,
            permit_callback=permit_callback,
        )
        return res.success

    def execute_sorting_task_ex(
        self,
        task_id: int,
        target_x_mm: float,
        target_y_mm: float,
        timeout_s: float = 15.0,
        permit_delay_s: float = 0.1,
        progress_callback: Optional[Callable[[str, int], None]] = None,
        cancel_token: Optional[Callable[[], bool]] = None,
        permit_callback: Optional[Callable[[], bool]] = None,
    ) -> SortingTaskResult:
        """
        完整自动化执行一次工件分拣闭环流程，返回结构化 SortingTaskResult:
        1. 检查报警并自动清警
        2. 发送目标坐标 (FLAG_MOTION, X, Y)
        3. 等待到达取件位正上方 (RES_FLAGS & 0x04 / RES_STAGE=2)
        4. 视觉二次确认 (若配置了 permit_callback) 并发送抓取许可 (FLAG_PERMIT)
        5. 等待抓取、运送、放置、归位
        6. 抓空、急停、超时异常自动判定与分流处置
        """
        def report(msg: str, pct: int):
            if progress_callback:
                try:
                    progress_callback(msg, pct)
                except Exception:
                    pass

        start_time = time.time()

        # 1. 准备检查
        report("检查下位机状态...", 5)
        st = self.latest_status
        if st and st.is_alarm:
            report("存在历史报警，下发复位...", 8)
            self.send_clear()
            time.sleep(0.1)

        # 2. 下发运动坐标
        report(f"下发目标坐标 X={target_x_mm:.2f}mm, Y={target_y_mm:.2f}mm...", 15)
        ok = self.send_move(task_id, target_x_mm, target_y_mm, timeout_ms=int(timeout_s * 1000))
        if not ok:
            report("下发运动指令未收到 ACK 响应！", 15)
            return SortingTaskResult(
                success=False,
                task_id=task_id,
                message="下发运动指令超时未收到 ACK 应答",
                status=self.latest_status
            )

        # 3. 跟踪运行并等待到达取件位 (RES_PICK 或 WAIT_PERMIT)
        report("机械臂前往取件位中...", 25)
        reached_pick = False
        # --- 方案B 兼容兜底: app.c 类固件不置 RES_FLAGS.PICK 时按"运动到位"判据发 PERMIT ---
        # 新固件把运动/抓取交给 app.c 业务层, 到达取件位后只停在"等 PERMIT", 不再置 0x04;
        # 若上位机死等 0x04 必然超时。这里用"位置已到目标附近且连续稳定一段时间"作为到达判据主动发许可。
        # 环境变量: PERMIT_FALLBACK=0 关闭; PERMIT_ARRIVE_TOL_MM(默认5.0); PERMIT_STABLE_S(默认1.0); PERMIT_MIN_ELAPSED_S(默认0.5)
        _pf_env = os.environ.get("PERMIT_FALLBACK", "1").strip().lower()
        _pf_on = _pf_env not in ("0", "false", "no", "off", "")
        try:
            _pf_tol = float(os.environ.get("PERMIT_ARRIVE_TOL_MM", "5.0") or 5.0)
        except ValueError:
            _pf_tol = 5.0
        try:
            _pf_stable = float(os.environ.get("PERMIT_STABLE_S", "1.0") or 1.0)
        except ValueError:
            _pf_stable = 1.0
        try:
            _pf_min = float(os.environ.get("PERMIT_MIN_ELAPSED_S", "0.5") or 0.5)
        except ValueError:
            _pf_min = 0.5
        _pf_since = None
        _pf_announced = False
        while time.time() - start_time < timeout_s:
            if cancel_token and cancel_token():
                report("上位机取消任务！", 0)
                self.send_cancel(task_id)
                return SortingTaskResult(
                    success=False,
                    task_id=task_id,
                    is_aborted=True,
                    message="任务被上位机手动取消",
                    status=self.latest_status
                )

            st = self.latest_status
            if st:
                if st.is_estop:
                    report("系统处于急停状态，任务中止！", 0)
                    return SortingTaskResult(
                        success=False,
                        task_id=task_id,
                        is_estop=True,
                        error=st.error,
                        error_desc=st.error_desc,
                        message="急停生效导致任务中止",
                        status=st
                    )

                if st.is_alarm and st.state == SysState.FAULT:
                    report(f"机械臂报警停机: {st.error_desc}", 0)
                    return SortingTaskResult(
                        success=False,
                        task_id=task_id,
                        error=st.error,
                        error_desc=st.error_desc,
                        message=f"机械臂故障: {st.error_desc}",
                        status=st
                    )

                if st.is_permit_required:
                    reached_pick = True
                    break

                # 方案B: 固件未上报 PICK, 但机械臂已运动到目标附近并稳定 → 视作到达, 主动发 PERMIT
                if _pf_on and not st.is_task_done:
                    _state_ok = st.state in (SysState.RUNNING, SysState.BUSY, SysState.IDLE)
                    if _state_ok and not st.is_estop and not st.is_alarm and int(st.error) == 0:
                        _near = (abs(float(st.cur_x_mm) - float(target_x_mm)) <= _pf_tol and
                                 abs(float(st.cur_y_mm) - float(target_y_mm)) <= _pf_tol)
                        _now = time.time()
                        if _near:
                            if _pf_since is None:
                                _pf_since = _now
                            if (_now - _pf_since) >= _pf_stable and (_now - start_time) >= _pf_min:
                                if not _pf_announced:
                                    report("固件未上报 PICK 位, 已按运动到位判定发许可(方案B兜底)", 42)
                                    logger.info("[Client] PERMIT fallback: pos=(%.1f,%.1f) target=(%.1f,%.1f) after %.2fs",
                                                st.cur_x_mm, st.cur_y_mm, target_x_mm, target_y_mm, _now - start_time)
                                    _pf_announced = True
                                reached_pick = True
                                break
                        else:
                            _pf_since = None

                pct = min(40, max(25, int(st.progress)))
                report(f"移动中... 当前位置 ({st.cur_x_mm:.1f}, {st.cur_y_mm:.1f})", pct)

            time.sleep(0.05)

        if not reached_pick:
            report("前往取件点超时或未就绪！", 0)
            return SortingTaskResult(
                success=False,
                task_id=task_id,
                message="前往取件点超时",
                status=self.latest_status
            )

        # 4. 到位二次确认，下发 PERMIT 许可
        report("已到达取件位正上方，授权抓取许可...", 45)
        if permit_delay_s > 0:
            time.sleep(permit_delay_s)

        if permit_callback:
            allow_permit = permit_callback()
            if not allow_permit:
                report("视觉二次确认未通过，取消抓取！", 0)
                self.send_cancel(task_id)
                return SortingTaskResult(
                    success=False,
                    task_id=task_id,
                    is_aborted=True,
                    message="视觉二次校准未通过，主动取消抓取",
                    status=self.latest_status
                )

        self.send_permit(task_id)

        # 5. 等待抓取、运送、放置及最终完结
        report("执行下潜抓取与运送放置...", 50)
        while time.time() - start_time < timeout_s:
            if cancel_token and cancel_token():
                report("上位机取消任务！", 0)
                self.send_cancel(task_id)
                return SortingTaskResult(
                    success=False,
                    task_id=task_id,
                    is_aborted=True,
                    message="任务被上位机手动取消",
                    status=self.latest_status
                )

            st = self.latest_status
            if st:
                # 检查急停
                if st.is_estop:
                    report("系统处于急停状态，任务中止！", 0)
                    return SortingTaskResult(
                        success=False,
                        task_id=task_id,
                        is_estop=True,
                        error=st.error,
                        error_desc=st.error_desc,
                        message="急停生效",
                        status=st
                    )

                # 检查抓空 / 抓取失败 (对齐 HOST_COMM_GUIDE.md 第 3.2 节)
                if st.is_grip_fail or (st.is_alarm and (st.error & ErrorMask.GRIPFAIL)):
                    report("抓空/抓取失败！机械臂已抬起待命，需视觉重新寻位", 0)
                    return SortingTaskResult(
                        success=False,
                        task_id=task_id,
                        is_grip_fail=True,
                        error=st.error,
                        error_desc=st.error_desc,
                        stage_desc=st.stage_desc,
                        message="抓取失败/抓空(机械臂已自动抬起安全待命)",
                        status=st
                    )

                # 检查其他严重报警停机
                if st.is_alarm and st.state == SysState.FAULT:
                    report(f"执行报警停机: {st.error_desc}", 0)
                    return SortingTaskResult(
                        success=False,
                        task_id=task_id,
                        error=st.error,
                        error_desc=st.error_desc,
                        stage_desc=st.stage_desc,
                        message=f"执行报警停机: {st.error_desc}",
                        status=st
                    )

                # 检查任务是否中止
                if st.is_aborted:
                    report("任务被中止 (ABORT)！", 0)
                    return SortingTaskResult(
                        success=False,
                        task_id=task_id,
                        is_aborted=True,
                        error=st.error,
                        error_desc=st.error_desc,
                        stage_desc=st.stage_desc,
                        message="任务被中止 (ABORT)",
                        status=st
                    )

                # 检查任务是否已了结
                if st.is_task_done:
                    if st.is_success:
                        report("分拣任务圆满完成！", 100)
                        return SortingTaskResult(
                            success=True,
                            task_id=task_id,
                            stage_desc=st.stage_desc,
                            message="分拣任务成功完成",
                            status=st
                        )
                    else:
                        report(f"任务结束但未成功 (标志=0x{st.res_flags:02X}, 错误=0x{st.error:04X})", 0)
                        return SortingTaskResult(
                            success=False,
                            task_id=task_id,
                            error=st.error,
                            error_desc=st.error_desc,
                            stage_desc=st.stage_desc,
                            message=f"任务结束未成功(标志=0x{st.res_flags:02X})",
                            status=st
                        )

                pct = min(95, max(50, int(st.progress)))
                report(f"执行中 ({st.stage_desc})...", pct)

            time.sleep(0.05)

        report("任务超时未完结！", 0)
        return SortingTaskResult(
            success=False,
            task_id=task_id,
            message="任务超时未完结",
            status=self.latest_status
        )


# 兼容 HOST_COMM_GUIDE.md 第 4 节的命名规范
IntelligentSortingHost = IntelligentSortingClient


# ==============================================================================
# 后端注册 —— 现有 USB CDC 32 字节定长帧实现, 作为系统默认通讯后端
# 接替方注册同名或新名后端即可整体替换, 上层无需改动 (见 communication/interfaces.py)
# ==============================================================================

register_comm_backend(
    CommBackend(
        name="usb_cdc_32b",
        display_name="USB CDC 32 字节定长帧 (内置实现)",
        client_factory=IntelligentSortingClient,
    )
)
