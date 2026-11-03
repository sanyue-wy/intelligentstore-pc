# -*- coding: utf-8 -*-
"""
每轮比赛的复盘报告: records/runs/<时间>/report.json + 每件货物截图 + 每次识别的标注图

report.json:
    {"start": ..., "end": ..., "elapsed_s": ..., "total_done": 12, "result": "完成|急停|停止|异常",
     "items":  [{"seq", "t", "label", "category", "conf", "slot", "u", "v", "x_mm", "y_mm",
                 "result": "ok|grip_fail|fail|estop", "elapsed_s", "shot", "scan"}],
     "events": [{"t", "level", "msg"}],
     "slots":  {"1": {"label": "红色正方体", "count": 2}, ...},
     "model":  "...", "warnings": [...],
     "category_core_counts": {"形状|颜色|": 件数, ...},
     "color_conflict": {"by_detection": {...}, "by_piece": {...}}}   # A1.1 按类冲突率双口径
网页调试台「复盘」页直接浏览; 识别错的那张一键归档到回归集 (dataset/regression)。

同时把每轮状态/每件结果/事件旁路发布到消息总线 race/* (信息处理中心 RaceLedger 按赛规校验,
结果写进 report.json 的 "ledger"); 总线出任何问题都不影响比赛流程。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS_DIR = os.environ.get("IS_RUNS_DIR") or os.path.join(ROOT, "records", "runs")
KEEP_RUNS = int(os.environ.get("IS_RUNS_KEEP", 30))

_race = {"bus": None, "ledger": None}


def _race_bus():
    """全局总线 + 信息处理中心账本 (首次调用时启动); 不可用返回 None。"""
    if _race["bus"] is None:
        try:
            from MessegeCenter import get_message_bus, RaceLedger
            _race["bus"] = get_message_bus()
            _race["ledger"] = RaceLedger(_race["bus"]).start()
        except Exception:
            _race["bus"] = False
    return _race["bus"] or None


class RunReport:
    def __init__(self, base: str = RUNS_DIR, model: str = ""):
        self.t0 = time.time()
        self.dir = os.path.join(base, time.strftime("%Y%m%d_%H%M%S"))
        self.data: Dict[str, Any] = {"start": time.strftime("%Y-%m-%d %H:%M:%S"), "end": "",
                                     "elapsed_s": 0.0, "total_done": 0, "result": "进行中",
                                     "items": [], "events": [], "slots": {}, "model": model,
                                     "warnings": []}
        # A1.1 颜色冲突双口径统计的旁路数据 (只读汇总; UI/run_competition.py 零改动):
        #   口径① by_detection 按检测次数, 每次 scan 的每个目标都计入 (同一件被多次 scan 会重复计入);
        #   口径② by_piece 按成功入盒件数, 每件只取它"被抓走前最后一次 scan"那个目标的 diagnostics。
        self._scan_conflicts: Dict[str, int] = {"scans": 0, "targets": 0, "conflicts": 0}
        self._scan_by_class: Dict[str, Dict[str, int]] = {}
        self._last_scan_targets: List[dict] = []
        self._piece_conflicts: Dict[str, int] = {"pieces": 0, "conflicts": 0, "unmatched": 0}
        self._piece_by_class: Dict[str, Dict[str, int]] = {}
        self._lock = threading.Lock()
        try:
            os.makedirs(self.dir, exist_ok=True)
            self._prune(base)
        except OSError:
            self.dir = ""
        bus = _race_bus()
        if bus:
            from MessegeCenter.race import publish_state
            publish_state(bus, "running", run_id=os.path.basename(self.dir) if self.dir else "",
                          expected_total=int(os.environ.get("EXPECTED_TOTAL", 0) or 0))

    @property
    def items(self) -> List[dict]:
        return self.data["items"]

    def event(self, msg: str, level: str = "info") -> None:
        with self._lock:
            self.data["events"].append({"t": round(time.time() - self.t0, 2), "level": level, "msg": msg})
        self.save()
        bus = _race_bus()
        if bus:
            from MessegeCenter.race import publish_event
            publish_event(bus, msg, level)

    def warn_once(self, msg: str) -> None:
        if msg not in self.data["warnings"]:
            self.data["warnings"].append(msg)
            self.event(msg, "warn")

    def item(self, **kw) -> dict:
        kw.setdefault("t", round(time.time() - self.t0, 2))
        shot = kw.get("shot")
        if shot and self.dir and os.path.exists(shot):
            dst = os.path.join(self.dir, f"item_{len(self.items) + 1:03d}.jpg")
            try:
                shutil.copy(shot, dst)
                kw["shot"] = os.path.basename(dst)
            except OSError:
                pass
        with self._lock:
            self.items.append(kw)
        if kw.get("result") == "ok":
            # A1.1 口径②: 每件成功入盒时, 用「被抓走前最后一次 scan」的 diagnostics 记一次
            self._note_piece(kw)
        self.save()
        bus = _race_bus()
        if bus:
            from MessegeCenter.race import publish_item
            publish_item(bus, seq=kw.get("seq"), label=kw.get("label", ""), category=kw.get("category", ""),
                         slot=int(kw.get("slot") or 0), result=kw.get("result", ""),
                         total=int(kw.get("seq") or 0) if kw.get("result") == "ok" else 0,
                         image=shot or "", conf=float(kw.get("conf") or 0), x_mm=float(kw.get("x_mm") or 0),
                         y_mm=float(kw.get("y_mm") or 0), elapsed_s=float(kw.get("elapsed_s") or 0))
        return kw

    def save_scan_image(self, ann, idx: int, raw=None, targets=None) -> str:
        """保存本次识别的标注图 scan_NNN.jpg (+ 原图 scan_NNN_raw.jpg, 供归档到回归集)。"""
        if ann is None or not self.dir:
            return ""
        try:
            import cv2
            name = f"scan_{idx:03d}.jpg"
            cv2.imwrite(os.path.join(self.dir, name), ann, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if raw is not None:
                cv2.imwrite(os.path.join(self.dir, f"scan_{idx:03d}_raw.jpg"), raw, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if targets is not None:
                roster = []
                for t in targets:
                    diag = dict(getattr(t, "diagnostics", None) or {})
                    rec = {"u": float(getattr(t, "u_pixel", 0.0) or 0.0),
                           "v": float(getattr(t, "v_pixel", 0.0) or 0.0),
                           "class": str(getattr(t, "class_name", "") or ""),
                           "label": str(getattr(t, "label", "") or ""),
                           "category": str(getattr(t, "category", "") or ""),
                           "conflict": bool(diag.get("color_conflict")),
                           "model_color": diag.get("model_color"),
                           "hsv_color": diag.get("hsv_color")}
                    roster.append(rec)
                    # A1.1 口径①: 按检测次数累计 (每次 scan 的每个目标)
                    self._note_detection(rec["class"], rec["conflict"])
                with self._lock:
                    self._scan_conflicts["scans"] += 1
                    self._last_scan_targets = roster
                with open(os.path.join(self.dir, f"scan_{idx:03d}.json"), "w", encoding="utf-8") as f:
                    from UI.planner import display_name
                    json.dump([{"label": display_name(t), "category": t.category, "conf": t.confidence,
                                "u": t.u_pixel, "v": t.v_pixel, "bbox": t.bbox,
                                # A1: 旁路数据原样落盘 (颜色冲突/model_color/hsv_color...), 只加字段,
                                # display_name 与 finish() 汇总逻辑不变。
                                "diagnostics": dict(getattr(t, "diagnostics", None) or {}),
                                # A4: category 前两段 (形状|颜色|) 单独落盘, 复盘可按它聚合;
                                # 与 category 一起给, 便于对比"信息段"是否解出。
                                "category_core": (getattr(t, "diagnostics", None) or {}).get("category_core")
                                                 or RunReport.core_of(t.category)}
                               for t in targets],
                              f, ensure_ascii=False, default=str)
            return name
        except Exception:
            return ""

    # ------------------------------------------------------------ A1.1 按类颜色冲突率
    def _note_detection(self, cls_name: str, conflict: bool) -> None:
        """口径①: 按检测次数 (每次 scan 的每个目标) 累计, 按模型类名分开。"""
        key = str(cls_name or "unknown")
        with self._lock:
            self._scan_conflicts["targets"] += 1
            if conflict:
                self._scan_conflicts["conflicts"] += 1
            st = self._scan_by_class.setdefault(key, {"targets": 0, "conflicts": 0})
            st["targets"] += 1
            if conflict:
                st["conflicts"] += 1

    def _match_piece_target(self, kw: dict) -> Optional[dict]:
        """口径②: 把入盒件匹配回「被抓走前最后一次 scan」里的那个目标。

        `save_scan_image(targets=...)` 在 planner.pick_next 之前调用, 两者用的是同一批
        targets, 所以 (u, v) 一般逐个精确相同 (容差 2px); 退化时按 category / label 就近匹配。
        """
        roster = self._last_scan_targets
        if not roster:
            return None
        u, v = float(kw.get("u") or 0.0), float(kw.get("v") or 0.0)
        best, best_d = None, None
        for rec in roster:
            d = abs(rec["u"] - u) + abs(rec["v"] - v)
            if best_d is None or d < best_d:
                best, best_d = rec, d
        if best is not None and best_d is not None and best_d <= 2.0:
            return best
        for field in ("category", "label"):
            want = str(kw.get(field) or "")
            if not want:
                continue
            same = [r for r in roster if r[field] == want]
            if same:
                return min(same, key=lambda r: abs(r["u"] - u) + abs(r["v"] - v))
        return None

    def _note_piece(self, kw: dict) -> None:
        """口径②: 只统计成功入盒件 (result == ok), 每件一次。"""
        rec = self._match_piece_target(kw)
        with self._lock:
            if rec is None:
                self._piece_conflicts["unmatched"] += 1
                return
            key = rec["class"] or "unknown"
            self._piece_conflicts["pieces"] += 1
            st = self._piece_by_class.setdefault(key, {"pieces": 0, "conflicts": 0})
            st["pieces"] += 1
            if rec["conflict"]:
                self._piece_conflicts["conflicts"] += 1
                st["conflicts"] += 1

    def color_conflict_report(self) -> Dict[str, Any]:
        """按类颜色冲突率汇总, **双口径** (A1.1; 只读)。

        口径① `by_detection`: 按检测次数 (同一件被多次 scan 重复计入);
        口径② `by_piece`:     按成功入盒件数 (每件取被抓走前最后一次 scan 的 diagnostics)。
        两个口径都以口径②为准核对"分类判定是否受颜色冲突影响"。
        """
        def _rate(st: Dict[str, int], a: str, b: str) -> float:
            n = int(st.get(b, 0) or 0)
            return round(int(st.get(a, 0) or 0) / n, 4) if n else 0.0

        det = dict(self._scan_conflicts)
        det["rate"] = _rate(det, "conflicts", "targets")
        det["by_class"] = {c: dict(s, rate=_rate(s, "conflicts", "targets"))
                           for c, s in sorted(self._scan_by_class.items())}
        pie = dict(self._piece_conflicts)
        pie["rate"] = _rate(pie, "conflicts", "pieces")
        pie["by_class"] = {c: dict(s, rate=_rate(s, "conflicts", "pieces"))
                           for c, s in sorted(self._piece_by_class.items())}
        return {"by_detection": det, "by_piece": pie,
                "note": "口径① 按检测次数(同一件多次 scan 重复计入); "
                        "口径② 按成功入盒件数(每件取被抓走前最后一次 scan 的 diagnostics), 以口径②为准"}

    @staticmethod
    def core_of(category) -> str:
        """取 category 的「形状|颜色|」两段 (A4)。不解析第三段信息, 只读汇总用。"""
        parts = str(category or "").split("|")
        return "|".join(parts[:2]) + "|" if len(parts) >= 2 else ""

    def category_core_counts(self) -> Dict[str, int]:
        """按 category_core 聚合本轮的件数 (A4)。

        只读汇总, 不改比赛流程; item 里没有 category_core 时用 item["category"] 前两段推导
        (UI/run_competition.py 未改, 它只传 category)。
        """
        out: Dict[str, int] = {}
        for it in self.items:
            core = it.get("category_core") or self.core_of(it.get("category"))
            out[core] = out.get(core, 0) + 1
        return out

    def finish(self, result: str, total_done: int, slots: Optional[dict] = None) -> None:
        self.data.update(result=result, total_done=total_done, end=time.strftime("%Y-%m-%d %H:%M:%S"),
                         elapsed_s=round(time.time() - self.t0, 1), slots=slots or {})
        # A4: 按 category_core (形状|颜色|) 聚合的件数, 复盘核对"换颜色是否被当新类别"。
        self.data["category_core_counts"] = self.category_core_counts()
        # A1.1: 按类颜色冲突率, 双口径 (① 检测次数 / ② 成功入盒件数)
        self.data["color_conflict"] = self.color_conflict_report()
        bus = _race_bus()
        if bus:
            from MessegeCenter.race import publish_state
            publish_state(bus, "finished", result=result, total_done=total_done)
            try:
                bus.flush(1.0)
                snap = _race["ledger"].snapshot()
                self.data["ledger"] = {"total_done": snap.total_done, "attempts": snap.attempts,
                                       "failures": snap.failures,
                                       "rules": [{"code": r.code, "level": r.level, "msg": r.message}
                                                 for r in snap.rules]}
                for r in snap.rules:
                    if r.level == "error" and r.message not in self.data["warnings"]:
                        self.data["warnings"].append(r.message)
            except Exception:
                pass
        self.save()

    def save(self) -> None:
        if not self.dir:
            return
        with self._lock:
            try:
                tmp = os.path.join(self.dir, "report.json.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=1, default=str)
                os.replace(tmp, os.path.join(self.dir, "report.json"))
            except OSError:
                pass

    @staticmethod
    def _prune(base: str) -> None:
        runs = sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))
        for d in runs[:-KEEP_RUNS] if len(runs) > KEEP_RUNS else []:
            shutil.rmtree(os.path.join(base, d), ignore_errors=True)


def list_runs(base: str = RUNS_DIR) -> List[dict]:
    out = []
    if not os.path.isdir(base):
        return out
    for d in sorted(os.listdir(base), reverse=True):
        p = os.path.join(base, d, "report.json")
        try:
            with open(p, encoding="utf-8") as f:
                r = json.load(f)
            out.append({"id": d, "start": r.get("start"), "result": r.get("result"),
                        "total_done": r.get("total_done"), "elapsed_s": r.get("elapsed_s"),
                        "warnings": len(r.get("warnings", []))})
        except Exception:
            continue
    return out
