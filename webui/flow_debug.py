# -*- coding: utf-8 -*-
"""比赛流程调试 / 完整过程调试后端。

单步驱动: 扫描一次 / 规划一次 / 执行一件 / 抖料 —— 直接调用 SorterPlanner 与
CompetitionController 的现成方法, 不重写比赛状态机 (UI/run_competition.py 的 run_round
逻辑保持不动)。完整过程调试只是把同样的单步动作在一个线程里循环, 用 MockSTM32 + 合成
相机即可离线联调。
"""
from __future__ import annotations

import threading
import time


class FlowDebug:
    def __init__(self, svc, client=None, target_to_dict=None, controller_timeout=8.0):
        self.svc = svc
        self.client = client
        self.t2d = target_to_dict or (lambda t: {"label": getattr(t, "label", "")})
        self.controller_timeout = float(controller_timeout)
        from UI.planner import SorterPlanner
        self.planner = SorterPlanner()
        self.targets = []
        self.scan_stat = None
        self.last_result = None
        self.events = []
        self._controller = None
        self._stop = False
        self._run_thread = None

    # ---------------------------------------------------------------- 内部
    def _ev(self, msg):
        self.events.append({"t": time.time(), "msg": str(msg)})
        if len(self.events) > 240:
            self.events = self.events[-240:]

    def _ctrl(self):
        if self._controller is None and self.client is not None:
            from UI.controller import CompetitionController
            self._controller = CompetitionController(self.client, timeout_s=self.controller_timeout)
        return self._controller

    def _ensure_client(self):
        c = self.client
        if c is None:
            return False
        if getattr(c, "is_connected", False):
            return True
        try:
            if c.connect_mock():
                self._ev("下位机未连接, 已自动用 MockSTM32 模拟 (真机请在通讯调试页连接)")
                self._controller = None
                return True
        except Exception as e:
            self._ev("Mock 连接失败: %s" % e)
        return False

    # ---------------------------------------------------------------- 单步
    def scan(self):
        ts, stat = self.svc.scan()
        self.targets = list(ts or [])
        self.scan_stat = stat
        self._ev("扫描: %d 件" % len(self.targets))
        return {"ok": True, "targets": [self.t2d(t) for t in self.targets], "stat": stat}

    def plan(self):
        chosen = None
        try:
            chosen = self.planner.pick_next(self.targets)
        except Exception as e:
            self._ev("规划异常: %s" % e)
        reason = self.planner.assess(chosen, self.targets) if chosen is not None else "无目标"
        out = {
            "ok": chosen is not None,
            "chosen": self.t2d(chosen) if chosen is not None else None,
            "reason": reason,
            "slots": self.planner.slots_summary(),
            "warnings": list(getattr(self.planner, "warnings", []) or []),
        }
        label = (out["chosen"] or {}).get("label", "无")
        self._ev("规划: 选中 %s (原因: %s)" % (label, reason))
        return out

    def exec_one(self, body=None):
        body = body or {}
        if not self._ensure_client():
            return {"ok": False, "error": "未连接下位机 (先在通讯调试页连接, 或直接点执行自动用 Mock)"}
        if not self.targets:
            return {"ok": False, "error": "没有目标, 先扫描一次"}
        try:
            chosen = self.planner.pick_next(self.targets)
        except Exception as e:
            return {"ok": False, "error": "选件异常: %s" % e}
        if chosen is None:
            return {"ok": False, "error": "无可抓目标 (可能都不可达/重叠)"}
        x = getattr(chosen, "x_mm", None)
        y = getattr(chosen, "y_mm", None)
        if x is None or y is None:
            try:
                x, y = self.svc.calibrator.pixel_to_robot(
                    float(chosen.u_pixel), float(chosen.v_pixel), float(body.get("h") or 0))
            except Exception:
                x, y = 0.0, 0.0
        try:
            slot = int(self.planner.slot_for(chosen))
        except Exception:
            slot = 0
        self._ev("执行: %s -> %d 号盒 (%.1f, %.1f)" % (getattr(chosen, "label", ""), slot, x, y))
        try:
            res = self._ctrl().sort_one(float(x), float(y), slot)
        except Exception as e:
            return {"ok": False, "error": "动作异常: %s" % e}
        d = {
            "ok": bool(getattr(res, "success", False)),
            "success": bool(getattr(res, "success", False)),
            "fatal": bool(getattr(res, "fatal", False)),
            "grip_fail": bool(getattr(res, "grip_fail", False)),
            "slot_fail": bool(getattr(res, "slot_fail", False)),
            "elapsed_s": round(float(getattr(res, "elapsed_s", 0) or 0), 2),
            "message": getattr(res, "message", ""),
        }
        if d["success"]:
            try:
                self.planner.mark_done(chosen)
            except Exception:
                pass
            self.targets = [t for t in self.targets if t is not chosen]
        self.last_result = d
        self._ev("结果: %s %s" % ("成功" if d["success"] else "失败", d["message"]))
        return d

    def shake(self):
        if not self._ensure_client():
            return {"ok": False, "error": "未连接下位机"}
        try:
            self._ctrl().shake_tray()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        self._ev("抖料")
        return {"ok": True}

    def reset(self):
        from UI.planner import SorterPlanner
        self.planner = SorterPlanner()
        self.targets = []
        self.last_result = None
        self.events = []
        self._stop = True
        self._ev("已重置")
        return {"ok": True}

    # ---------------------------------------------------------------- 完整流程
    def start_full(self, body=None):
        body = body or {}
        if self._run_thread is not None and self._run_thread.is_alive():
            return {"ok": False, "error": "完整流程已在运行"}
        if not self._ensure_client():
            return {"ok": False, "error": "未连接下位机"}
        self._stop = False
        max_items = int(body.get("max_items") or 20)
        self._ev("===== 开始完整流程 (最多 %d 件) =====" % max_items)
        self._run_thread = threading.Thread(target=self._full_loop, args=(max_items,),
                                             name="FlowFull", daemon=True)
        self._run_thread.start()
        return {"ok": True}

    def _full_loop(self, max_items):
        for _ in range(max_items):
            if self._stop:
                break
            self.scan()
            if not self.targets:
                self._ev("无目标, 结束")
                break
            r = self.exec_one({})
            if not r.get("ok"):
                if r.get("fatal"):
                    self._ev("急停/致命错误, 结束")
                    break
                if r.get("error"):
                    self._ev("本件失败: %s" % r["error"])
        self._ev("===== 完整流程结束 =====")

    def stop_full(self):
        self._stop = True
        return {"ok": True}

    def running(self):
        return bool(self._run_thread is not None and self._run_thread.is_alive())

    # ---------------------------------------------------------------- 快照
    def snapshot(self):
        return {
            "targets": [self.t2d(t) for t in self.targets],
            "scan_stat": self.scan_stat,
            "slots": self.planner.slots_summary(),
            "warnings": list(getattr(self.planner, "warnings", []) or []),
            "last_result": self.last_result,
            "events": self.events[-80:],
            "connected": bool(self.client and getattr(self.client, "is_connected", False)),
            "running": self.running(),
        }
