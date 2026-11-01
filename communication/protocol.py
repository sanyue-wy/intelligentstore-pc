"""
STM32 智能分拣系统 - 上位机通信协议层
对齐 D:\\stm32\\stm32cubemx\\IntelligentSorting\\User\\Module\\Communication\\host_comm.h / HOST_COMM_GUIDE.md

传输规范:
- 定长 32 字节二进制帧 (小端 Little-Endian)
- 同步头: 0xAA 0x55
- 协议版本: 0x01
- 下行帧类型: 0x01 (Host -> STM32 命令帧)
- 上行帧类型: 0x81 (STM32 -> Host 状态帧)
- 坐标单位: 微米 (um, 1mm = 1000um)
"""

from __future__ import annotations
import struct
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag
from typing import Optional, Tuple, List, Callable


# ==============================================================================
# 协议基础常量
# ==============================================================================
HOST_SOF0: int = 0xAA
HOST_SOF1: int = 0x55
HOST_SOF: bytes = bytes([HOST_SOF0, HOST_SOF1])  # b'\xAA\x55'
HOST_FRAME_LEN: int = 32
HOST_VER: int = 0x01
HOST_TYPE_CMD: int = 0x01
HOST_TYPE_STATUS: int = 0x81


# ==============================================================================
# 枚举与位掩码定义
# ==============================================================================

class CmdFlags(IntFlag):
    """下行命令标志位 (位掩码)"""
    NONE    = 0x00
    MOTION   = 0x01  # 运动有效 (包含目标 X, Y 坐标)
    GRIP     = 0x02  # 夹爪动作有效 (执行 grip_cmd)
    ESTOP    = 0x04  # 急停 (立刻刹停并锁存)
    CLEAR    = 0x08  # 清除报警/复位 (清除错误与急停锁存)
    CANCEL   = 0x10  # 取消任务 (中止当前任务并安全复位)
    PERMIT   = 0x20  # 抓取许可信号 (到位后授权允许下潜抓取)
    Z_MOTION = 0x40  # Z 轴升降动作有效 (执行 z_cmd)
    AUX      = 0x80  # 辅助机构有效 (托盘 tray_cmd / 储物格 storage_cmd)


class GripCmd(IntEnum):
    """下行夹爪命令"""
    HOLD  = 0  # 保持
    OPEN  = 1  # 张开
    CLOSE = 2  # 闭合


class ZCmd(IntEnum):
    """下行 Z 轴升降控制命令 (Byte 22)"""
    HOLD = 0  # 保持
    UP   = 1  # 抬起至安全高度 (0°)
    DOWN = 2  # 下潜至工作取件高度 (180°)


class TrayCmd(IntEnum):
    """下行托盘控制命令 (Byte 23)"""
    HOLD   = 0  # 保持
    SHAKE  = 1  # 震动/翻转摆动一次
    HOME   = 2  # 托盘回原点 (0°)


class StorageCmd(IntEnum):
    """下行储物格旋转控制命令 (Byte 24)"""
    HOLD      = 0  # 保持
    STEP_CCW  = 1  # 逆时针旋转 60° (+1格, 逆时针为正)
    STEP_CW   = 2  # 顺时针旋转 60° (-1格)
    GOTO_SLOT = 3  # 定位到指定槽位 (0~5)


class ZState(IntEnum):
    """上行 Z 轴当前状态 (Byte 28)"""
    UP     = 0  # 抬起安全位
    DOWN   = 1  # 下潜取件位
    MOVING = 2  # 升降动作中

    @classmethod
    def get_desc(cls, val: int) -> str:
        names = {cls.UP: "抬起安全位", cls.DOWN: "下潜取件位", cls.MOVING: "升降动作中"}
        return names.get(val, f"Z轴({val})")


class TrayState(IntEnum):
    """上行托盘当前状态 (Byte 29)"""
    IDLE   = 0  # 空闲原点
    MOVING = 1  # 震动/旋转中

    @classmethod
    def get_desc(cls, val: int) -> str:
        names = {cls.IDLE: "空闲原点", cls.MOVING: "动作中"}
        return names.get(val, f"托盘({val})")


