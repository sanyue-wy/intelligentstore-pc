# -*- coding: utf-8 -*-
"""
视觉自检门禁 (计划 A3) —— 把"能不能开始比赛"从"运行中途才发现"提前到"发送/启动之前"。

11 条检查项 (每条有稳定 id + 级别 + 详情), 级别三级:
    off   不检查
    warn  只提示 (调试模式默认; 不影响退出码)
    fail  拦截 (比赛模式默认; start() 抛异常 / start_competition.sh 非零退出)

级别来源 (后者优先):
    gates.<id> in vision/calib_config.json  ->  IS_GATE_<ID> 环境变量
    没写 = 按模式取默认: 调试模式普遍 warn, 比赛模式 (vision.config.race_mode()) 关键项 fail

"比赛模式"唯一判据 = 锁文件存在 (vision.config.race_mode() / competition_lock_path()),
单测用 IS_COMPETITION_LOCK 指向临时路径, 不碰 /tmp。

参考点指纹 ref_points_fp (计划 D1):
    由「四角 + 内参(camera_matrix/dist_coeffs) + 分辨率 image_size
        + tray.fixed_z_mm + tray.axes + tray.origin_mm」派生 (sha1 前 12 位)。
    是否过期**只比指纹, 不比时间** —— 板子可能没有 RTC, 时间戳不可信;
    ref_points_verified_at 只作展示。
    改了上面任一项 -> 指纹对不上 -> `ref_points` 门禁判"过期";
    重新点过参考点 (CoordinateCalibrator.set_tray_ref_points) 或显式 `python3 -m vision.gates --seal`
    才会重新盖章。
    注意: 内框尺寸 inner_w/h_mm 不参与指纹 (计划列的六项就是这六项); 改尺寸会被 fit_quality 的
    比例检查先抓到, 这里不重复。

用法:
    python3 -m vision.gates --check          # 只读检查, 退出码 0=放行 / 1=有 fail / 2=用法错误
    python3 -m vision.gates --json           # 机器可读 (调试台/脚本)
    python3 -m vision.gates --seal           # 现场确认参考点: 写 ref_points_fp (会改 calib_config.json)
    python3 -m vision.gates --calib /tmp/x.json --vision-config /tmp/v.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

GATE_IDS: Tuple[str, ...] = (
    "ref_points", "ref_collinear", "fit_quality", "residual_poly", "class_height",
    "imaging_locked", "intrinsics", "color_imaging", "robot_limits", "pose", "cam_self",
)

GATE_LABEL: Dict[str, str] = {
    "ref_points": "参考点",
    "ref_collinear": "参考点共线",
    "fit_quality": "拟合质量",
    "residual_poly": "残差多项式",
    "class_height": "类别高度",
    "imaging_locked": "成像锁定",
    "intrinsics": "相机内参",
    "color_imaging": "颜色标定",
    "robot_limits": "机械臂限位",
    "pose": "相机位姿",
    "cam_self": "焦距自估",
}

# (调试模式, 比赛模式) 默认级别 —— 计划 §A3 的表格
GATE_DEFAULT: Dict[str, Tuple[str, str]] = {
    "ref_points": ("warn", "fail"),
    "ref_collinear": ("warn", "fail"),
    "fit_quality": ("fail", "fail"),
    "residual_poly": ("warn", "warn"),
    "class_height": ("warn", "warn"),
    "imaging_locked": ("warn", "fail"),
    "intrinsics": ("warn", "warn"),
    "color_imaging": ("warn", "fail"),
    "robot_limits": ("warn", "warn"),
    "pose": ("fail", "fail"),
    "cam_self": ("warn", "warn"),
}

LEVELS: Tuple[str, ...] = ("off", "warn", "fail")

# 阈值 (全部可被 calib_config.json 顶层 gate_thresholds 覆盖; 不写死)
THRESHOLD_DEFAULTS: Dict[str, float] = {
    "min_ref_points": 3,             # ref_points: 至少几个参考点
    "collinear_area_ratio": 0.02,    # ref_collinear: 三角形面积 / 最长边^2 低于此值 = 近共线
    "collinear_min_angle_deg": 5.0,  # ref_collinear: 最小内角小于此值 = 近共线
    "fit_scale_lo": 0.97,            # fit_quality: 比例下限
    "fit_scale_hi": 1.03,            # fit_quality: 比例上限
    "fit_mean_err_mm": 2.0,          # fit_quality: 平均重投影误差上限
    "fit_max_err_mm": 4.0,           # fit_quality: 单点最大误差上限
    "class_height_ratio": 0.8,       # class_height: 占比超过此值 = 高度是占位值
    "residual_min_gain_mm": 0.05,    # residual_poly: 残差至少要把平均误差降这么多才值得开
}

_CUR_DIR = os.path.dirname(os.path.abspath(__file__))
CALIB_PATH = os.path.join(_CUR_DIR, "calib_config.json")
CLASSES_PATH = os.path.join(_CUR_DIR, "classes.json")


# ============================================================================ 工具
def _load_json(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _r(v: Any, n: int) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, n) if f == f else None


def _points(v) -> List[List[float]]:
    out: List[List[float]] = []
    for p in v or []:
        try:
            out.append([float(p[0]), float(p[1])])
        except (TypeError, ValueError, IndexError):
            continue
    return out


def ref_points_fp(data: Optional[Dict[str, Any]]) -> str:
    """
    参考点指纹 (计划 D1): 只由映射关系依赖的六项派生, 不掺时间。
    返回 sha1 前 12 位十六进制字符串。任何一项缺失都参与计算 (缺失 = None), 所以
    "补上内参" 也会改变指纹 -> 参考点判过期, 需要重新确认 —— 这是刻意的。
    """
    tray = (data or {}).get("tray") or {}
    fx = tray.get("fixed_px") or {}
    cm = (data or {}).get("camera_matrix")
    dc = (data or {}).get("dist_coeffs")
    payload = {
        "corners": [[_r(p[0], 3), _r(p[1], 3)] for p in (fx.get("corners") or tray.get("corners_px") or [])] or None,
        "camera_matrix": [[_r(x, 6) for x in row] for row in cm] if cm else None,
        "dist_coeffs": [_r(x, 6) for x in dc] if dc else None,
        "image_size": [int(x) for x in (fx.get("image_size") or (data or {}).get("image_size") or [])] or None,
        "fixed_z_mm": _r(tray.get("fixed_z_mm"), 4),
        "axes": list(tray.get("axes") or []) or None,
        "origin_mm": [_r(x, 4) for x in (tray.get("origin_mm") or [])] or None,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def imaging_fp(cam: Optional[Dict[str, Any]]) -> str:
    """
    成像指纹: 颜色标定 (HSV 区间) 只在同一套成像参数下有效。
    由 exposure_us / gain_db / wb_kelvin / 分辨率 / 设备 / fourcc / swap_rb 派生。
    classes.json 的 hsv.imaging_fp 由颜色标定流程写入 (A4 负责), 这里只负责比对。
    """
    cam = cam or {}
    payload = {
        "exposure_us": cam.get("exposure_us"),
        "gain_db": cam.get("gain_db"),
        "wb_kelvin": cam.get("wb_kelvin"),
        "size": [cam.get("width"), cam.get("height")],
        "device": str(cam.get("device", "")),
        "fourcc": str(cam.get("fourcc", "")),
        "swap_rb": bool(cam.get("swap_rb", False)),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def resolve_levels(gates_cfg: Optional[Dict[str, Any]] = None, race: bool = False,
                   env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """每条门禁的最终级别: 配置 > 环境变量 > 模式默认。"""
    env = os.environ if env is None else env
    gates_cfg = gates_cfg or {}
    out: Dict[str, str] = {}
    for gid in GATE_IDS:
        d = GATE_DEFAULT[gid][1 if race else 0]
        v = gates_cfg.get(gid, None)
        ev = env.get("IS_GATE_" + gid.upper())
        if ev:
            v = ev
        if v is not None:
            v = str(v).strip().lower()
            if v not in LEVELS:
                logger.warning("门禁 %s 的级别 %r 不认识 (只支持 %s), 按默认 %s", gid, v, "|".join(LEVELS), d)
                v = None
            else:
                d = v
        out[gid] = d
    return out


def thresholds_from(data: Optional[Dict[str, Any]],
                    env: Optional[Dict[str, str]] = None) -> Dict[str, float]:
    """阈值: calib_config.json 的 gate_thresholds 覆盖默认值。"""
    env = os.environ if env is None else env
    out = dict(THRESHOLD_DEFAULTS)
    got = ((data or {}).get("gate_thresholds") or {})
    if isinstance(got, dict):
        for k, v in got.items():
            if k not in out:
                logger.warning("未知门禁阈值 %s (忽略)", k)
                continue
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                logger.warning("门禁阈值 %s=%r 不是数值 (忽略)", k, v)
    for k in list(out):
        ev = env.get("IS_GATE_TH_" + k.upper())
        if ev:
            try:
                out[k] = float(ev)
            except ValueError:
                pass
    return out


# ============================================================================ 报告
@dataclass
class GateItem:
    id: str
    level: str            # off | ok | warn | fail
    configured: str       # 配置/模式解析出来的 off|warn|fail
    text: str
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def blocks(self) -> bool:
        return self.level == "fail"

    def line(self) -> str:
        tag = {"off": "[off ]", "ok": "[ ok ]", "warn": "[warn]", "fail": "[FAIL]"}[self.level]
        return f"{tag} {self.id:<16} {self.text}"

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "level": self.level, "configured": self.configured,
                "text": self.text, "detail": self.detail}


@dataclass
class GateReport:
    race: bool = False
    items: List[GateItem] = field(default_factory=list)
    thresholds: Dict[str, float] = field(default_factory=dict)
    sources: Dict[str, Any] = field(default_factory=dict)

    @property
    def fails(self) -> List[GateItem]:
        return [i for i in self.items if i.level == "fail"]

    @property
    def warns(self) -> List[GateItem]:
        return [i for i in self.items if i.level == "warn"]

    @property
    def oks(self) -> List[GateItem]:
        return [i for i in self.items if i.level == "ok"]

    @property
    def ok(self) -> bool:
        """True = 可以启动/发送 (fail 级一条都没有)。"""
        return not self.fails

    def problems(self) -> List[str]:
        """中文可读清单, 直接并入 self_check()['problems'] (不含 off 的项)。"""
        return [f"{GATE_LABEL.get(i.id, i.id)}: {i.text}" for i in self.items if i.level in ("warn", "fail")]

    def lines(self) -> List[str]:
        head = "视觉门禁 (%s, 共 %d 项): 通过 %d / 提醒 %d / 拦截 %d" % (
            "比赛模式" if self.race else "调试模式", len(self.items),
            len(self.oks), len(self.warns), len(self.fails))
        return [head] + [i.line() for i in self.items]

    def as_dict(self) -> Dict[str, Any]:
        return {"race": self.race, "ok": self.ok,
                "counts": {"ok": len(self.oks), "warn": len(self.warns), "fail": len(self.fails)},
                "items": [i.as_dict() for i in self.items],
                "thresholds": self.thresholds, "sources": self.sources}


# ============================================================================ 上下文
@dataclass
class Ctx:
    """门禁需要的全部输入。任何一项拿不到都不算 "通过", 也不算 "拦截" (记 warn + 说明)。"""
    calib_data: Dict[str, Any] = field(default_factory=dict)
    calib_path: str = ""
    tray_cfg: Dict[str, Any] = field(default_factory=dict)
    tray_status: Dict[str, Any] = field(default_factory=dict)
    camera_matrix: Any = None
    dist_coeffs: Any = None
    image_size: Any = None
    reproj_error_mm: Optional[float] = None
    pose: Dict[str, Any] = field(default_factory=dict)
    vcfg_camera: Dict[str, Any] = field(default_factory=dict)
    vcfg_color: Dict[str, Any] = field(default_factory=dict)
    vcfg_source: str = ""
    classes: Dict[str, Any] = field(default_factory=dict)
    race: bool = False

    def ref_points(self) -> List[Dict[str, Any]]:
        out = []
        for r in (self.tray_cfg.get("ref_points") or []):
            if isinstance(r, dict) and r.get("tray_mm") and r.get("robot_mm"):
                out.append(r)
        return out


def build_context(service=None, calib_path: Optional[str] = None,
                  vision_config_path: Optional[str] = None, classes_path: Optional[str] = None,
                  race: Optional[bool] = None, env: Optional[Dict[str, str]] = None) -> Ctx:
    """从运行中的 service 或纯配置文件搭上下文 (离线也能跑门禁, 不打开相机)。"""
    env = os.environ if env is None else env
    ctx = Ctx()
    if race is None:
        try:
            from .config import race_mode
            race = race_mode()
        except Exception:
            race = os.path.exists(env.get("IS_COMPETITION_LOCK") or "/tmp/is_competition.lock")
    ctx.race = bool(race)

    calib = getattr(service, "calibrator", None) if service is not None else None
    ctx.calib_path = (getattr(calib, "config_path", "") if calib is not None else "") \
        or calib_path or env.get("IS_CALIB_CONFIG") or CALIB_PATH
    ctx.calib_data = _load_json(ctx.calib_path)

    if calib is not None:
        ctx.tray_cfg = dict(getattr(calib, "tray_cfg", {}) or {})
        ctx.tray_status = dict(getattr(calib, "tray_status", {}) or {})
        ctx.camera_matrix = getattr(calib, "camera_matrix", None)
        ctx.dist_coeffs = getattr(calib, "dist_coeffs", None)
        ctx.image_size = getattr(calib, "image_size", None)
        ctx.reproj_error_mm = getattr(calib, "reproj_error_mm", None)
        ctx.pose = dict(ctx.tray_status.get("perspective") or {})
    else:
        # 离线: 用标定文件本身 (tray 分支能算出 scale/rms/per_point, 不需要画面)
        ctx.camera_matrix = ctx.calib_data.get("camera_matrix")
        ctx.dist_coeffs = ctx.calib_data.get("dist_coeffs")
        ctx.image_size = ((ctx.calib_data.get("tray") or {}).get("fixed_px") or {}).get("image_size") \
            or ctx.calib_data.get("image_size")
        ctx.tray_cfg = dict(ctx.calib_data.get("tray") or {})
        try:
            from .calibration import CoordinateCalibrator
            c = CoordinateCalibrator(ctx.calib_path)
            ctx.tray_cfg = dict(c.tray_cfg or {})
            ctx.tray_status = dict(c.tray_status or {})
            ctx.reproj_error_mm = c.reproj_error_mm
            ctx.pose = dict(ctx.tray_status.get("perspective") or {})
            if c.camera_matrix is not None:
                ctx.camera_matrix = c.camera_matrix
                ctx.dist_coeffs = c.dist_coeffs
            if c.image_size:
                ctx.image_size = list(c.image_size)
        except Exception as e:
            logger.warning("离线加载标定失败 (%s), 门禁按配置文件判定", e)

    # 视觉配置 (相机成像参数 + 颜色来源)
    vcfg = getattr(service, "cfg", None) if service is not None else None
    if vcfg is not None:
        ctx.vcfg_camera = dict(getattr(vcfg, "camera", {}) or {})
        ctx.vcfg_color = dict(getattr(vcfg, "color", {}) or {})
        ctx.vcfg_source = str(getattr(vcfg, "source", "") or "")
    else:
        try:
            from .config import load_vision_config
            v = load_vision_config(vision_config_path)
            ctx.vcfg_camera = dict(v.camera or {})
            ctx.vcfg_color = dict(v.color or {})
            ctx.vcfg_source = str(v.source or "")
        except Exception as e:
            logger.warning("离线加载视觉配置失败 (%s)", e)

    ctx.classes = _load_json(classes_path or env.get("IS_VISION_CLASSES")
                             or env.get("IS_CLASSES_JSON") or CLASSES_PATH)
    return ctx


# ============================================================================ 11 条检查
def _g_ref_points(ctx: Ctx, th: Dict[str, float]) -> Tuple[Optional[bool], str, Dict[str, Any]]:
    pts = ctx.ref_points()
    need = int(th["min_ref_points"])
    fp_now = ref_points_fp(ctx.calib_data)
    fp_saved = ctx.calib_data.get("ref_points_fp")
    det = {"points": len(pts), "need": need, "fp_now": fp_now, "fp_saved": fp_saved,
           "verified_at": ctx.calib_data.get("ref_points_verified_at"),
           "calib_path": ctx.calib_path}
    if len(pts) < need:
        return False, f"参考点不足: 只有 {len(pts)} 个 (需要 >= {need}); ref_points_fp={fp_saved or '缺失'}", det
    if not fp_saved:
        return False, (f"参考点未标定: 有 {len(pts)} 个参考点但 calib_config.json 缺 ref_points_fp 指纹, "
                       f"无法证明它们对应当前四角/内参/分辨率/高度/axes/origin (按未标定处理); "
                       f"当前应为 {fp_now} —— 现场确认后跑 `python3 -m vision.gates --seal` 盖章"), det
    if str(fp_saved) != fp_now:
        return False, (f"参考点已过期: 指纹 {fp_saved} != 当前 {fp_now} "
                       f"(四角/内参/分辨率/fixed_z_mm/axes/origin_mm 改过), 需重新点参考点并盖章"), det
    return True, f"参考点 {len(pts)} 个, 指纹 {fp_now} 与当前配置一致" \
                 f"(确认于 {det['verified_at'] or '未记录, 只作展示'})", det


def _g_ref_collinear(ctx: Ctx, th: Dict[str, float]) -> Tuple[Optional[bool], str, Dict[str, Any]]:
    pts = ctx.ref_points()
    if len(pts) < 3:
        return None, f"参考点少于 3 个 ({len(pts)}), 共线判定不适用", {"points": len(pts)}
    for which in ("robot_mm", "tray_mm"):
        P = _points([p.get(which) for p in pts])
        if len(P) < 3:
            continue
        try:
            import math
            best = None
            for i in range(len(P)):
                a, b, c = P[i], P[(i + 1) % len(P)], P[(i + 2) % len(P)]
                v1 = (b[0] - a[0], b[1] - a[1])
                v2 = (c[0] - a[0], c[1] - a[1])
                area = abs(v1[0] * v2[1] - v1[1] * v2[0]) / 2.0
                sides = [math.dist(a, b), math.dist(b, c), math.dist(c, a)]
                mx = max(sides) or 1e-9
                ratio = area / (mx * mx)
                angs = []
                for p, q, r in ((a, b, c), (b, c, a), (c, a, b)):
                    u = (p[0] - q[0], p[1] - q[1])
                    w = (r[0] - q[0], r[1] - q[1])
                    nu, nw = math.hypot(*u) or 1e-9, math.hypot(*w) or 1e-9
                    angs.append(math.degrees(math.acos(max(-1.0, min(1.0, (u[0] * w[0] + u[1] * w[1]) / (nu * nw))))))
                cand = {"ratio": round(ratio, 5), "min_angle_deg": round(min(angs), 2),
                        "triplet": [a, b, c], "coords": which}
                if best is None or cand["ratio"] < best["ratio"]:
                    best = cand
            if best and (best["ratio"] < float(th["collinear_area_ratio"])
                         or best["min_angle_deg"] < float(th["collinear_min_angle_deg"])):
                return False, (f"参考点近似共线 ({which}): 最小三角面积比 {best['ratio']} < "
                               f"{th['collinear_area_ratio']} 或最小内角 {best['min_angle_deg']}° < "
                               f"{th['collinear_min_angle_deg']}°; 三点共线时刚体解不稳, 误差会放大"), \
                       {"worst": best, "points": len(pts)}
        except Exception as e:
            return None, f"共线判定失败: {e}", {"points": len(pts)}
    return True, f"{len(pts)} 个参考点不共线 (三角面积比/最小内角都在阈值内)", {"points": len(pts)}


def _g_fit_quality(ctx: Ctx, th: Dict[str, float]) -> Tuple[Optional[bool], str, Dict[str, Any]]:
    ts = ctx.tray_status or {}
    scale = ts.get("scale")
    mean_err = ctx.reproj_error_mm
    per = ts.get("per_point_mm") or []
    mx = max(per) if per else None
    det = {"scale": scale, "mean_err_mm": None if mean_err is None else round(float(mean_err), 2),
           "max_err_mm": mx, "per_point_mm": per, "rigid_rms_mm": ts.get("rigid_rms_mm"),
           "thresholds": {k: th[k] for k in ("fit_scale_lo", "fit_scale_hi",
                                             "fit_mean_err_mm", "fit_max_err_mm")}}
    bad: List[str] = []
    if scale is not None and not (float(th["fit_scale_lo"]) <= float(scale) <= float(th["fit_scale_hi"])):
        bad.append(f"拟合比例 {float(scale):.4f} 不在 {th['fit_scale_lo']}~{th['fit_scale_hi']} "
                   f"(参考点间距与托盘尺寸不符)")
    if mean_err is None:
        if scale is None:
            return None, "无参考点/拟合结果, 拟合质量无法判定", det
    else:
        if float(mean_err) > float(th["fit_mean_err_mm"]):
            bad.append(f"平均重投影误差 {float(mean_err):.2f}mm > {th['fit_mean_err_mm']}mm")
    if mx is not None and float(mx) > float(th["fit_max_err_mm"]):
        bad.append(f"单点最大误差 {float(mx):.2f}mm > {th['fit_max_err_mm']}mm")
    if bad:
        return False, "; ".join(bad), det
    return True, (f"拟合比例 {scale if scale is None else round(float(scale), 4)}, "
                  f"平均误差 {det['mean_err_mm']}mm (<= {th['fit_mean_err_mm']}), "
                  f"最大误差 {mx}mm (<= {th['fit_max_err_mm']})"), det


def _g_residual_poly(ctx: Ctx, th: Dict[str, float]) -> Tuple[Optional[bool], str, Dict[str, Any]]:
    ts = ctx.tray_status or {}
    eff = ts.get("residual_effect") or {}
    order = ts.get("residual")
    det = {"order": order, "effect": eff, "min_gain_mm": th["residual_min_gain_mm"]}
    if not eff:
        return True, f"未使用残差多项式 (参考点 <5 个或 residual=none), 无需判定", det
    if not eff.get("applied"):
        return False, (f"残差多项式无收益, 已自动停用: 开残差后平均误差 "
                       f"{eff.get('with_mm')}mm 反而 >= 不开的 {eff.get('without_mm')}mm "
                       f"(需要至少降 {th['residual_min_gain_mm']}mm); 标定侧已不再应用残差"), det
    return True, (f"残差多项式(order={eff.get('order')})有效: 平均误差 {eff.get('without_mm')}mm -> "
                  f"{eff.get('with_mm')}mm"), det


def _g_class_height(ctx: Ctx, th: Dict[str, float]) -> Tuple[Optional[bool], str, Dict[str, Any]]:
    cs = (ctx.classes or {}).get("classes") or []
    hs: Dict[str, int] = {}
    for c in cs:
        try:
            k = str(round(float((c or {}).get("height_mm")), 3))
        except (TypeError, ValueError):
            k = "None"
        hs[k] = hs.get(k, 0) + 1
    det = {"n_classes": len(cs), "height_hist": hs, "ratio_threshold": th["class_height_ratio"]}
    if not cs:
        return None, "classes.json 没有 classes 列表, 无法判定", det
    top, cnt = max(hs.items(), key=lambda kv: kv[1])
    ratio = cnt / float(len(cs))
    if ratio >= float(th["class_height_ratio"]):
        return False, (f"类别高度是占位值: {cnt}/{len(cs)} ({ratio:.0%}) 类 height_mm 都是 {top}mm "
                       f"(>= {th['class_height_ratio']:.0%}); 高度只用于视差补偿, 未逐类标定会影响抓取精度"), det
    return True, f"类别高度分布不单一 (最高占比 {ratio:.0%} = {top}mm), 不像占位值", det


def _g_imaging_locked(ctx: Ctx, th: Dict[str, float]) -> Tuple[Optional[bool], str, Dict[str, Any]]:
    cam = ctx.vcfg_camera or {}
    keys = ("exposure_us", "gain_db", "wb_kelvin")
    nulls = [k for k in keys if cam.get(k) is None]
    det = {k: cam.get(k) for k in keys}
    det["vision_config"] = ctx.vcfg_source
    if nulls:
        return False, (f"成像未锁定: {', '.join(nulls)} 为 null (相机在自动曝光/自动白平衡下, "
                       f"颜色与阈值每帧都在漂); 现场确认后写进 vision_config.json camera 段"
                       f"或 IS_CAM_EXPOSURE/IS_CAM_GAIN/IS_CAM_WB"), det
    return True, f"成像已锁定: exposure={cam.get('exposure_us')}us gain={cam.get('gain_db')}dB " \
                 f"wb={cam.get('wb_kelvin')}K", det


def _g_intrinsics(ctx: Ctx, th: Dict[str, float]) -> Tuple[Optional[bool], str, Dict[str, Any]]:
    cm, dc = ctx.camera_matrix, ctx.dist_coeffs
    det = {"camera_matrix": None if cm is None else "有", "dist_coeffs": None if dc is None else "有",
           "calib_path": ctx.calib_path}
    if cm is None:
        return False, ("无内参: calib_config.json 没有 camera_matrix/dist_coeffs, 不做畸变校正; "
                       "广角镜头托盘边缘会偏 (四角与参考点对不上时优先做这个)"), det
    return True, "有内参+畸变系数", det


def _g_color_imaging(ctx: Ctx, th: Dict[str, float]) -> Tuple[Optional[bool], str, Dict[str, Any]]:
    color = ctx.vcfg_color or {}
    src = str(color.get("source") or "hsv").strip().lower()
    hsv = dict((ctx.classes or {}).get("hsv") or {})
    hsv.update(color.get("hsv") or {})
    saved = (color.get("imaging_fp") or (color.get("hsv") or {}).get("imaging_fp")
             or (ctx.classes or {}).get("imaging_fp") or hsv.get("imaging_fp"))
    now = imaging_fp(ctx.vcfg_camera)
    det = {"source": src, "imaging_fp_saved": saved, "imaging_fp_now": now,
           "hsv_keys": sorted(hsv.keys()), "camera": {k: (ctx.vcfg_camera or {}).get(k)
                                                      for k in ("exposure_us", "gain_db", "wb_kelvin")}}
    if src != "hsv":
        return True, f"颜色来源 source={src} (不是 hsv), 成像一致性不适用", det
    if not hsv:
        return False, "颜色未标定: 没有 hsv 区间 (classes.json 的 hsv 段为空)", det
    if not saved:
        return False, ("颜色未标定: 有 hsv 区间但缺 imaging_fp (不知道这套区间是在什么曝光/增益/白平衡下"
                       "采的); 换个照明或自动曝光漂了就会误判颜色"), det
    if str(saved) != now:
        return False, (f"颜色标定与当前成像不一致: imaging_fp {saved} != 当前 {now} "
                       f"(曝光/增益/白平衡/分辨率变了), 需在锁定成像后重新采色"), det
    return True, f"颜色标定与当前成像一致 (imaging_fp {now})", det


def _g_robot_limits(ctx: Ctx, th: Dict[str, float]) -> Tuple[Optional[bool], str, Dict[str, Any]]:
    from . import limits as limitsmod
    raw = ctx.calib_data.get("robot_limits_mm")
    lim = limitsmod.parse_limits(raw)
    margin = ctx.tray_cfg.get("margin_mm")
    det = {"robot_limits_mm": raw, "parsed": lim,
           "tray_margin_mm": ctx.calib_data.get("tray_margin_mm"),
           "tray_margin_action": ctx.calib_data.get("tray_margin_action") or "warn"}
    if lim is None:
        return False, ("机械臂限位未配置 (robot_limits_mm=null): 发送前不会拦截越界坐标 "
                       "(调试够用; 比赛前建议填 {x_min,x_max,y_min,y_max})"), det
    return True, ("机械臂限位 x[%.1f,%.1f] y[%.1f,%.1f]" % (lim["x_min"], lim["x_max"],
                                                            lim["y_min"], lim["y_max"])), det


def _g_pose(ctx: Ctx, th: Dict[str, float]) -> Tuple[Optional[bool], str, Dict[str, Any]]:
    pose = ctx.pose or {}
    det = dict(pose)
    method = pose.get("method")
    if not pose:
        return None, "没有位姿信息 (托盘模式未启用 / 还没跑过托盘定位), 无法判定", det
    if pose.get("ok") is False:
        return False, (f"estimate_pose() 返回 ok=False: {pose.get('msg') or '求解失败'} "
                       f"(method={method}, tilt={pose.get('tilt_deg')}°, height={pose.get('height_mm')}mm); "
                       f"已退回平面单应, 相机斜看时高度视差会有偏差"), det
    if method != "pose":
        return None, (f"透视位姿未启用 (method={method}): {pose.get('msg') or ''}"), det
    return True, (f"位姿求解正常: method=pose source={pose.get('source')} tilt={pose.get('tilt_deg')}° "
                  f"height={pose.get('height_mm')}mm f={pose.get('f_px')}px"), det


def _g_cam_self(ctx: Ctx, th: Dict[str, float]) -> Tuple[Optional[bool], str, Dict[str, Any]]:
    pose = ctx.pose or {}
    src = pose.get("source")
    det = {"source": src, "tilt_deg": pose.get("tilt_deg"), "f_px": pose.get("f_px"),
           "min_tilt_deg": 12.0}
    if src is None:
        return None, "没有焦距来源信息 (托盘模式未启用), 无法判定", det
    if src == "self":
        return False, (f"焦距是托盘四角自估的 (source=self, tilt={pose.get('tilt_deg')}°): "
                       f"倾角小时不准; 建议填 tray.focal_px 或做一次棋盘格标定 (内参), "
                       f"或填准 cam_height_mm 让焦距按镜头高反推"), det
    return True, f"焦距来源 source={src} (不是四角自估)", det


_CHECKS = {
    "ref_points": _g_ref_points,
    "ref_collinear": _g_ref_collinear,
    "fit_quality": _g_fit_quality,
    "residual_poly": _g_residual_poly,
    "class_height": _g_class_height,
    "imaging_locked": _g_imaging_locked,
    "intrinsics": _g_intrinsics,
    "color_imaging": _g_color_imaging,
    "robot_limits": _g_robot_limits,
    "pose": _g_pose,
    "cam_self": _g_cam_self,
}


# ============================================================================ 主入口
def evaluate(ctx: Optional[Ctx] = None, race: Optional[bool] = None,
             gates_cfg: Optional[Dict[str, Any]] = None,
             calib_data: Optional[Dict[str, Any]] = None) -> GateReport:
    """跑全部 11 条门禁。ctx 缺省时自动搭一个 (离线, 不打开相机)。"""
    if ctx is None:
        ctx = build_context(race=race)
    if race is None:
        race = ctx.race
    data = calib_data if calib_data is not None else ctx.calib_data
    if gates_cfg is None:
        gates_cfg = dict((data or {}).get("gates") or {})
    levels = resolve_levels(gates_cfg, race=bool(race))
    th = thresholds_from(data)
    rep = GateReport(race=bool(race), thresholds=th,
                     sources={"calib": ctx.calib_path, "vision_config": ctx.vcfg_source,
                              "classes": sorted((ctx.classes or {}).keys()),
                              "gates_cfg": gates_cfg})
    for gid in GATE_IDS:
        configured = levels[gid]
        if configured == "off":
            rep.items.append(GateItem(gid, "off", configured, "已关闭 (gates.%s=off)" % gid, {}))
            continue
        try:
            ok, text, detail = _CHECKS[gid](ctx, th)
        except Exception as e:
            ok, text, detail = None, f"检查异常: {e}", {"error": repr(e)}
        if ok is True:
            lvl = "ok"
        elif ok is False:
            lvl = configured           # warn 或 fail
        else:
            lvl = "warn"               # 无法判定一律不拦截
            text = "无法判定: " + text
        rep.items.append(GateItem(gid, lvl, configured, text, detail))
    return rep


def check(service=None, **kw) -> GateReport:
    return evaluate(build_context(service=service, **kw))


def race_banner(rep: GateReport) -> str:
    if rep.race:
        return "比赛模式 (锁文件存在): 有拦截项就必须停下来, 不要带着 %d 项 fail 开赛" % len(rep.fails)
    return "调试模式 (无锁文件): 拦截项只打印, 不阻止启动"


# ============================================================================ CLI
def _print(rep: GateReport, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(rep.as_dict(), ensure_ascii=False, indent=2))
        return
    for ln in rep.lines():
        print(ln)
    print(race_banner(rep))
    if rep.fails:
        print("失败项 (必须处理): " + "; ".join(f"{i.id}={i.text}" for i in rep.fails))


def seal(calib_path: str, stamp: Optional[str] = None) -> Dict[str, Any]:
    """现场确认参考点: 把当前指纹写进 calib_config.json (保留其它键不动)。"""
    data = _load_json(calib_path)
    if not data:
        raise SystemExit("找不到/读不了标定文件: %s" % calib_path)
    fp = ref_points_fp(data)
    data["ref_points_fp"] = fp
    if stamp:
        data["ref_points_verified_at"] = stamp
    elif not data.get("ref_points_verified_at"):
        data["ref_points_verified_at"] = ""
    tmp = calib_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, calib_path)
    logger.info("参考点指纹已写入 %s: %s", calib_path, fp)
    return {"ok": True, "ref_points_fp": fp, "path": calib_path}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="vision.gates", description="视觉自检门禁 (计划 A3)")
    ap.add_argument("--check", action="store_true", help="跑门禁, 退出码 0=放行 1=有拦截项")
    ap.add_argument("--json", action="store_true", help="输出 JSON (隐含 --check)")
    ap.add_argument("--seal", action="store_true", help="现场确认参考点: 写 ref_points_fp")
    ap.add_argument("--at", default=None, help="--seal 时写 ref_points_verified_at (展示用)")
    ap.add_argument("--calib", default=None, help="标定文件路径 (默认 vision/calib_config.json)")
    ap.add_argument("--vision-config", default=None, help="vision_config.json 路径")
    ap.add_argument("--classes", default=None, help="classes.json 路径")
    ap.add_argument("--quiet", action="store_true", help="只输出失败项")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if args.seal:
        out = seal(args.calib or os.environ.get("IS_CALIB_CONFIG") or CALIB_PATH, args.at)
        print("参考点已盖章: %(path)s ref_points_fp=%(ref_points_fp)s" % out)
        return 0

    ctx = build_context(calib_path=args.calib, vision_config_path=args.vision_config,
                        classes_path=args.classes)
    rep = evaluate(ctx)
    if args.quiet:
        for i in rep.fails + rep.warns:
            print(i.line())
        if rep.fails:
            print("失败项: " + "; ".join(f"{i.id}={i.text}" for i in rep.fails))
    else:
        _print(rep, as_json=args.json)
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())