"""
信息处理中心: 比赛信息账本 (RaceLedger) —— 按《智能分拣》赛项规则汇总、校验每一轮的分拣信息

赛规要点 (2027 智能分拣, 现场初赛) 与本模块的对应:
  * 一键启动, 计时开始                         -> race/state  running, 记录开始时间
  * 形状、颜色、信息完全一致才进同一个盒        -> category 键 (形状|颜色|信息) 与盒号一一对应校验
  * 6 个储物盒, 每盒不超过 4 件                 -> 第 7 类 / 单盒第 5 件 告警
  * 每分拣一件, 显示屏显示: 序号、货物名称、图片、
    分拣成功总数量, 显示完成后才能分拣下一件       -> race/item 字段齐全校验, 序号连续、总数 = 成功件数
  * 超过 15 秒没有动作或没有显示本次分拣信息, 本轮结束
                                                 -> 活动看门狗: 12s 预警, 15s 记违规 (只记录与告警,
                                                    动作由比赛主循环的看门狗保证)
  * 货物不少于 12 件、种类不少于 6 种 (初赛)      -> 结束时汇总, 少于期望件数给出提示

数据从哪里来: UI/run_report.py 在记录每件结果/事件时顺手发布到总线 (只发布, 不影响比赛流程);
谁在用: 监控模块 (告警)、调试界面 (python main.py --msg 查看 race/*)、赛后复盘 (snapshot)。
总线不可用时比赛照常进行 —— 本模块只是旁路的「记分员」。
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .topics import (
    TOPIC_MONITOR_ALERT, TOPIC_RACE_EVENT, TOPIC_RACE_ITEM, TOPIC_RACE_RULE, TOPIC_RACE_STATE,
    Message, RaceRule, RaceState, SortRecord, SystemAlert,
)

MAX_SLOTS = 6
MAX_PER_SLOT = 4
IDLE_LIMIT_S = 15.0
IDLE_WARN_S = 12.0


@dataclass
class SlotTally:
    slot: int
    category: str = ""
    label: str = ""
    count: int = 0


@dataclass
class RaceSnapshot:
    state: str = "idle"                 # idle | running | finished
    run_id: str = ""
    started_at: float = 0.0
    elapsed_s: float = 0.0
    total_done: int = 0                 # 分拣成功总数量 (= 显示屏上的总数)
    attempts: int = 0
    failures: int = 0
    last_seq: int = 0
    slots: Dict[int, SlotTally] = field(default_factory=dict)
    rules: List[RaceRule] = field(default_factory=list)
    result: str = ""
    expected_total: int = 0
    last_activity: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["slots"] = {str(k): asdict(v) for k, v in sorted(self.slots.items())}
        return d


class RaceLedger:
    """
    订阅 race/*, 维护本轮的权威计数, 违规/风险发布到 race/rule 和 monitor/alert。

        ledger = RaceLedger(bus); ledger.start()
        ...
        ledger.snapshot().to_dict()
    """

    def __init__(self, bus, idle_limit_s: float = IDLE_LIMIT_S, idle_warn_s: float = IDLE_WARN_S,
                 max_slots: int = MAX_SLOTS, max_per_slot: int = MAX_PER_SLOT):
        self.bus = bus
        self.idle_limit_s, self.idle_warn_s = idle_limit_s, idle_warn_s
        self.max_slots, self.max_per_slot = max_slots, max_per_slot
        self._lock = threading.RLock()
        self._snap = RaceSnapshot()
        self._cat_slot: Dict[str, int] = {}
        self._idle_flags = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._subs = [(TOPIC_RACE_STATE, self._on_state), (TOPIC_RACE_ITEM, self._on_item),
                      (TOPIC_RACE_EVENT, self._on_event)]

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> "RaceLedger":
        for t, cb in self._subs:
            self.bus.subscribe(t, cb)
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="RaceLedgerWatch", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        for t, cb in self._subs:
            try:
                self.bus.unsubscribe(t, cb)
            except Exception:
                pass

    def snapshot(self) -> RaceSnapshot:
        with self._lock:
            s = self._snap
            if s.state == "running":
                s.elapsed_s = round(time.time() - s.started_at, 1)
            return RaceSnapshot(**{**s.__dict__, "slots": {k: SlotTally(**v.__dict__) for k, v in s.slots.items()},
                                   "rules": list(s.rules)})

    # ------------------------------------------------------------------ 处理
    def _payload(self, msg):
        return msg.payload if isinstance(msg, Message) else msg

    def _on_state(self, msg):
        st = self._payload(msg)
        if isinstance(st, dict):
            st = RaceState(**{k: v for k, v in st.items() if k in RaceState.__dataclass_fields__})
        if not isinstance(st, RaceState):
            return
        with self._lock:
            if st.state == "running":
                self._snap = RaceSnapshot(state="running", run_id=st.run_id, started_at=st.timestamp,
                                          expected_total=st.expected_total, last_activity=st.timestamp)
                self._cat_slot.clear()
                self._idle_flags.clear()
                return
            if st.state == "finished" and self._snap.state == "running":
                s = self._snap
                s.state, s.result = "finished", st.result
                s.elapsed_s = round(st.timestamp - s.started_at, 1)
                if st.total_done and st.total_done != s.total_done:
                    self._rule("count_mismatch", "warn",
                               f"主循环报告成功 {st.total_done} 件, 账本记录 {s.total_done} 件, 显示屏总数可能不对")
                if s.expected_total and s.total_done < s.expected_total:
                    self._rule("incomplete", "warn", f"本轮成功 {s.total_done}/{s.expected_total} 件")
            elif st.state == "idle":
                self._snap.state = "idle" if self._snap.state != "finished" else "finished"

    def _on_event(self, msg):
        with self._lock:
            if self._snap.state == "running":
                self._snap.last_activity = time.time()
                self._idle_flags.clear()

    def _on_item(self, msg):
        rec = self._payload(msg)
        if isinstance(rec, dict):
            rec = SortRecord(**{k: v for k, v in rec.items() if k in SortRecord.__dataclass_fields__})
        if not isinstance(rec, SortRecord):
            return
        with self._lock:
            s = self._snap
            if s.state != "running":
                return
            s.last_activity = time.time()
            self._idle_flags.clear()
            s.attempts += 1
            if rec.result != "ok":
                s.failures += 1
                return
            # ---- 同类同盒 / 一盒一类
            cat = rec.category or rec.label
            if cat in self._cat_slot and self._cat_slot[cat] != rec.slot:
                self._rule("same_category_two_slots", "error",
                           f"{rec.label} 已放 {self._cat_slot[cat]} 号盒, 这次放进了 {rec.slot} 号盒")
            tally = s.slots.get(rec.slot)
            if tally and tally.category and tally.category != cat:
                self._rule("slot_mixed", "error",
                           f"{rec.slot} 号盒已有 {tally.label}, 又放入 {rec.label} (形状/颜色/信息不一致)")
            if cat not in self._cat_slot:
                self._cat_slot[cat] = rec.slot
                if len(self._cat_slot) > self.max_slots:
                    self._rule("too_many_categories", "error",
                               f"出现第 {len(self._cat_slot)} 类货物, 只有 {self.max_slots} 个盒")
            if tally is None:
                tally = s.slots[rec.slot] = SlotTally(slot=rec.slot, category=cat, label=rec.label)
            tally.count += 1
            if tally.count > self.max_per_slot:
                self._rule("slot_overflow", "warn",
                           f"{rec.slot} 号盒第 {tally.count} 件, 超过赛规保证容量 {self.max_per_slot} 件")
            # ---- 显示信息: 序号 / 名称 / 图片 / 成功总数
            s.total_done += 1
            if rec.seq and rec.seq != s.last_seq + 1:
                self._rule("seq_gap", "warn", f"显示序号 {rec.seq}, 上一件是 {s.last_seq}")
            s.last_seq = rec.seq or s.last_seq + 1
            missing = [n for n, v in (("货物名称", rec.label), ("图片", rec.image)) if not v]
            if missing:
                self._rule("display_incomplete", "warn", f"第 {s.last_seq} 件显示信息缺 {'/'.join(missing)}")
            if rec.total and rec.total != s.total_done:
                self._rule("total_mismatch", "warn", f"显示总数 {rec.total} ≠ 成功件数 {s.total_done}")

    def _rule(self, code: str, level: str, message: str):
        r = RaceRule(code=code, level=level, message=message)
        self._snap.rules.append(r)
        try:
            self.bus.publish(TOPIC_RACE_RULE, r, sender="race_ledger")
            self.bus.publish(TOPIC_MONITOR_ALERT,
                             SystemAlert(level="ERROR" if level == "error" else "WARNING",
                                         source="race", code=400, message=message), sender="race_ledger")
        except Exception:
            pass

    def _watch(self):
        while not self._stop.wait(0.5):
            with self._lock:
                s = self._snap
                if s.state != "running" or not s.last_activity:
                    continue
                idle = time.time() - s.last_activity
                if idle >= self.idle_limit_s and "limit" not in self._idle_flags:
                    self._idle_flags.add("limit")
                    self._rule("idle_15s", "error", f"{idle:.0f}s 没有动作/显示 (赛规 15s 判本轮结束)")
                elif idle >= self.idle_warn_s and not self._idle_flags:
                    self._idle_flags.add("warn")
                    self._rule("idle_warn", "warn", f"已 {idle:.0f}s 没有动作/显示")


# ============================================================================== 发布端便捷函数
def publish_state(bus, state: str, **kw):
    """比赛主循环 / 报告模块调用: state = idle | running | finished。"""
    try:
        bus.publish(TOPIC_RACE_STATE, RaceState(state=state, **kw), sender="race")
    except Exception:
        pass


def publish_item(bus, **kw):
    try:
        bus.publish(TOPIC_RACE_ITEM, SortRecord(**{k: v for k, v in kw.items()
                                                   if k in SortRecord.__dataclass_fields__}), sender="race")
    except Exception:
        pass


def publish_event(bus, msg: str, level: str = "info"):
    try:
        bus.publish(TOPIC_RACE_EVENT, {"level": level, "msg": msg}, sender="race")
    except Exception:
        pass