class StatusFlags(IntFlag):
    """
    上行系统运行标志位 (Byte 4)
    严格对齐 host_comm.h: HOST_UF_*
    """
    NONE      = 0x00
    TASK_DONE = 0x02  # 任务刚完成事件跳变 (HOST_UF_TASK_DONE)
    RESULT    = 0x04  # 任务结果字段有效 (HOST_UF_RESULT)
    ESTOP     = 0x08  # 处于急停锁存状态 (HOST_UF_ESTOP)
    ALARM     = 0x10  # 存在未清除的报警 (HOST_UF_ALARM)
    BUSY      = 0x20  # 机构运动或动作中 (HOST_UF_BUSY)
    USB_OK    = 0x40  # USB CDC 枚举与链路就绪 (HOST_UF_USB_OK)


class SysState(IntEnum):
    """
    系统主状态 (Byte 19)
    对应 host_comm.h: HOST_STATE_*
    """
    IDLE      = 0  # 待机空闲 (HOST_STATE_IDLE)
    BUSY      = 1  # 纯动作中 / 点动 / 单轴运动 (HOST_STATE_BUSY)
    RUNNING   = 2  # 分拣任务执行中 (HOST_STATE_RUNNING)
    PAUSED    = 3  # 暂停 (HOST_STATE_PAUSED)
    FAULT     = 4  # 故障报警锁存 (HOST_STATE_FAULT)
    ESTOP     = 5  # 急停锁存 (HOST_STATE_ESTOP)

    # 兼容历史别名
    ALARM     = 4
    TASK_DONE = 6

    @classmethod
    def get_desc(cls, val: int) -> str:
        names = {
            cls.IDLE: "待机空闲 (IDLE)",
            cls.BUSY: "动作执行中 (BUSY)",
            cls.RUNNING: "任务执行中 (RUNNING)",
            cls.PAUSED: "暂停 (PAUSED)",
            cls.FAULT: "故障报警 (FAULT)",
            cls.ESTOP: "急停锁存 (ESTOP)",
            cls.TASK_DONE: "任务完成 (TASK_DONE)",
        }
        return names.get(val, f"状态 ({val})")


class GripperState(IntEnum):
    """
    夹爪当前状态 (Byte 20)
    严格对齐下位机 host_comm.h: HOST_GRIP_ST_*
    """
    OPEN     = 0  # 张开到位 (HOST_GRIP_ST_OPEN)
    CLOSED   = 1  # 闭合到位但力=0(未夹持) (HOST_GRIP_ST_CLOSED)
    MOVING   = 2  # 闭合动作中 (HOST_GRIP_ST_MOVING)
    HELD     = 3  # 夹住, 力>0 (HOST_GRIP_ST_HELD)
    OPENING  = 4  # 张开动作中 (HOST_GRIP_ST_OPENING)
    EMPTY    = 5  # 空夹/未夹到 (HOST_GRIP_ST_EMPTY)
    SLIP     = 6  # 夹取脱落 (HOST_GRIP_ST_SLIP)

    # 兼容历史别名
    OPENED   = 0
    HOLDING  = 3
    FAULT    = 5
    UNKNOWN  = 255

    @classmethod
    def get_desc(cls, val: int) -> str:
        names = {
            cls.OPEN: "张开到位 (OPEN)",
            cls.CLOSED: "闭合到位未夹持 (CLOSED)",
            cls.MOVING: "闭合动作中 (MOVING)",
            cls.HELD: "夹持保持 (HELD)",
            cls.OPENING: "张开动作中 (OPENING)",
            cls.EMPTY: "空夹/未夹到 (EMPTY)",
            cls.SLIP: "夹取脱落 (SLIP)",
            cls.UNKNOWN: "未知状态",
        }
        return names.get(val, f"状态 ({val})")


