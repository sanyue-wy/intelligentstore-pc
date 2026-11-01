"""
STM32 智能分拣系统 - 下位机行为仿真器 (Mock Device)
用于在无实际硬件或开发板脱机时，验证上位机通讯程序、工作流及调试GUI界面。

仿真行为包含:
- 严格 32 字节定长帧交互与 ACK 流水号机制
- 真实的步进插补/移动平滑过渡 (模拟 20Hz 状态上报)
- 抓取阶段时序: 移动到取件点 -> 触发 WAIT_PERMIT (RES_PICK) -> 等待上位机 PERMIT ->
  下潜抓取 -> 运送 -> 释放 -> 归位 -> 触发 TASK_DONE
- 支持 ESTOP 急停、CLEAR 复位、CANCEL 取消以及模拟抓取失败等异常场景
"""

from __future__ import annotations
import time
import threading
from typing import Optional, Callable
from .protocol import (
    HOST_FRAME_LEN, HOST_SOF, HOST_VER, HOST_TYPE_STATUS,
    CmdFlags, GripCmd, ZCmd, TrayCmd, StorageCmd, StatusFlags, SysState, GripperState,
    ZState, TrayState, ResFlags, ResStage, ErrorMask, HostCmd, HostStatus,
    _CMD_STRUCT, _STATUS_STRUCT
)


