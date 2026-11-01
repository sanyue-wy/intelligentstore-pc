# UI/planner.py
"""
分拣规划: 类别 -> 储物盒 的持久映射 + 每轮挑选下一个要抓的工件

规则 (PDF 第 11~12 页): 形状、颜色、信息完全一致的货物进同一个储物盒, 共 6 个盒。
旧版问题:
  * 每次 plan() 重新编号, 若中途重扫, 同一类可能被分到不同盒子 —— 直接丢分;
  * 槽位 1~6 直接传给 send_storage_slot(0~5 协议), 6 号被 %6 变成 0 号, 编号错位;
  * 一次扫描后按旧坐标连抓到底, 抓第一个时碰动了别的件也不知道。
本版: 类别首次出现时分配下一个空盒并永久记住; 每抓一件都重新扫描, 只挑当前最好抓的一件。

赛规相关的检查 (PDF 第 10~12 页):
  * 每个储物盒最多装 4 件 (赛规保证同类不超过 4 件) -> 某盒第 5 件时告警 (几乎肯定识别错了);
  * 共 6 个盒 -> 出现第 7 类时告警并并入最相近的盒;
  * 分错盒比慢一点更亏: assess() 判断目标是否"值得现在就抓"
    (置信度过低 / 与别的货物框重叠 / 帧数不足), 不值得时先抖料重扫。
"""

from __future__ import annotations

import logging
import math
import threading
from typing import Dict, List, Optional

from UI.interfaces import PlannerBase

logger = logging.getLogger(__name__)


_COLOR_CN = {"red": "红色", "orange": "橙色", "yellow": "黄色", "green": "绿色", "light_blue": "浅蓝",
             "blue": "蓝色", "purple": "紫色", "black": "黑色", "white": "白色", "gray": "灰色"}
_SHAPE_CN = {"cube": "正方体", "circle": "圆形", "cylinder": "圆柱体", "triangle": "三角形",
             "rectangle": "长方体", "sphere": "球体"}


def display_name(target) -> str:
    """大屏显示的货物名称: 视觉给的 label (例 "红色五棱柱"), 缺省时由颜色+形状拼出, 绝不为空。"""
    if getattr(target, "label", ""):
        return target.label
    c = _COLOR_CN.get(target.color, target.color or "")
    s = _SHAPE_CN.get(target.shape, target.shape or "货物")
    return (c + s) or "货物"