class ResFlags(IntFlag):
    """
    任务结果标志位 (Byte 26)
    对应 host_comm.h: HOST_RES_*
    """
    NONE  = 0x00
    DONE  = 0x01  # 任务已完结 (HOST_RES_DONE)
    OK    = 0x02  # 任务成功完成 (HOST_RES_OK)
    PICK  = 0x04  # 已到达取件点，等待许可 (HOST_RES_PICK)
    ABORT = 0x80  # 任务中止 (急停/超时/取消, HOST_RES_ABORT)


class ResStage(IntEnum):
    """
    任务所处/失败阶段 (Byte 27)
    严格对齐下位机 host_comm.h: HOST_STAGE_*
    """
    NONE        = 0  # 无/不适用 (HOST_STAGE_NONE)
    PICK        = 1  # 去取件位 (HOST_STAGE_PICK)
    GRIP        = 2  # 夹取 (HOST_STAGE_GRIP)
    TRANSPORT   = 3  # 运输 (HOST_STAGE_TRANSPORT)
    PLACEGO     = 4  # 去放置位 (HOST_STAGE_PLACEGO)
    PLACE       = 5  # 放置 (HOST_STAGE_PLACE)
    RETURN      = 6  # 返回 (HOST_STAGE_RETURN)
    ABORT       = 7  # 急停/超时中止 (HOST_STAGE_ABORT)

    # 兼容历史别名
    APPROACH    = 1
    WAIT_PERMIT = 1  # 兼容: 取件位等待许可阶段通过 RES_FLAGS & 0x04 触发
    GRASP       = 2
    RELEASE     = 5

    @classmethod
    def get_desc(cls, val: int) -> str:
        names = {
            cls.NONE: "无",
            cls.PICK: "去取件位",
            cls.GRIP: "夹取阶段",
            cls.TRANSPORT: "运输中",
            cls.PLACEGO: "前往放置位",
            cls.PLACE: "放置阶段",
            cls.RETURN: "返回原点",
            cls.ABORT: "急停/超时中止",
        }
        return names.get(val, f"阶段{val}")


class ErrorMask(IntFlag):
    """
    错误码掩码定义 (Byte 21-22, 16位小端)
    严格对齐下位机 host_comm.h: HOST_ERR_* 与 HOST_COMM_GUIDE.md
    """
    NONE        = 0x0000
    PROTO       = 0x0001  # 协议错误 (VER/TYPE 不识别), 帧被拒 (HOST_ERR_PROTO)
    PARAM       = 0x0002  # 参数/字段越界 (坐标越界/GRIP_CMD>2), 帧被拒 (HOST_ERR_PARAM)
    QUEUE       = 0x0004  # 命令队列满/运动规划队满, 被拒 (HOST_ERR_QUEUE)
    BUSY        = 0x0004  # 别名: 系统忙 / 队列满
    TIMEOUT     = 0x0008  # 命令 TIMEOUT 超时, 已停运动 (HOST_ERR_TIMEOUT)
    NOTREACH    = 0x0010  # 未到目标 (运动超时/取消导致未到位) (HOST_ERR_NOTREACH)
    GRIPFAIL    = 0x0020  # 夹取失败/抓空 (HOST_ERR_GRIPFAIL)
    GRIPSLIP    = 0x0040  # 夹取脱落 (HOST_ERR_GRIPSLIP)
    PLACEFAIL   = 0x0080  # 放置失败 (HOST_ERR_PLACEFAIL)
    ESTOP       = 0x0100  # 急停锁存 (HOST_ERR_ESTOP)
    INTERNAL    = 0x0200  # 内部故障 (运动系统 FAULT/通信异常) (HOST_ERR_INTERNAL)
    COMM        = 0x0400  # 通信异常/掉线恢复 (HOST_ERR_COMM)
    FOREIGN_OBJ = 0x0800  # 异物检测报警 (HOST_ERR_FOREIGN_OBJ)

    # 兼容历史别名
    DROP        = 0x0040  # 映射为夹取脱落
    COLLISION   = 0x0800  # 映射为异物检测/碰撞

    @classmethod
    def get_descriptions(cls, err: int) -> List[str]:
        if err == 0:
            return ["无故障 (OK)"]
        desc_map = {
            cls.PROTO: "协议错误(SOF/版本/类型异常)",
            cls.PARAM: "参数超限(坐标/速度越界)",
            cls.QUEUE: "命令队列满/系统忙",
            cls.TIMEOUT: "动作或等待许可超时",
            cls.NOTREACH: "未到目标(运动超时/取消未到位)",
            cls.GRIPFAIL: "抓取失败/抓空",
            cls.GRIPSLIP: "工件夹取脱落",
            cls.PLACEFAIL: "工件放置失败",
            cls.ESTOP: "急停锁存中指令被拒",
            cls.INTERNAL: "内部硬件/驱动器故障",
            cls.COMM: "通信掉线/链路看门狗触发",
            cls.FOREIGN_OBJ: "异物检测报警",
        }
        res = []
        for bit, text in desc_map.items():
            if (err & bit.value) == bit.value:
                res.append(text)
        return res or [f"未知故障码 (0x{err:04X})"]