class MockSTM32:
    """
    虚拟 STM32 下位机仿真器
    """

    def __init__(self, tx_callback: Optional[Callable[[bytes], None]] = None):
        self.tx_callback = tx_callback
        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # 内部状态寄存器
        self.cur_x_um: int = 0
        self.cur_y_um: int = 0
        self.target_x_um: int = 0
        self.target_y_um: int = 0
        self.home_x_um: int = 0
        self.home_y_um: int = 0
        self.drop_x_um: int = 80000   # 默认放置区: X=80mm, Y=0mm
        self.drop_y_um: int = 0

        self.state: int = SysState.IDLE
        self.gripper: int = GripperState.OPEN
        self.error: int = ErrorMask.NONE
        self.flags: int = StatusFlags.USB_OK
        self.res_flags: int = ResFlags.NONE
        self.res_stage: int = ResStage.NONE
        self.progress: int = 0

        self.task_id: int = 0
        self.seq_tx: int = 1
        self.last_ack_seq: int = 0

        # 辅助机构状态
        self.z_state: int = ZState.UP
        self.tray_state: int = TrayState.IDLE
        self.storage_slot: int = 0

        # 模拟可配置行为
        self.simulate_grip_fail: bool = False  # 模拟抓取失败
        self.speed_um_per_s: int = 60000       # 移动速度: 60mm/s

        # 动作执行定时器与阶段
        self._stage_timer: float = 0.0

    def start(self):
        """启动仿真背景线程 (20Hz 状态主循环)"""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._run_loop, name="MockSTM32Loop", daemon=True)
            self._thread.start()

    def stop(self):
        """停止仿真"""
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def on_rx_bytes(self, data: bytes):
        """接收上位机发送的数据流 (下行)"""
        if len(data) < HOST_FRAME_LEN:
            return

        for offset in range(0, len(data) - HOST_FRAME_LEN + 1, HOST_FRAME_LEN):
            chunk = data[offset:offset + HOST_FRAME_LEN]
            self._process_rx_frame(chunk)

    # 别名兼容
    receive_cmd_frame = on_rx_bytes

    def _process_rx_frame(self, frame: bytes):
        if len(frame) != HOST_FRAME_LEN:
            return

        try:
            (
                sof, ver, f_type, flags, seq,
                task_id, target_x, target_y, grip_cmd,
                timeout_ms, z_cmd, tray_cmd, storage_cmd,
                storage_param, _res
            ) = _CMD_STRUCT.unpack(frame)
        except Exception:
            return

        if sof != HOST_SOF or ver != HOST_VER:
            return

        with self._lock:
            # 记录 ACK
            self.last_ack_seq = seq

            # 1. 急停指令优先
            if flags & CmdFlags.ESTOP:
                self.state = SysState.ESTOP
                self.error = ErrorMask.ESTOP
                self.flags = StatusFlags.ESTOP | StatusFlags.ALARM | StatusFlags.USB_OK
                self.res_flags = ResFlags.ABORT | ResFlags.DONE
                self.res_stage = ResStage.ABORT
                self._send_status_now()
                return

            # 2. 清除报警与复位
            if flags & CmdFlags.CLEAR:
                self.error = ErrorMask.NONE
                self.state = SysState.IDLE
                self.flags = StatusFlags.USB_OK
                self.res_flags = ResFlags.NONE
                self.res_stage = ResStage.NONE
                self.progress = 0
                self._send_status_now()
                return

            # 若已在急停或报警锁存态，拒绝后续移动等指令并报 ESTOP 错
            if self.state in (SysState.ESTOP, SysState.FAULT):
                self.error |= ErrorMask.ESTOP if self.state == SysState.ESTOP else ErrorMask.BUSY
                self._send_status_now()
                return

            # 3. 取消任务
            if flags & CmdFlags.CANCEL:
                if self.state == SysState.RUNNING:
                    self.state = SysState.IDLE
                    self.res_flags = ResFlags.ABORT | ResFlags.DONE
                    self.flags = StatusFlags.TASK_DONE | StatusFlags.USB_OK
                    self.res_stage = ResStage.ABORT
                    self.progress = 0
                self._send_status_now()
                return

            # 4. 抓取许可信号 (PERMIT)
            if flags & CmdFlags.PERMIT:
                if self.state == SysState.RUNNING and (self.res_stage == ResStage.WAIT_PERMIT or (self.res_flags & ResFlags.PICK)):
                    # 获得许可，进入下潜抓取阶段
                    self.res_stage = ResStage.GRIP
                    self._stage_timer = time.time()
                    self.res_flags &= ~ResFlags.PICK
                self._send_status_now()
                return

            # 5. 夹爪直接命令
            if flags & CmdFlags.GRIP:
                if grip_cmd == GripCmd.OPEN:
                    self.gripper = GripperState.OPEN
                elif grip_cmd == GripCmd.CLOSE:
                    self.gripper = GripperState.CLOSED
                self._send_status_now()

            # 6. Z 轴升降命令
            if (flags & CmdFlags.Z_MOTION) or z_cmd != ZCmd.HOLD:
                if z_cmd == ZCmd.UP:
                    self.z_state = ZState.UP
                elif z_cmd == ZCmd.DOWN:
                    self.z_state = ZState.DOWN
                self._send_status_now()

            # 7. 辅助机构命令 (托盘/储物盘)
            if (flags & CmdFlags.AUX) or tray_cmd != TrayCmd.HOLD or storage_cmd != StorageCmd.HOLD:
                if tray_cmd == TrayCmd.SHAKE:
                    self.tray_state = TrayState.MOVING
                elif tray_cmd == TrayCmd.HOME:
                    self.tray_state = TrayState.IDLE

                if storage_cmd == StorageCmd.STEP_CCW:
                    self.storage_slot = (self.storage_slot + 1) % 6
                elif storage_cmd == StorageCmd.STEP_CW:
                    self.storage_slot = (self.storage_slot - 1 + 6) % 6
                elif storage_cmd == StorageCmd.GOTO_SLOT:
                    self.storage_slot = storage_param % 6
                self._send_status_now()

            # 8. 运动目标指令 (新任务下发)
            if flags & CmdFlags.MOTION:
                self.task_id = task_id
                self.target_x_um = target_x
                self.target_y_um = target_y
                self.state = SysState.RUNNING
                self.flags = StatusFlags.BUSY | StatusFlags.USB_OK
                self.res_flags = ResFlags.NONE
                self.res_stage = ResStage.PICK
                self.progress = 5
                self._stage_timer = time.time()
                self._send_status_now()
                return

    def _send_status_now(self):
        """立即打包并发送一帧上行状态 (回调在锁外执行以防外部回调重入死锁)"""
        with self._lock:
            # 构建上行 FLAGS
            up_flags = StatusFlags.USB_OK
            if self.state in (SysState.RUNNING, SysState.BUSY):
                up_flags |= StatusFlags.BUSY
            if self.error != 0 or self.state == SysState.FAULT:
                up_flags |= StatusFlags.ALARM
            if self.state == SysState.ESTOP:
                up_flags |= StatusFlags.ESTOP
            if self.res_flags != 0 or self.res_stage != ResStage.NONE:
                up_flags |= StatusFlags.RESULT
            if self.res_flags & ResFlags.DONE:
                up_flags |= StatusFlags.TASK_DONE

            data = _STATUS_STRUCT.pack(
                HOST_SOF,
                HOST_VER,
                HOST_TYPE_STATUS,
                up_flags,
                self.seq_tx & 0xFFFF,
                self.task_id & 0xFFFFFFFF,
                int(self.cur_x_um),
                int(self.cur_y_um),
                int(self.state),
                int(self.gripper),
                int(self.error) & 0xFFFF,
                int(self.last_ack_seq) & 0xFFFF,
                int(self.progress) & 0xFF,
                int(self.res_flags) & 0xFF,
                int(self.res_stage) & 0xFF,
                int(self.z_state) & 0xFF,
                int(self.tray_state) & 0xFF,
                int(self.storage_slot) & 0xFF,
                b"\x00"
            )
            self.seq_tx += 1
            cb = self.tx_callback

        if cb:
            try:
                cb(data)
            except Exception:
                pass

    def _run_loop(self):
        """20Hz 仿真周期循环 (50ms 间隔)"""
        last_tick = time.time()
        while self._running:
            time.sleep(0.05)
            now = time.time()
            dt = now - last_tick
            last_tick = now

            with self._lock:
                self._update_simulation(dt)
                self._send_status_now()

    def _update_simulation(self, dt: float):
        if self.state != SysState.RUNNING:
            return

        step = int(self.speed_um_per_s * dt)

        # 阶段 1: PICK (前往取件点)
        if self.res_stage in (ResStage.PICK, ResStage.APPROACH):
            dx = self.target_x_um - self.cur_x_um
            dy = self.target_y_um - self.cur_y_um
            dist = (dx**2 + dy**2) ** 0.5

            if dist <= step or dist < 500:
                self.cur_x_um = self.target_x_um
                self.cur_y_um = self.target_y_um
                # 到达取件点上方，等待上位机抓取许可 (RES_FLAGS: HOST_RES_PICK)
                self.res_stage = ResStage.PICK
                self.res_flags = ResFlags.PICK
                self.progress = 45  # 对齐 HOST_COMM_GUIDE.md: 45%
            else:
                self.cur_x_um += int(dx * (step / dist))
                self.cur_y_um += int(dy * (step / dist))
                self.progress = min(44, int(5 + 39 * (1.0 - dist / 150000)))

        # 阶段: 等待许可 (保持静止)
        elif (self.res_flags & ResFlags.PICK) != 0:
            pass

        # 阶段 3: GRIP (下潜与抓取)
        elif self.res_stage in (ResStage.GRIP, ResStage.GRASP):
            elapsed = time.time() - self._stage_timer
            if elapsed >= 0.8:  # 抓取动作耗时约 0.8s
                if self.simulate_grip_fail:
                    # 抓空异常分支 (对齐 HOST_COMM_GUIDE.md 第 3.2 节)
                    self.gripper = GripperState.CLOSED
                    self.error = ErrorMask.GRIPFAIL
                    self.state = SysState.FAULT
                    self.res_flags = ResFlags.DONE  # DONE 但无 OK
                    self.res_stage = ResStage.PICK
                    self.progress = 50
                else:
                    # 抓取成功
                    self.gripper = GripperState.HELD
                    self.res_stage = ResStage.PLACEGO
                    self.progress = 55
                    self._stage_timer = time.time()

        # 阶段 4: PLACEGO (运送至放置区)
        elif self.res_stage in (ResStage.PLACEGO, ResStage.TRANSPORT):
            dx = self.drop_x_um - self.cur_x_um
            dy = self.drop_y_um - self.cur_y_um
            dist = (dx**2 + dy**2) ** 0.5
            if dist <= step or dist < 500:
                self.cur_x_um = self.drop_x_um
                self.cur_y_um = self.drop_y_um
                self.res_stage = ResStage.PLACE
                self.progress = 80
                self._stage_timer = time.time()
            else:
                self.cur_x_um += int(dx * (step / dist))
                self.cur_y_um += int(dy * (step / dist))
                self.progress = min(78, int(55 + 23 * (1.0 - dist / 150000)))

        # 阶段 5: PLACE (张开夹爪释放工件)
        elif self.res_stage in (ResStage.PLACE, ResStage.RELEASE):
            elapsed = time.time() - self._stage_timer
            if elapsed >= 0.5:
                self.gripper = GripperState.OPEN
                self.res_stage = ResStage.RETURN
                self.progress = 88
                self._stage_timer = time.time()

        # 阶段 6: RETURN (归位至原点)
        elif self.res_stage == ResStage.RETURN:
            dx = self.home_x_um - self.cur_x_um
            dy = self.home_y_um - self.cur_y_um
            dist = (dx**2 + dy**2) ** 0.5
            if dist <= step or dist < 500:
                self.cur_x_um = self.home_x_um
                self.cur_y_um = self.home_y_um
                # 全部任务圆满完成！
                self.state = SysState.IDLE
                self.res_flags = ResFlags.DONE | ResFlags.OK
                self.res_stage = ResStage.NONE
                self.progress = 100
                self._stage_timer = time.time()
            else:
                self.cur_x_um += int(dx * (step / dist))
                self.cur_y_um += int(dy * (step / dist))
                self.progress = min(98, int(88 + 10 * (1.0 - dist / 150000)))

        # 完成后延迟切换回 IDLE
        elif self.state == SysState.TASK_DONE:
            if time.time() - self._stage_timer > 0.5:
                self.state = SysState.IDLE
                self.res_flags = ResFlags.NONE
                self.res_stage = ResStage.NONE
                self.progress = 0