class SorterPlanner(PlannerBase):
    def __init__(self, max_slots: int = 6, preset: Optional[Dict[str, int]] = None):
        self.max_slots = max_slots
        self._lock = threading.RLock()     # 执行页 web 线程读 slots_summary, 主线程写
        self.slot_of: Dict[str, int] = dict(preset or {})   # category -> 0..5
        self.label_of: Dict[str, str] = {}
        self.count_of: Dict[str, int] = {}
        self.box_capacity = 4
        self.warnings: List[str] = []
        self.commit_conf = 0.45          # 低于此置信度先重扫/抖料 (比赛流程可覆盖)
        self.overlap_iou = 0.15          # 与其它货物框重叠超过此值视为叠放/紧贴

    # ------------------------------------------------------------------ 槽位
    @staticmethod
    def key_of(target) -> str:
        return target.category or f"{target.shape}|{target.color}|{target.info}"

    def slot_for(self, target) -> int:
        with self._lock:
            key = self.key_of(target)
            if key in self.slot_of:
                return self.slot_of[key]
            used = set(self.slot_of.values())
            free = [s for s in range(self.max_slots) if s not in used]
            if free:
                slot = free[0]
            else:
                # 第 7 类: 必然有识别错误。归到 形状+颜色 最接近的已有类别, 并告警
                slot = self._closest_slot(key)
                msg = f"类别数超过 {self.max_slots}: {target.label or key} 并入 {slot + 1} 号盒 (很可能识别错误)"
                logger.warning("[Planner] %s", msg)
                self._warn(msg)
            self.slot_of[key] = slot
            self.label_of[key] = display_name(target)
            logger.info("[Planner] 新类别 %s -> %d 号储物盒", key, slot + 1)
            return slot

    def _closest_slot(self, key: str) -> int:
        shape, color, _ = (key.split("|") + ["", "", ""])[:3]
        for k, s in self.slot_of.items():
            ks, kc, _ = (k.split("|") + ["", "", ""])[:3]
            if ks == shape and kc == color:
                return s
        for k, s in self.slot_of.items():
            if k.split("|")[0] == shape:
                return s
        return min(self.slot_of.values(), key=lambda s: sum(1 for v in self.slot_of.values() if v == s))

    def _warn(self, msg: str):
        if msg not in self.warnings:
            self.warnings.append(msg)

    def slot_count(self, slot: int) -> int:
        return sum(c for k, c in self.count_of.items() if self.slot_of.get(k) == slot)

    def mark_done(self, target):
        with self._lock:
            key = self.key_of(target)
            self.count_of[key] = self.count_of.get(key, 0) + 1
            slot = self.slot_of.get(key)
            if slot is not None and self.slot_count(slot) == self.box_capacity + 1:
                self._warn(f"{slot + 1} 号盒已超过 {self.box_capacity} 件 "
                           f"({self.label_of.get(key, key)}), 赛规每类不超过 4 件, 可能有识别错误")

    def slots_summary(self) -> Dict[str, dict]:
        with self._lock:
            out = {}
            for k, s in sorted(self.slot_of.items(), key=lambda kv: kv[1]):
                e = out.setdefault(str(s + 1), {"label": [], "count": 0})
                e["label"].append(self.label_of.get(k, k))
                e["count"] += self.count_of.get(k, 0)
            for e in out.values():
                e["label"] = " / ".join(e["label"])
            return out

    # ------------------------------------------------------------------ 风险评估
    @staticmethod
    def _iou(a, b) -> float:
        if not a or not b:
            return 0.0
        ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
        return inter / u if u > 0 else 0.0

    def assess(self, target, targets: List) -> Optional[str]:
        """返回 None = 可以抓; 否则返回不建议现在抓的原因 (调用方决定抖料重扫还是硬抓)。"""
        if target is None:
            return "无目标"
        if target.confidence < self.commit_conf:
            return f"置信度低 {target.confidence:.2f}"
        worst = max((self._iou(target.bbox, o.bbox) for o in targets if o is not target), default=0.0)
        if worst > self.overlap_iou:
            return f"与其它货物重叠 IoU={worst:.2f}"
        return None

    # ------------------------------------------------------------------ 选件
    def pick_next(self, targets: List, skip: Optional[List[tuple]] = None, skip_radius_px: float = 15.0,
                  blocked: Optional[List[tuple]] = None, blocked_radius_px: float = 25.0):
        """
        选下一件: 置信度 + 与邻居的间隙 (夹爪下去不碰别的件) 综合打分。
        skip: 刚刚抓失败过的像素位置, 先避开, 换一件抓 (全被 skip 时仍退回置信度最高的一件)。
        blocked: 同一位置反复失败 (可能不可达/卡住) 的像素位置, 永不再选; 全被 blocked 时返回 None。
        """
        blocked = blocked or []
        targets = [t for t in (targets or []) if not any(
            math.hypot(t.u_pixel - u, t.v_pixel - v) < blocked_radius_px for u, v in blocked)]
        if not targets:
            return None
        skip = skip or []
        best, best_score = None, -1e9
        for t in targets:
            if any(math.hypot(t.u_pixel - u, t.v_pixel - v) < skip_radius_px for u, v in skip):
                continue
            gap = min((math.hypot(t.u_pixel - o.u_pixel, t.v_pixel - o.v_pixel)
                       for o in targets if o is not t), default=999.0)
            ov = max((self._iou(t.bbox, o.bbox) for o in targets if o is not t), default=0.0)
            score = (t.confidence + 0.4 * min(1.0, gap / 90.0) + 0.05 * min(getattr(t, "hits", 1), 5)
                     - 1.0 * ov)
            if score > best_score:
                best, best_score = t, score
        return best if best is not None else max(targets, key=lambda t: t.confidence)