# ==============================================================================
# 数据载荷模型
# ==============================================================================

@dataclass
class HostCmd:
    """下行命令帧 (Host -> STM32)"""
    flags: int = CmdFlags.NONE
    seq: int = 0
    task_id: int = 0
    target_x_um: int = 0
    target_y_um: int = 0
    grip_cmd: int = GripCmd.HOLD
    timeout_ms: int = 0
    z_cmd: int = ZCmd.HOLD           # Byte 22: Z轴指令 (0=保持, 1=抬起, 2=下潜)
    tray_cmd: int = TrayCmd.HOLD     # Byte 23: 托盘指令 (0=保持, 1=震动, 2=复位)
    storage_cmd: int = StorageCmd.HOLD # Byte 24: 储物格指令 (0=保持, 1=逆时针+60°, 2=顺时针-60°, 3=转到槽位)
    storage_param: int = 0           # Byte 25: 储物格参数 (目标槽位 0~5)

    @property
    def target_x_mm(self) -> float:
        return self.target_x_um / 1000.0

    @target_x_mm.setter
    def target_x_mm(self, val: float):
        self.target_x_um = int(round(val * 1000.0))

    @property
    def target_y_mm(self) -> float:
        return self.target_y_um / 1000.0

    @target_y_mm.setter
    def target_y_mm(self, val: float):
        self.target_y_um = int(round(val * 1000.0))

    def pack(self) -> bytes:
        """打包为 32 字节下行二进制帧"""
        return pack_cmd(self)


