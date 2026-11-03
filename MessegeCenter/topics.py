"""
STM32 智能分拣系统 - 消息中心主题定义与消息载荷模型
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Dict, List


# ==============================================================================
# 标准主题字符串常量 (Topics)
# ==============================================================================

# 下位机通讯主题 (Communication)
TOPIC_COMM_STATUS       = "comm/status"        # 下位机 20Hz 状态上报 (HostStatus)
TOPIC_COMM_CMD_SEND     = "comm/cmd/send"      # 请求下发下行命令 (HostCmd)
TOPIC_COMM_ESTOP        = "comm/estop"         # 紧急停机广播
TOPIC_COMM_CLEAR        = "comm/clear"         # 清除报警/复位
TOPIC_COMM_CONNECTION   = "comm/connection"    # 串口连接/断开状态变化

# 视觉处理主题 (Vision)
TOPIC_VISION_FRAME      = "vision/frame"       # 摄像头实时视频帧采集 (VisionFrame)
TOPIC_VISION_TARGET     = "vision/target"      # 视觉识别到的工件目标结果 (VisionTarget)
TOPIC_VISION_TRIGGER    = "vision/trigger"     # 触发视觉拍照与识别检测
TOPIC_VISION_CALIB      = "vision/calib"       # 标定参数更新通知

# 业务调度与任务流主题 (Task / Dispatch)
TOPIC_TASK_START        = "task/start"         # 启动分拣任务
TOPIC_TASK_PROGRESS     = "task/progress"      # 任务阶段进度更新
TOPIC_TASK_PERMIT       = "task/permit"        # 抓取许可信号交互
TOPIC_TASK_DONE         = "task/done"          # 任务完结通知 (成功/失败)
TOPIC_TASK_CANCEL       = "task/cancel"        # 取消任务

# 系统监控与告警主题 (Monitor)
TOPIC_MONITOR_HEARTBEAT = "monitor/heartbeat"  # 各模块心跳包 (ModuleHeartbeat)
TOPIC_MONITOR_ALERT     = "monitor/alert"      # 系统报警与告警 (SystemAlert)
TOPIC_MONITOR_METRICS   = "monitor/metrics"    # 系统资源性能指标 (SystemMetrics)

# 比赛信息主题 (Race) —— 信息处理中心 RaceLedger 汇总校验, 见 MessegeCenter/race.py
TOPIC_RACE_STATE        = "race/state"         # 一轮比赛状态 idle/running/finished (RaceState)
TOPIC_RACE_ITEM         = "race/item"          # 每件分拣结果 + 显示屏信息 (SortRecord)
TOPIC_RACE_EVENT        = "race/event"         # 流程事件 (dict{level,msg}), 也作为「有动作」心跳
TOPIC_RACE_RULE         = "race/rule"          # 赛规校验结果 (RaceRule)

# 通用系统日志主题 (Log)
TOPIC_SYS_LOG           = "sys/log"            # 全局调试与业务日志 (LogMessage)


# ==============================================================================
# 结构化消息载荷数据类
# ==============================================================================

@dataclass
class Message:
    """消息基础包装容器"""
    topic: str
    payload: Any
    sender: str = "unknown"
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())
    msg_id: int = 0

    @property
    def time_str(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S.%f")[:12]


@dataclass
class VisionTarget:
    """视觉识别到的目标工件信息"""
    target_id: int = 0
    color: str = "red"              # 颜色分类 (red, green, blue, yellow)
    shape: str = "cube"             # 形状 (cube, cylinder, sphere)
    confidence: float = 0.95        # 识别置信度
    u_pixel: float = 0.0            # 图像像素坐标 u
    v_pixel: float = 0.0            # 图像像素坐标 v
    x_mm: float = 0.0               # 转换后的物理机械臂 X 坐标 (mm)
    y_mm: float = 0.0               # 转换后的物理机械臂 Y 坐标 (mm)
    # ---- 以下为 YOLO 管线新增字段 (均有默认值, 旧代码不受影响) ----
    class_name: str = ""            # 模型类别 id (classes.json 中的 id)
    label: str = ""                 # 大屏显示用中文名, 如 "红色五棱柱"
    info: str = ""                  # 附加信息 (图案/文字/二维码内容), 参与分类
    category: str = ""              # 分拣类别键: 形状+颜色+信息 完全一致才同类
    bbox: tuple = ()                # (x1, y1, x2, y2) 像素
    angle_deg: float = 0.0          # 最小外接矩形角度, 可供夹爪旋转使用
    hits: int = 1                   # 多帧融合时出现的帧数
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())
    # ---- A1 旁路数据 (可选, 有默认值; 不参与对外契约, 原有 16 个字段名/语义未动) ----
    # 约定键: model_color / hsv_color / color_conflict / color_source / category_core /
    #         qr_decoder / qr_ms / qr_budget_exhausted
    diagnostics: dict = field(default_factory=dict)


@dataclass
class ModuleHeartbeat:
    """模块健康心跳包"""
    module_name: str                # 模块名称 (communication, vision, monitor, coordinator)
    status: str = "OK"              # OK, BUSY, WARNING, ERROR
    fps: float = 0.0                # 模块当前刷新速率 / 帧率
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())


@dataclass
class SystemAlert:
    """监控告警信息"""
    level: str                      # INFO, WARNING, ERROR, CRITICAL
    source: str                     # 告警来源模块
    code: int                       # 错误码
    message: str                    # 告警文字描述
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())


@dataclass
class LogMessage:
    """全局系统日志"""
    level: str                      # DEBUG, INFO, WARN, ERROR
    tag: str                        # 标签分类
    content: str                    # 日志文本
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())


@dataclass
class RaceState:
    """一轮比赛的状态变化 (race/state)"""
    state: str                      # idle | running | finished
    run_id: str = ""
    result: str = ""                # 完成 | 停止 | 急停 | 异常
    total_done: int = 0
    expected_total: int = 0
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())


@dataclass
class SortRecord:
    """一件货物的分拣结果, 也是显示屏要显示的内容 (race/item)"""
    seq: Optional[int] = None       # 显示序号 (成功才有)
    label: str = ""                 # 货物名称 (中文)
    category: str = ""              # 形状|颜色|信息
    slot: int = 0                   # 储物盒 1~6
    result: str = "ok"              # ok | grip_fail | fail | estop
    total: int = 0                  # 显示屏上的分拣成功总数量
    image: str = ""                 # 显示屏图片 (货物截图路径)
    conf: float = 0.0
    x_mm: float = 0.0
    y_mm: float = 0.0
    elapsed_s: float = 0.0
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())


@dataclass
class RaceRule:
    """赛规校验提示 (race/rule)"""
    code: str                       # same_category_two_slots / slot_mixed / slot_overflow / idle_15s ...
    level: str                      # warn | error
    message: str
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())
