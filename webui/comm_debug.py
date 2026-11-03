# -*- coding: utf-8 -*-
"""上下位机通讯调试后端 —— 替代 debug/communication_debug/app.py 的 Tk 界面。

只做"接线 + 收发 + 展示"，不改 communication/ 的帧格式、SEQ/ACK、is_pick_ready 判定。
串口自动发现、连接、MockSTM32 模拟、实时状态帧、发送各条命令、收发日志 —— 全部复用
communication 模块现成接口。
"""
from __future__ import annotations

import math
import threading
import time


def _finite(v):
    """把 link_age_s 之类的值收窄成有限 float; inf/nan -> None (浏览器 JSON.parse 不认 Infinity)。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 1) if math.isfinite(f) else None


class CommDebug:
    def __init__(self, client=None, calibrator=None):
        self._lock = threading.Lock()
        self.logs = []
        self.client = client
        # A2: 有了标定器就能在发 MOVE 之前用 vision.limits 的同一个检查函数判越界
        self.calibrator = calibrator
        if self.client is None:
            try:
                from communication import create_comm_client
                self.client = create_comm_client()
            except Exception:
                self.client = None
        self._hook()

    # ---------------------------------------------------------------- 日志
    def _log(self, kind, text):
        with self._lock:
            self.logs.append({"t": time.time(), "kind": kind, "text": str(text)})
            if len(self.logs) > 400:
                self.logs = self.logs[-400:]

    def _hook(self):
        c = self.client
        if not c:
            return
        def _hex(d):
            try:
                return d.hex(" ")
            except Exception:
                return str(d)
        try:
            c.on_raw_tx_callbacks.append(lambda d: self._log("tx", _hex(d)))
            c.on_raw_rx_callbacks.append(lambda d: self._log("rx", _hex(d)))
        except Exception:
            pass

    # ---------------------------------------------------------------- 设备
    def list_ports(self):
        try:
            from communication import IntelligentSortingClient
            return [{"port": p, "desc": d} for p, d in IntelligentSortingClient.list_serial_ports()]
        except Exception:
            return []

    def connect(self, body):
        c = self.client
        if not c:
            return {"ok": False, "error": "通讯客户端不可用"}
        body = body or {}
        port = (body.get("port") or "").strip() or None
        try:
            baud = int(body.get("baudrate") or 115200)
        except (TypeError, ValueError):
            baud = 115200
        try:
            ok = c.connect(port, baudrate=baud) if port else c.connect_auto(baudrate=baud)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        self._log("ev", "连接 %s: %s" % (port or "自动发现", "成功" if ok else "失败"))
        return {"ok": bool(ok), "state": self.snapshot()}

    def connect_mock(self):
        c = self.client
        if not c:
            return {"ok": False, "error": "通讯客户端不可用"}
        try:
            ok = c.connect_mock()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        self._log("ev", "MockSTM32 模拟: %s" % ("已连接" if ok else "失败"))
        return {"ok": bool(ok), "state": self.snapshot()}

    def disconnect(self):
        c = self.client
        if not c:
            return {"ok": False, "error": "通讯客户端不可用"}
        try:
            c.disconnect()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        self._log("ev", "已断开")
        return {"ok": True, "state": self.snapshot()}

    # ---------------------------------------------------------------- 状态
    def snapshot(self):
        c = self.client
        if not c:
            return {"available": False, "logs": self._tail()}
        st = None
        for attr in ("latest_status",):
            try:
                st = getattr(c, attr)
            except Exception:
                st = None
        d = {
            "available": True,
            "connected": bool(getattr(c, "is_connected", False)),
            "mock": bool(getattr(c, "is_mock", False)),
            "link": getattr(c, "link_description", "") if callable(getattr(c, "link_description", None))
                    else getattr(c, "link_description", ""),
            "alive": bool(getattr(c, "is_link_alive", False)),
            "age_s": _finite(getattr(c, "link_age_s", None)),
        }
        if st is not None:
            d["status"] = {
                "state": getattr(st, "state", None),
                "state_desc": getattr(st, "state_desc", ""),
                "gripper": getattr(st, "gripper", None),
                "error": getattr(st, "error", 0),
                "res_flags": getattr(st, "res_flags", 0),
                "res_stage": getattr(st, "res_stage", 0),
                "progress": getattr(st, "progress", 0),
                "storage_slot": getattr(st, "storage_slot", 0),
                "x_mm": round(float(getattr(st, "cur_x_mm", 0) or 0), 1),
                "y_mm": round(float(getattr(st, "cur_y_mm", 0) or 0), 1),
                "busy": bool(getattr(st, "is_busy", False)),
                "estop": bool(getattr(st, "is_estop", False)),
                "alarm": bool(getattr(st, "is_alarm", False)),
                "pick_ready": bool(getattr(st, "is_pick_ready", False)),
            }
        d["logs"] = self._tail()
        return d

    def _tail(self, n=80):
        with self._lock:
            return list(self.logs[-n:])

    # ---------------------------------------------------------------- 限位 (A2)
    def _limit_verdict(self, x, y):
        """
        发送 MOVE 之前的越界判定 —— 与 scan 过滤 / 网页「点哪去哪」用的是
        vision.limits 里同一个检查函数 (check_point / check_robot_xy)。
        没有 vision 包或读不到配置时返回 None (不做拦截, 与基线行为一致)。
        """
        try:
            from vision import limits as limitsmod
        except Exception:
            return None
        if self.calibrator is not None:
            try:
                return self.calibrator.check_robot_xy(x, y)
            except Exception as e:
                self._log("ev", "限位检查异常 (不拦截): %s" % e)
                return None
        try:
            import json
            import os
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.environ.get("IS_CALIB_CONFIG") or os.path.join(root, "vision", "calib_config.json")
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            lim, margin, action = limitsmod.limits_from_config(data)
            return limitsmod.check_point(x, y, limits=lim, tray_margin_mm=margin,
                                         tray_margin_action=action, source="comm")
        except Exception as e:
            self._log("ev", "限位检查异常 (不拦截): %s" % e)
            return None

    # ---------------------------------------------------------------- 命令
    def send(self, body):
        c = self.client
        if not c or not getattr(c, "is_connected", False):
            return {"ok": False, "error": "未连接 (先在左侧连接串口或点 Mock)"}
        body = body or {}
        cmd = (body.get("cmd") or "").strip()
        try:
            tid = int(body.get("tid") or 0)
        except (TypeError, ValueError):
            tid = 0
        try:
            x = float(body.get("x") or 0)
            y = float(body.get("y") or 0)
        except (TypeError, ValueError):
            x = y = 0.0
        try:
            slot = int(body.get("slot") or 0)
        except (TypeError, ValueError):
            slot = 0
        if cmd == "move":
            # A2: 越界目标不许进发送序列 —— 拦在 send_move 之前, 帧格式/协议不动
            v = self._limit_verdict(x, y)
            if v is not None:
                for w in (v.warnings or []):
                    self._log("ev", "限位提示: %s" % w)
                if not v.ok:
                    self._log("ev", "限位拦截 /comm MOVE x=%.1f y=%.1f: %s" % (x, y, v.reason))
                    return {"ok": False, "cmd": cmd, "limit": v.as_dict(),
                            "error": "坐标越界, 已拒绝发送 (x=%.1f, y=%.1f): %s" % (x, y, v.reason)}
        table = {
            "clear": lambda: c.send_clear(),
            "estop": lambda: c.send_estop(),
            "cancel": lambda: c.send_cancel(tid),
            "permit": lambda: c.send_permit(tid),
            "grip_open": lambda: c.send_grip_open(),
            "grip_close": lambda: c.send_grip_close(),
            "z_up": lambda: c.send_z_up(),
            "z_down": lambda: c.send_z_down(),
            "tray_shake": lambda: c.send_tray_shake(),
            "tray_home": lambda: c.send_tray_home(),
            "storage_step": lambda: c.send_storage_step(bool(body.get("ccw", True))),
            "storage_slot": lambda: c.send_storage_slot(slot),
            "poll": lambda: c.send_poll(),
            "move": lambda: c.send_move(int(body.get("tid") or 0), x, y,
                                        timeout_ms=int(body.get("timeout_ms") or 10000),
                                        wait_ack=False),
        }
        fn = table.get(cmd)
        if not fn:
            return {"ok": False, "error": "未知命令: %s" % cmd}
        try:
            ok = fn()
        except Exception as e:
            self._log("ev", "命令 %s 异常: %s" % (cmd, e))
            return {"ok": False, "error": str(e)}
        self._log("ev", "发送 %s -> %s" % (cmd, "ACK" if ok else "无响应"))
        return {"ok": bool(ok), "cmd": cmd}
