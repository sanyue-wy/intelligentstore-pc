# -*- coding: utf-8 -*-
"""
每轮比赛的复盘报告: records/runs/<时间>/report.json + 每件货物截图 + 每次识别的标注图

report.json:
    {"start": ..., "end": ..., "elapsed_s": ..., "total_done": 12, "result": "完成|急停|停止|异常",
     "items":  [{"seq", "t", "label", "category", "conf", "slot", "u", "v", "x_mm", "y_mm",
                 "result": "ok|grip_fail|fail|estop", "elapsed_s", "shot", "scan"}],
     "events": [{"t", "level", "msg"}],
     "slots":  {"1": {"label": "红色正方体", "count": 2}, ...},
     "model":  "...", "warnings": [...]}
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
                with open(os.path.join(self.dir, f"scan_{idx:03d}.json"), "w", encoding="utf-8") as f:
                    from UI.planner import display_name
                    json.dump([{"label": display_name(t), "category": t.category, "conf": t.confidence,
                                "u": t.u_pixel, "v": t.v_pixel, "bbox": t.bbox,
                                # A1: 旁路数据原样落盘 (颜色冲突/model_color/hsv_color...), 只加字段,
                                # display_name 与 finish() 汇总逻辑不变。
                                "diagnostics": dict(getattr(t, "diagnostics", None) or {})}
                               for t in targets],
                              f, ensure_ascii=False, default=str)
            return name
        except Exception:
            return ""

    def finish(self, result: str, total_done: int, slots: Optional[dict] = None) -> None:
        self.data.update(result=result, total_done=total_done, end=time.strftime("%Y-%m-%d %H:%M:%S"),
                         elapsed_s=round(time.time() - self.t0, 1), slots=slots or {})
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
