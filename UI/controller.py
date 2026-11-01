# UI/controller.py
"""
单件分拣动作 —— 走下位机固件的闭环任务流程

旧版问题 (会导致真机全流程失败):
  旧版先 send_move() 再手动 z_down/grip_close/z_up, 再 send_move(80,0) 去放置。
  但按协议 (communication/mock_device.py 与 execute_sorting_task_ex 一致),
  MOTION 帧本身就开启一个"分拣任务": 下位机去取件位 -> 置 RES_PICK 等待 PERMIT ->
  自动下潜夹取 -> 运送 -> 放置 -> 归位 -> 上报 DONE|OK。
  旧版从不发 PERMIT, 下位机一直停在取件位等待; 第二个 send_move 又开了一个新任务。
  另外全部用 time.sleep 硬等, 既慢又不知道是否抓空。

本版:
  1. 先 GOTO_SLOT 把储物盘转到目标格 (0~5), 它与机械臂取件并行, 不额外占时间;
  2. 调用通讯模块现成的 execute_sorting_task_ex 闭环 (等到位 -> PERMIT -> 等完成),
     能区分 成功 / 抓空 / 急停 / 超时;
  3. 总超时 < 15s (赛规: 超过 15 秒无动作或无显示本轮结束)。
"""

from __future__ import annotations

import logging
import os
import time

from UI.interfaces import ControllerBase, SortResult

logger = logging.getLogger(__name__)


class CompetitionController(ControllerBase):
    def __init__(self, client, timeout_s: float = None):
        self.client = client
        self.timeout_s = float(timeout_s or os.environ.get("SORT_TIMEOUT_S", 12.0))
        self._task_id = 0
        #: 最近一次装置动作的时间 (赛规 15s 无动作判负, 比赛流程据此做"动作看门狗")
        self.last_motion = time.time()
        self.cancel_flag = False

    def sort_one(self, x_mm: float, y_mm: float, slot: int, on_stage=None) -> SortResult:
        """
        slot: 0~5 (协议槽位号)。
        on_stage(res_stage:int): 可选, 任务执行中下位机阶段变化时回调 (见 protocol.ResStage:
            1 去取件 2 抓取 3 运输 4 去放置位 5 放置 6 返回)。比赛流程用它在机械臂离开托盘视野后
            提前扫描下一件 (PREFETCH_STAGE)。回调里不要做耗时操作。
        """
        t0 = time.time()
        self.last_motion = t0
        self._task_id = (self._task_id % 250) + 1
        last = {"stage": -1}

        def progress(_msg, _pct):
            self.last_motion = time.time()
            if on_stage is None:
                return
            st = getattr(self.client, "latest_status", None)
            stage = int(getattr(st, "res_stage", -1)) if st is not None else -1
            if stage != last["stage"]:
                last["stage"] = stage
                try:
                    on_stage(stage)
                except Exception:
                    logger.exception("[Controller] on_stage 回调异常")

        try:
            # GOTO_SLOT 是绝对槽位, 重发无副作用; send_cmd 内部已重试, 这里再整体补发一次。
            # 仍无 ACK 就放弃本件: 盘没转到位就抓放 = 放错盒 (直接丢分), 宁可不放。
            if not self.client.send_storage_slot(int(slot) % 6):
                self.last_motion = time.time()
                logger.warning("[Controller] 储物盘转位无 ACK, 补发一次")
                if not self.client.send_storage_slot(int(slot) % 6):
                    logger.error("[Controller] 储物盘转位仍无 ACK, 放弃本件 (不抓取, 避免放错盒)")
                    return SortResult(False, message="储物盘转位无 ACK, 未抓取", slot_fail=True,
                                      elapsed_s=time.time() - t0)
            res = self.client.execute_sorting_task_ex(
                task_id=self._task_id, target_x_mm=float(x_mm), target_y_mm=float(y_mm),
                timeout_s=self.timeout_s, permit_delay_s=0.0, progress_callback=progress,
                cancel_token=lambda: self.cancel_flag)
        except Exception as e:
            logger.exception("[Controller] 动作异常")
            return SortResult(False, message=str(e), elapsed_s=time.time() - t0)
        finally:
            self.last_motion = time.time()

        out = SortResult(success=res.success, grip_fail=res.is_grip_fail,
                         fatal=res.is_estop, message=res.message, elapsed_s=time.time() - t0)
        if not res.success:
            logger.warning("[Controller] 任务 %d 失败: %s", self._task_id, res.message)
            if not res.is_estop:
                try:
                    self.client.send_clear()          # 清抓空等报警, 便于下一件继续
                except Exception:
                    pass
        return out

    def shake_tray(self, settle_s: float = 0.8) -> None:
        """托盘抖料, 让叠放/贴边/倒下的货物散开后重扫。"""
        self.last_motion = time.time()
        try:
            self.client.send_tray_shake()
            time.sleep(settle_s)
            self.client.send_tray_home()
            time.sleep(0.3)
            self.last_motion = time.time()
        except Exception as e:
            logger.warning("[Controller] 抖料失败: %s", e)