@dataclass
class HostStatus:
    """上行状态帧 (STM32 -> Host)"""
    flags: int = 0
    seq: int = 0
    task_id: int = 0
    cur_x_um: int = 0
    cur_y_um: int = 0
    state: int = SysState.IDLE
    gripper: int = GripperState.OPEN
    error: int = 0
    ack_seq: int = 0
    progress: int = 0
    res_flags: int = 0
    res_stage: int = 0
    z_state: int = ZState.UP         # Byte 28: Z轴状态 (0=抬起, 1=下潜, 2=动作中)
    tray_state: int = TrayState.IDLE # Byte 29: 托盘状态 (0=空闲/原点, 1=动作中)
    storage_slot: int = 0            # Byte 30: 储物盘当前槽位 (0~5)
    raw_bytes: bytes = field(default_factory=bytes, repr=False)

    @property
    def cur_x_mm(self) -> float:
        return self.cur_x_um / 1000.0

    @property
    def cur_y_mm(self) -> float:
        return self.cur_y_um / 1000.0

    @property
    def z_desc(self) -> str:
        return ZState.get_desc(self.z_state)

    @property
    def tray_desc(self) -> str:
        return TrayState.get_desc(self.tray_state)

    @property
    def storage_desc(self) -> str:
        return f"槽位 {self.storage_slot}"

    # 快捷标志与业务阶段判定
    @property
    def is_busy(self) -> bool:
        """系统是否处于动作或运行状态"""
        return bool(self.flags & StatusFlags.BUSY) or (self.state in (SysState.BUSY, SysState.RUNNING))

    @property
    def is_alarm(self) -> bool:
        """是否存在故障报警或故障停机"""
        return bool(self.flags & StatusFlags.ALARM) or (self.error != 0) or (self.state == SysState.FAULT)

    @property
    def is_estop(self) -> bool:
        """是否处于急停状态"""
        return bool(self.flags & StatusFlags.ESTOP) or (self.state == SysState.ESTOP)

    @property
    def is_usb_ok(self) -> bool:
        """USB CDC 枚举链路是否就绪"""
        return bool(self.flags & StatusFlags.USB_OK)

    @property
    def is_task_done(self) -> bool:
        """任务是否已了结 (成功、失败或中止)"""
        return bool(self.flags & StatusFlags.TASK_DONE) or bool(self.res_flags & ResFlags.DONE)

    @property
    def is_pick_ready(self) -> bool:
        """是否已到达取件位并就绪 (严格以 RES_FLAGS & ResFlags.PICK 为准)"""
        return bool(self.res_flags & ResFlags.PICK)

    @property
    def is_permit_required(self) -> bool:
        """是否到达取件位并正等待上位机抓取许可 (严格基于下位机上报的 RES_FLAGS & ResFlags.PICK 标志位)"""
        return bool(self.res_flags & ResFlags.PICK)

    @property
    def is_success(self) -> bool:
        """任务是否圆满成功 (已了结且带有 OK 标志且无异常中止)"""
        return bool((self.res_flags & ResFlags.DONE) and (self.res_flags & ResFlags.OK) and not (self.res_flags & ResFlags.ABORT) and (self.error == 0))

    @property
    def is_grip_fail(self) -> bool:
        """是否发生抓空/抓取失败报警"""
        return bool(self.error & ErrorMask.GRIPFAIL)

    @property
    def is_aborted(self) -> bool:
        """任务是否异常中止 (急停/超时/取消)"""
        return bool(self.res_flags & ResFlags.ABORT) or (self.res_stage == ResStage.ABORT)

    @property
    def error_desc(self) -> str:
        return " | ".join(ErrorMask.get_descriptions(self.error))

    @property
    def state_desc(self) -> str:
        return SysState.get_desc(self.state)

    @property
    def gripper_desc(self) -> str:
        return GripperState.get_desc(self.gripper)

    @property
    def stage_desc(self) -> str:
        return ResStage.get_desc(self.res_stage)


# ==============================================================================
# 打包与解包实现 (结构体字节布局，严格定长 32 字节)
# ==============================================================================

# 下行帧格式: 2B SOF, 1B VER, 1B TYPE, 1B FLAGS, 2B SEQ, 4B TASK_ID, 4B X, 4B Y, 1B GRIP, 2B TIMEOUT, 1B Z_CMD, 1B TRAY_CMD, 1B STORAGE_CMD, 1B STORAGE_PARAM, 6B RESERVED
_CMD_STRUCT = struct.Struct("<2sBBBHIiiBHBBBB6s")

# 上行帧格式: 2B SOF, 1B VER, 1B TYPE, 1B FLAGS, 2B SEQ, 4B TASK_ID, 4B X, 4B Y, 1B STATE, 1B GRIPPER, 2B ERROR, 2B ACK_SEQ, 1B PROG, 1B RES_FLAGS, 1B RES_STAGE, 1B Z_STATE, 1B TRAY_STATE, 1B STORAGE_SLOT, 1B RESERVED
_STATUS_STRUCT = struct.Struct("<2sBBBHIiiBBHHBBBBBB1s")


