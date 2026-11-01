"""
STM32 智能分拣系统 - 比赛流程模块接口契约 (Competition Interfaces)
================================================================
本文件是 **比赛流程 (UI/) 与其余模块之间的契约**。

------------------------------------------------------------------------------
1. 分层结构
------------------------------------------------------------------------------
    run_competition.py   一键启动主循环: 扫描 -> 选件 -> 分盒 -> 抓放 -> 显示
    planner.py           PlannerBase 实现: 类别 -> 储物盒 映射 + 选件策略
    controller.py        ControllerBase 实现: 单件闭环抓放 (走通讯模块)
    display_ui.py        ScreenBase 实现: 高亮显示屏 (序号/名称/图片/总数)
    remote_screen_client.py  大屏 HTTP 推送客户端 (display_ui 内部使用)

------------------------------------------------------------------------------
2. 对其他模块的依赖 (接替方必须保证)
------------------------------------------------------------------------------
    视觉: VisionServiceBase.scan(n_frames) -> (List[VisionTarget], frame)
          每个 VisionTarget 需要: x_mm, y_mm, u_pixel, v_pixel, confidence,
          label (大屏中文名), category (形状|颜色|信息), bbox, hits
    通讯: 见下方 SortingClientLike —— 内置 IntelligentSortingClient 全部满足。
          注意 send_storage_slot / send_tray_shake / send_tray_home /
          execute_sorting_task_ex 不在 SortingClientBase 抽象方法里,
          若替换通讯后端必须一并提供。

------------------------------------------------------------------------------
3. 赛规约束 (2027 工创 智能分拣, PDF 第 10~12 页)
------------------------------------------------------------------------------
    * 形状、颜色、信息完全一致的货物进同一储物盒, 共 6 个盒 (槽位 0~5)。
    * 每分拣一件, 显示屏须显示: 序号 / 货物名称 / 图片 / 分拣成功总数量。
    * 超过 15 秒无动作或未显示本次分拣信息, 本轮结束 -> 单件超时须 < 15s。
    * 显示屏只能显示, 不得出现操作/调试界面。
    * 裁判下令后"一键启动", 运行中不能触碰笔记本、不能遥控 -> start_trigger.py (USB 键/GPIO/执行页按钮)。

------------------------------------------------------------------------------
4. 比赛运行时组件 (2026-10 新增)
------------------------------------------------------------------------------
    start_trigger.py     StartTrigger: 一键启动 (USB 键 / GPIO / 执行页按钮 / 终端回车)
    run_page.py          执行页 http://<X5>:8088 —— 状态/流程/错误/开始/急停/停止/一键重启
    run_report.py        每轮复盘报告 records/runs/<时间>/
    precheck.py          赛前自检清单 (相机/BPU/模型/标定/串口/大屏/启动键)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple, runtime_checkable

from MessegeCenter.topics import VisionTarget

#: 储物盒数量 (赛规)
NUM_SLOTS = 6
#: 赛规: 无动作/无显示超时
RULE_IDLE_LIMIT_S = 15.0


# ==============================================================================
# 数据契约
# ==============================================================================

@dataclass
class SortResult:
    """单件分拣结果"""
    success: bool
    grip_fail: bool = False      # 抓空: 换一件或重扫
    fatal: bool = False          # 急停等不可继续的情况: 结束本轮
    message: str = ""
    elapsed_s: float = 0.0
    slot_fail: bool = False      # 储物盘转位未确认, 本件没动 (不是货物的问题, 不计入该位置的失败次数)


# ==============================================================================
# 对通讯模块的最小依赖 (结构化类型, 无需继承)
# ==============================================================================

@runtime_checkable
class SortingClientLike(Protocol):
    def connect(self, port: Optional[str] = None, baudrate: int = 115200) -> bool: ...
    def connect_mock(self) -> bool: ...
    def disconnect(self) -> None: ...
    def send_clear(self, wait_ack: bool = True) -> bool: ...
    def send_storage_slot(self, slot: int, wait_ack: bool = True) -> bool: ...
    def send_tray_shake(self, wait_ack: bool = True) -> bool: ...
    def send_tray_home(self, wait_ack: bool = True) -> bool: ...
    def execute_sorting_task_ex(self, task_id: int, target_x_mm: float, target_y_mm: float,
                                timeout_s: float = 15.0, permit_delay_s: float = 0.1,
                                **kwargs): ...


# ==============================================================================
# 抽象基类
# ==============================================================================

class PlannerBase(ABC):
    """类别 -> 储物盒 映射与选件策略。"""

    max_slots: int = NUM_SLOTS

    @abstractmethod
    def slot_for(self, target: VisionTarget) -> int:
        """返回该目标应放入的槽位 0~5。同一 category 在整轮比赛中必须恒定。"""

    @abstractmethod
    def pick_next(self, targets: List[VisionTarget],
                  skip: Optional[List[Tuple[float, float]]] = None) -> Optional[VisionTarget]:
        """从本次扫描结果中挑下一件; skip 为刚抓失败的像素位置, 应优先避开。"""

    def mark_done(self, target: VisionTarget) -> None:
        """一件分拣成功后调用 (统计用)。"""


class ControllerBase(ABC):
    """单件抓放执行器。"""

    #: 最近一次装置动作的时间戳 (time.time()), 比赛流程的"动作看门狗"读取
    last_motion: float = 0.0

    @abstractmethod
    def sort_one(self, x_mm: float, y_mm: float, slot: int, on_stage=None) -> SortResult:
        """把 (x_mm, y_mm) 处的货物放入 slot (0~5) 号储物盒, 阻塞到结束, 总时长须 < 15s。
        on_stage(res_stage) 可选: 下位机任务阶段变化时回调 (提前扫描下一件用)。"""

    def shake_tray(self, settle_s: float = 0.8) -> None:
        """托盘抖料 (可选)。"""


class ScreenBase(ABC):
    """装置上的高亮显示屏 (只显示)。"""

    @abstractmethod
    def update_status(self, seq, name, total, image_path: str = "", stage: str = "分拣中") -> None:
        """显示: 序号 / 货物名称 / 图片 / 分拣成功总数量。必须非阻塞。"""

    @abstractmethod
    def reset(self) -> None:
        """回到待机画面。"""

    def hold(self, seconds: float = 5.0) -> None:
        """保持当前画面 seconds 秒。"""

    def close(self) -> None:
        """释放资源。"""


__all__ = [
    "NUM_SLOTS", "RULE_IDLE_LIMIT_S", "SortResult", "SortingClientLike",
    "PlannerBase", "ControllerBase", "ScreenBase",
]
