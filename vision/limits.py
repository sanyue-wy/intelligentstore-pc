# -*- coding: utf-8 -*-
"""
发送前越界拦截 (计划 A2) —— 像素 / 托盘 / 机械臂三套坐标的**单一检查函数**。

被三处共用, 保证"发出去之前"用的是同一套判据:
    1. vision/service.py::scan()      识别结果过滤 (越界目标不进返回列表/不进发送序列)
    2. tools/vision_tool.py::goto()   网页「点哪去哪」发 MOVE 之前
    3. webui/comm_debug.py::send()    /comm 手动 MOVE 之前

判据 (全部来自配置, 不写死; 见 vision/calib_config.json + IS_* 环境变量):
    robot_limits_mm    {"x_min":..,"x_max":..,"y_min":..,"y_max":..}  默认 null = 不检查
                       —— **唯一的硬拦截项**。闭区间: x_min <= x <= x_max 放行。
    tray_margin_mm     托盘内框外扩多少 mm 算"出托"                  默认 null = 不检查
    tray_margin_action warn  (默认) 出托只提示, 不丢件
                       drop         出托也拦截 (要求 margin >= 最小件半宽)

环境变量覆盖:
    IS_ROBOT_LIMITS="x_min,x_max,y_min,y_max"  例 IS_ROBOT_LIMITS="-20,200,0,190"
    IS_TRAY_MARGIN_MM=10
    IS_TRAY_MARGIN_ACTION=drop

边界声明 (C5): 本模块只保证**上位机不把越界目标发出去**。
不声称、也不试图修复下位机的驱动器故障、参数超限实现、ACM0→ACM1 重枚举;
下潜 / 退避 / 归位等中间动作的坐标由固件与 UI 决定, 不在本模块覆盖范围内。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

TRIGGER_ROBOT = "robot_limits"
TRIGGER_TRAY = "tray_margin"
TRIGGER_INVALID = "invalid_coord"

ACTIONS = ("warn", "drop")


# ---------------------------------------------------------------------------- 配置解析
def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None   # nan/inf 不收


def parse_limits(value: Any) -> Optional[Dict[str, float]]:
    """
    解析 robot_limits_mm。返回 {"x_min","x_max","y_min","y_max"} 或 None (= 不检查)。

    格式不完整 / 非数值 -> 返回 None 并记 warning: 视为"未配置", 不静默拦截全部目标
    (配置打错一个字就让整轮抓不到货, 比漏一次拦截更糟)。缺配置由 A3 门禁 `robot_limits` 提示。
    """
    if value is None or value == "" or value == {}:
        return None
    if not isinstance(value, dict):
        logger.warning("robot_limits_mm 应为对象, 收到 %r; 视为未配置", value)
        return None
    out: Dict[str, float] = {}
    for k in ("x_min", "x_max", "y_min", "y_max"):
        f = _num(value.get(k))
        if f is None:
            logger.warning("robot_limits_mm 缺 %s 或不是数值 (%r); 视为未配置", k, value.get(k))
            return None
        out[k] = f
    if out["x_min"] > out["x_max"] or out["y_min"] > out["y_max"]:
        logger.warning("robot_limits_mm 上下限颠倒 (min>max): %r; 视为未配置", out)
        return None
    return out


def limits_from_env(env: Optional[Dict[str, str]] = None) -> Optional[Dict[str, float]]:
    """IS_ROBOT_LIMITS="x_min,x_max,y_min,y_max"。"""
    env = os.environ if env is None else env
    raw = (env.get("IS_ROBOT_LIMITS") or "").strip()
    if not raw:
        return None
    parts = [p.strip() for p in raw.replace(";", ",").split(",")]
    if len(parts) != 4:
        logger.warning("IS_ROBOT_LIMITS 需要 4 个数 (x_min,x_max,y_min,y_max), 收到 %r; 忽略", raw)
        return None
    try:
        x_min, x_max, y_min, y_max = (float(p) for p in parts)
    except ValueError:
        logger.warning("IS_ROBOT_LIMITS 含非数值: %r; 忽略", raw)
        return None
    return parse_limits({"x_min": x_min, "x_max": x_max, "y_min": y_min, "y_max": y_max})


def limits_from_config(cfg: Optional[Dict[str, Any]], env: Optional[Dict[str, str]] = None
                       ) -> Tuple[Optional[Dict[str, float]], Optional[float], str]:
    """
    从 calib_config.json 的顶层 (env 优先) 取三项限位配置。
    返回 (robot_limits_mm, tray_margin_mm, tray_margin_action)。
    """
    env = os.environ if env is None else env
    cfg = cfg or {}
    lim = limits_from_env(env) or parse_limits(cfg.get("robot_limits_mm"))
    if parse_limits(cfg.get("robot_limits_mm")) is not None and limits_from_env(env) is not None:
        logger.info("限位: 环境变量 IS_ROBOT_LIMITS 覆盖 calib_config.json 的 robot_limits_mm")
    if env.get("IS_TRAY_MARGIN_MM"):
        margin = _num(env["IS_TRAY_MARGIN_MM"])
    else:
        margin = _num(cfg.get("tray_margin_mm"))
    action = (env.get("IS_TRAY_MARGIN_ACTION") or cfg.get("tray_margin_action") or "warn")
    action = str(action).strip().lower()
    if action not in ACTIONS:
        logger.warning("tray_margin_action=%r 不认识 (只支持 warn|drop), 按 warn", action)
        action = "warn"
    return lim, margin, action


# ---------------------------------------------------------------------------- 判定
@dataclass
class LimitVerdict:
    """一次检查的结论。ok=True 才允许发送。"""
    ok: bool = True
    trigger: str = ""                      # "" | robot_limits | tray_margin | invalid_coord
    axes: List[str] = field(default_factory=list)      # 越界的轴: x / y / t_x / t_y
    reason: str = ""                       # 拦截原因 (ok=False 时非空)
    warnings: List[str] = field(default_factory=list)   # 放行但需提示 (tray_margin_action=warn)
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return not self.ok

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "blocked": self.blocked, "trigger": self.trigger,
                "axes": list(self.axes), "reason": self.reason,
                "warnings": list(self.warnings), "detail": dict(self.detail)}

    def text(self) -> str:
        """一行可读结论; 事件日志与网页回显共用, 保证两处文字一致。"""
        d = self.detail or {}
        px, tr, rb = d.get("pixel_uv"), d.get("tray_xy"), d.get("robot_xy")
        return ("像素(%s) 托盘(%s) 机械臂(%s)" % (
            _fmt(px), _fmt(tr), _fmt(rb)))


def _fmt(v) -> str:
    if not v:
        return "?"
    try:
        return ",".join("%.1f" % float(x) for x in v)
    except (TypeError, ValueError):
        return str(v)


def check_point(x_mm: Any, y_mm: Any,
                limits: Optional[Dict[str, float]] = None,
                tray_margin_mm: Optional[float] = None,
                tray_margin_action: str = "warn",
                tray_xy: Optional[Sequence[float]] = None,
                pixel_uv: Optional[Sequence[float]] = None,
                tray_size: Optional[Sequence[float]] = None,
                source: str = "") -> LimitVerdict:
    """
    唯一检查函数: 判断机械臂坐标 (x_mm, y_mm) 能不能发出去。

    参数:
        x_mm, y_mm          要发送的机械臂坐标 (mm) —— 判据的核心
        limits              robot_limits_mm; None = 不检查 (但 detail 里标明"未配置")
        tray_margin_mm      None = 不检查托盘边距
        tray_margin_action  "warn" 只提示 / "drop" 也拦截
        tray_xy             托盘坐标 (mm), 只用于托盘边距判定与详情
        pixel_uv            像素坐标, 只用于详情
        tray_size           托盘内框 [w, h] mm, 托盘边距判定用
        source              调用位置 (scan / goto / comm_move), 写进 detail 便于复盘

    返回 LimitVerdict: ok=False 就一定不要发送。
    边界是**闭区间** (x_min <= x <= x_max 放行)。
    """
    x, y = _num(x_mm), _num(y_mm)
    action = str(tray_margin_action or "warn").strip().lower()
    if action not in ACTIONS:
        action = "warn"
    det: Dict[str, Any] = {
        "pixel_uv": list(pixel_uv) if pixel_uv is not None else None,
        "tray_xy": list(tray_xy) if tray_xy is not None else None,
        "robot_xy": [x, y],
        "robot_limits_mm": dict(limits) if limits else None,
        "robot_limits_configured": bool(limits),
        "tray_margin_mm": tray_margin_mm,
        "tray_margin_action": action,
        "tray_margin_configured": tray_margin_mm is not None,
        "tray_size_mm": list(tray_size) if tray_size is not None else None,
        "source": source,
    }
    if x is None or y is None:
        det["tray_margin_skipped"] = None
        return LimitVerdict(ok=False, trigger=TRIGGER_INVALID, axes=["x", "y"],
                            reason="坐标不是有限数值, 拒绝发送", detail=det)

    axes: List[str] = []
    reasons: List[str] = []
    warn: List[str] = []

    if limits:
        if x < limits["x_min"] or x > limits["x_max"]:
            axes.append("x")
            reasons.append("x=%.1f 超出 [%.1f, %.1f]" % (x, limits["x_min"], limits["x_max"]))
        if y < limits["y_min"] or y > limits["y_max"]:
            axes.append("y")
            reasons.append("y=%.1f 超出 [%.1f, %.1f]" % (y, limits["y_min"], limits["y_max"]))
    else:
        det["robot_limits_note"] = "未配置 robot_limits_mm, 未做机械臂限位检查"

    if axes:
        det["robot_out_of_range"] = True
        return LimitVerdict(ok=False, trigger=TRIGGER_ROBOT, axes=axes,
                            reason="; ".join(reasons), detail=det)

    # ---- 托盘边距 (非硬拦截: 默认 warn 只提示)
    if tray_margin_mm is None:
        det["tray_margin_note"] = "未配置 tray_margin_mm, 未做托盘边距检查"
    elif tray_xy is None or tray_size is None:
        det["tray_margin_skipped"] = "托盘坐标/尺寸未知, 本次跳过托盘边距检查"
    else:
        tx, ty = _num(tray_xy[0]), _num(tray_xy[1])
        w, h = _num(tray_size[0]), _num(tray_size[1])
        if tx is None or ty is None or w is None or h is None:
            det["tray_margin_skipped"] = "托盘坐标/尺寸不是数值, 本次跳过"
        else:
            m = float(tray_margin_mm)
            tax: List[str] = []
            if tx < -m or tx > w + m:
                tax.append("t_x")
            if ty < -m or ty > h + m:
                tax.append("t_y")
            if tax:
                det["tray_out_of_range"] = True
                msg = ("tray=%.1f,%.1f 出托盘内框 [0,%.1f]x[0,%.1f] 外扩 %.1f mm"
                       % (tx, ty, w, h, m))
                if action == "drop":
                    return LimitVerdict(ok=False, trigger=TRIGGER_TRAY, axes=tax,
                                        reason=msg, detail=det)
                warn.append("托盘边距(warn, 不丢件): " + msg)
    return LimitVerdict(ok=True, trigger="", axes=[], reason="", warnings=warn, detail=det)


# ---------------------------------------------------------------------------- 适配器
def _tray_size_of(calibrator) -> Optional[List[float]]:
    try:
        cfg = getattr(calibrator, "tray_cfg", None) or {}
        w, h = _num(cfg.get("inner_w_mm")), _num(cfg.get("inner_h_mm"))
        if w and h:
            return [w, h]
    except Exception:
        pass
    return None


def robot_to_tray(calibrator, x_mm: float, y_mm: float) -> Optional[Tuple[float, float]]:
    """机械臂 mm -> 托盘 mm (用标定的刚体反解; 不反解残差多项式, 只用于边距提示)。"""
    try:
        rig = getattr(calibrator, "tray_rigid", None)
        if not rig:
            return None
        R = rig["R"]
        t = rig["t"]
        p = R.T @ (__import__("numpy").float64([x_mm, y_mm]) - t)
        return float(p[0]), float(p[1])
    except Exception:
        return None


def check_robot_xy(x_mm, y_mm, limits=None, tray_margin_mm=None, tray_margin_action="warn",
                   calibrator=None, source="") -> LimitVerdict:
    """
    MOVE 类入口 (网页「点哪去哪」/ `/comm` 手动 MOVE) 用: 已知机械臂 mm, 反查像素/托盘坐标做详情。
    """
    pixel_uv = tray_xy = None
    if calibrator is not None:
        try:
            pixel_uv = calibrator.robot_to_pixel(float(x_mm), float(y_mm))
        except Exception:
            pixel_uv = None
        if tray_margin_mm is not None:
            tr = robot_to_tray(calibrator, float(x_mm), float(y_mm))
            if tr is not None:
                tray_xy = [tr[0], tr[1]]
    return check_point(x_mm, y_mm, limits=limits, tray_margin_mm=tray_margin_mm,
                       tray_margin_action=tray_margin_action, tray_xy=tray_xy,
                       pixel_uv=pixel_uv, tray_size=_tray_size_of(calibrator) if calibrator else None,
                       source=source)


def check_target(target, calibrator=None, limits=None, tray_margin_mm=None,
                 tray_margin_action="warn", height_mm: float = 0.0, source="scan") -> LimitVerdict:
    """scan 的识别目标 -> 判定 (像素/托盘坐标从标定器换算, 机械臂坐标直接用目标自带值)。"""
    u = _num(getattr(target, "u_pixel", None))
    v = _num(getattr(target, "v_pixel", None))
    x = _num(getattr(target, "x_mm", None))
    y = _num(getattr(target, "y_mm", None))
    tray_xy = None
    if calibrator is not None and u is not None and v is not None:
        try:
            if getattr(calibrator, "tray_mode", False):
                tx, ty = calibrator.pixel_to_tray(u, v, float(height_mm or 0.0))
                tray_xy = [float(tx), float(ty)]
        except Exception:
            tray_xy = None
    return check_point(x, y, limits=limits, tray_margin_mm=tray_margin_mm,
                       tray_margin_action=tray_margin_action, tray_xy=tray_xy,
                       pixel_uv=[u, v] if (u is not None and v is not None) else None,
                       tray_size=_tray_size_of(calibrator) if calibrator else None,
                       source=source)


def format_reject(target, verdict: LimitVerdict, target_id: Optional[int] = None) -> str:
    """
    被拦目标的一行事件文字。**必须与 UI 的"空盘"事件 (`托盘无可识别货物, 抖料后重扫`) 可区分**,
    所以统一以 "视觉限位拦截:" 开头。D2 要求三套坐标齐全。
    """
    d = verdict.detail or {}
    px, tr, rb = d.get("pixel_uv"), d.get("tray_xy"), d.get("robot_xy")
    tid = target_id if target_id is not None else getattr(target, "target_id", None)
    name = (getattr(target, "label", "") or getattr(target, "class_name", "")
            or getattr(target, "category", "") or "?")
    return ("视觉限位拦截: 目标#%s %s 像素(%s) 托盘(%s) 机械臂(%s) 触发=%s 轴=%s 原因=%s"
            % (tid, name, _fmt(px), _fmt(tr), _fmt(rb),
               verdict.trigger or "-", ",".join(verdict.axes) or "-", verdict.reason or "-"))


def format_rejected_all(n: int, rejects: List[Dict[str, Any]]) -> str:
    """整次 scan 全部被拦时的汇总事件 (D2: 写成"N 件超出机械臂限位", 不要像空盘)。"""
    xs = [r.get("robot_xy", [None, None]) for r in rejects]
    trig = sorted({r.get("trigger") or "-" for r in rejects})
    span = ""
    try:
        xv = [float(p[0]) for p in xs if p and p[0] is not None]
        yv = [float(p[1]) for p in xs if p and p[1] is not None]
        if xv and yv:
            span = " (x=%.1f~%.1f, y=%.1f~%.1f)" % (min(xv), max(xv), min(yv), max(yv))
    except (TypeError, ValueError, IndexError):
        span = ""
    return ("视觉限位拦截: 本次 %d 件目标全部越界, 未返回任何目标 (触发=%s)%s"
            " —— 不是空盘, 是限位/标定问题" % (n, "/".join(trig), span))