def pack_cmd(cmd: HostCmd) -> bytes:
    """将 HostCmd 实例打包为 32 字节定长指令帧"""
    return _CMD_STRUCT.pack(
        HOST_SOF,
        HOST_VER,
        HOST_TYPE_CMD,
        int(cmd.flags),
        int(cmd.seq) & 0xFFFF,
        int(cmd.task_id) & 0xFFFFFFFF,
        int(cmd.target_x_um),
        int(cmd.target_y_um),
        int(cmd.grip_cmd) & 0xFF,
        int(cmd.timeout_ms) & 0xFFFF,
        int(cmd.z_cmd) & 0xFF,
        int(cmd.tray_cmd) & 0xFF,
        int(cmd.storage_cmd) & 0xFF,
        int(cmd.storage_param) & 0xFF,
        b"\x00" * 6
    )


def unpack_status(data: bytes) -> Optional[HostStatus]:
    """
    解包 32 字节数据为 HostStatus 对象
    若数据长度不足、同步头不匹配或版本/类型错误，返回 None
    """
    if len(data) != HOST_FRAME_LEN:
        return None

    try:
        (
            sof, ver, frame_type, flags, seq,
            task_id, cur_x, cur_y, state, gripper,
            error, ack_seq, progress, res_flags,
            res_stage, z_state, tray_state, storage_slot,
            _reserved
        ) = _STATUS_STRUCT.unpack(data)
    except struct.error:
        return None

    if sof != HOST_SOF or ver != HOST_VER or frame_type != HOST_TYPE_STATUS:
        return None

    return HostStatus(
        flags=flags,
        seq=seq,
        task_id=task_id,
        cur_x_um=cur_x,
        cur_y_um=cur_y,
        state=state,
        gripper=gripper,
        error=error,
        ack_seq=ack_seq,
        progress=progress,
        res_flags=res_flags,
        res_stage=res_stage,
        z_state=z_state,
        tray_state=tray_state,
        storage_slot=storage_slot,
        raw_bytes=data,
    )


# ==============================================================================
# 便捷命令帧构造函数
# ==============================================================================

def build_z_cmd(seq: int, z_cmd: int) -> bytes:
    """构造 Z 轴升降动作指令帧 (1=抬起, 2=下潜)"""
    cmd = HostCmd(
        flags=CmdFlags.Z_MOTION,
        seq=seq,
        task_id=0,
        z_cmd=z_cmd,
        timeout_ms=5000
    )
    return cmd.pack()


def build_tray_cmd(seq: int, tray_cmd: int) -> bytes:
    """构造托盘控制指令帧 (1=震动/翻转, 2=复位原点)"""
    cmd = HostCmd(
        flags=CmdFlags.AUX,
        seq=seq,
        task_id=0,
        tray_cmd=tray_cmd,
        timeout_ms=5000
    )
    return cmd.pack()


def build_storage_cmd(seq: int, storage_cmd: int, param: int = 0) -> bytes:
    """构造储物格旋转指令帧 (1=逆时针+60°, 2=顺时针-60°, 3=转到槽位)"""
    cmd = HostCmd(
        flags=CmdFlags.AUX,
        seq=seq,
        task_id=0,
        storage_cmd=storage_cmd,
        storage_param=param,
        timeout_ms=10000
    )
    return cmd.pack()

def build_move_cmd(seq: int, task_id: int, x_mm: float, y_mm: float, timeout_ms: int = 10000) -> bytes:
    """构造移动指令帧 (前往指定毫米坐标)"""
    cmd = HostCmd(
        flags=CmdFlags.MOTION,
        seq=seq,
        task_id=task_id,
        grip_cmd=GripCmd.HOLD,
        timeout_ms=timeout_ms
    )
    cmd.target_x_mm = x_mm
    cmd.target_y_mm = y_mm
    return cmd.pack()


def build_permit_cmd(seq: int, task_id: int) -> bytes:
    """构造抓取许可帧 (通知下位机可以下潜抓取)"""
    cmd = HostCmd(
        flags=CmdFlags.PERMIT,
        seq=seq,
        task_id=task_id,
        target_x_um=0,
        target_y_um=0,
        grip_cmd=GripCmd.HOLD,
        timeout_ms=0
    )
    return cmd.pack()


def build_estop_cmd(seq: int) -> bytes:
    """构造急停指令帧"""
    cmd = HostCmd(
        flags=CmdFlags.ESTOP,
        seq=seq,
        task_id=0,
        target_x_um=0,
        target_y_um=0,
        grip_cmd=GripCmd.HOLD,
        timeout_ms=0
    )
    return cmd.pack()


def build_clear_cmd(seq: int) -> bytes:
    """构造清除报警与复位指令帧"""
    cmd = HostCmd(
        flags=CmdFlags.CLEAR,
        seq=seq,
        task_id=0,
        target_x_um=0,
        target_y_um=0,
        grip_cmd=GripCmd.HOLD,
        timeout_ms=0
    )
    return cmd.pack()


def build_cancel_cmd(seq: int, task_id: int = 0) -> bytes:
    """构造取消当前在途任务指令帧"""
    cmd = HostCmd(
        flags=CmdFlags.CANCEL,
        seq=seq,
        task_id=task_id,
        target_x_um=0,
        target_y_um=0,
        grip_cmd=GripCmd.HOLD,
        timeout_ms=0
    )
    return cmd.pack()


def build_grip_cmd(seq: int, grip_cmd: int) -> bytes:
    """构造直接夹爪动作指令帧 (张开/闭合)"""
    cmd = HostCmd(
        flags=CmdFlags.GRIP,
        seq=seq,
        task_id=0,
        target_x_um=0,
        target_y_um=0,
        grip_cmd=grip_cmd,
        timeout_ms=3000
    )
    return cmd.pack()


def build_poll_cmd(seq: int) -> bytes:
    """构造空查询心跳指令帧 (flags=0)"""
    cmd = HostCmd(
        flags=CmdFlags.NONE,
        seq=seq,
        task_id=0,
        target_x_um=0,
        target_y_um=0,
        grip_cmd=GripCmd.HOLD,
        timeout_ms=0
    )
    return cmd.pack()


# ==============================================================================
# 字节流组帧器 (解决串口分包、粘包、垃圾字节恢复)
# ==============================================================================

class StreamFramer:
    """
    串口流式定长 32 字节组帧器
    特点:
    - 快速定位 0xAA 0x55 同步头
    - 自动丢弃前置无效/垃圾字节
    - 支持半包跨数据包拼接
    - 验证版本与帧类型有效性
    """

    def __init__(self, on_frame: Optional[Callable[[HostStatus], None]] = None):
        self._buffer = bytearray()
        self.on_frame = on_frame
        self.rx_frames: int = 0
        self.rx_bad_bytes: int = 0

    def feed(self, data: bytes) -> List[HostStatus]:
        """送入新收到的字节切片，返回解析出的所有完整 HostStatus 列表"""
        self._buffer.extend(data)
        results: List[HostStatus] = []

        while len(self._buffer) >= HOST_FRAME_LEN:
            # 寻找 0xAA 0x55
            idx = self._buffer.find(HOST_SOF)
            if idx < 0:
                # 缓冲区中完全没有同步头，保留最后一个字节(防止 AA 恰好在末尾)
                discard_len = len(self._buffer) - 1 if len(self._buffer) > 0 else 0
                self.rx_bad_bytes += discard_len
                if discard_len > 0:
                    del self._buffer[:discard_len]
                break

            if idx > 0:
                # 丢弃同步头之前的垃圾字节
                self.rx_bad_bytes += idx
                del self._buffer[:idx]

            # 此时 buffer[0:2] 必为 0xAA 0x55
            if len(self._buffer) < HOST_FRAME_LEN:
                break

            candidate = bytes(self._buffer[:HOST_FRAME_LEN])
            status = unpack_status(candidate)

            if status is not None:
                del self._buffer[:HOST_FRAME_LEN]
                self.rx_frames += 1
                results.append(status)
                if self.on_frame:
                    try:
                        self.on_frame(status)
                    except Exception:
                        pass
            else:
                # 不是合法的状态帧(可能是误匹配到内部数据)，前进1字节继续搜索
                self.rx_bad_bytes += 1
                del self._buffer[0:1]

        return results

    def reset(self):
        """清空内部缓冲区"""
        self._buffer.clear()
