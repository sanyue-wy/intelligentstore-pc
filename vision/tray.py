"""
白色托盘定位 + 托盘坐标系 (vision/calib_config.json 的 "tray" 段)

思路: 托盘本身就是标定板。每次 scan 找到白色托盘的四条边, 用已知的内框尺寸
把像素映射到「托盘坐标 (mm)」, 再用固定参考点把托盘坐标平移/旋转到机械臂坐标。
相机被碰歪、换支架、重新装相机后不用重新做 9 点标定。

托盘坐标系约定 (从相机画面看):
    原点 = 画面左上方那个内框角 (托盘底面内壁的角)
    +x  = 沿画面向右的那条内边, 长度 inner_w_mm
    +y  = 沿画面向下的那条内边, 长度 inner_h_mm
用尺子量参考点到这两条内边的距离, 就是它的托盘坐标 (xt, yt)。

检测流程 (detect_tray):
    1. 白色分割: HSV 低饱和 + 高亮度 (亮度阈值默认在低饱和像素上自动 Otsu)
    2. 大核闭运算填掉托盘上货物留下的洞, 取最大外轮廓
    3. 轮廓点先去畸变 (有内参时), 凸包 -> 粗四边形
    4. 每条边取贴着凸包边的轮廓点 (被货物挡住凹进去的点自动排除),
       Huber 鲁棒直线拟合, 相邻两线求交 = 亚像素角点
    5. 长宽比与 (W+2*rim):(H+2*rim) 比对, 面积占比检查
TrayTracker: 多帧中值; 单帧跳变 (手伸进画面) 忽略; 连续 confirm 帧一致的新位置才接受
             (相机被碰后自动重新锁定并告警)。

estimate_pose(): 托盘四角单应 + 焦距 -> 相机位姿 (倾角/高度), height_homography() 给出
               「高度 h 平面 -> 像素」的单应, 用于斜看相机的高度视差。
snap_corners(): 手点的四角吸附到白色托盘边缘 (亚像素)。

estimate_k1(): 没有棋盘格时, 用「托盘四条边应该是直线」估计径向畸变 k1 (铅垂线法)。
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)

TRAY_DEFAULTS: Dict = {
    "enabled": False,
    "inner_w_mm": 0.0,          # 内框沿画面左右方向的长度
    "inner_h_mm": 0.0,          # 内框沿画面上下方向的长度
    "rim_mm": 0.0,              # 白色区域外边界到内框的距离 (托盘壁厚+外沿); 外围贴黑边时白色区域含壁顶
    "rim_height_mm": 0.0,       # 壁顶比托盘底面高多少 (配合 cam_height_mm 消除壁顶视差)
    "axes": ["tray_x", "tray_y"],   # 机械臂 +X, +Y 分别对应托盘哪个方向 (tray_x/-tray_x/tray_y/-tray_y)
    "ref_points": [],           # [{"tray_mm":[xt,yt], "robot_mm":[xr,yr]}], 1 点需 axes, >=2 点自动求旋转
    "residual": "auto",         # auto: >=5 点一阶, >=9 点二阶多项式残差修正; none 关闭
    "white_s_max": 70,
    "white_v_min": 0,           # 0 = 自动
    "min_area_ratio": 0.10,
    "aspect_tol": 0.08,
    "jump_px": 4.0,
    "confirm": 3,
    "history": 7,
    "redetect_every": 15,       # 连续预览时每 N 帧复查一次托盘
    "auto_roi": True,           # 未手动设 ROI 时, 用托盘内框 (外扩 roi_margin_mm) 当 ROI
    "roi_margin_mm": 6.0,
    "corners_px": [],           # 上次锁定的角点 (去畸变像素, 壁顶), 启动时先用
    # ---- 固定托盘位置 (填了就不再自动检测托盘)
    # 托盘内框在画面里的像素位置, 坐标系: 画面左上角为 (0,0), x 向右, y 向下 (原始画面像素)。
    #   {"x": 左上角x, "y": 左上角y, "w": 内框宽像素, "h": 内框高像素}
    #   或 {"x1","y1","x2","y2"} 左上角+右下角, 或 {"corners": [[左上],[右上],[右下],[左下]]}
    #   可选 "image_size": [宽, 高] 量取时的画面分辨率, 分辨率不同时自动按比例换算。
    "fixed_px": {},
    # 托盘内框左上角 (托盘原点) 在输出坐标系里的位置 mm; 没有参考点时 输出 = origin_mm + axes(托盘坐标)。
    "origin_mm": None,
    # fixed_px 四角在哪个高度: 0 = 托盘底面内角 (默认); 点的是壁顶内沿时填壁高
    "fixed_z_mm": 0.0,
    # ---- 透视 (相机斜看托盘)
    # auto: 由托盘四角单应 + 焦距求出相机位姿, 按真实三维几何把「高度 h 处的像点」投回托盘坐标;
    #       相机斜看时比按光心径向缩放的旧视差公式准得多。求不出位姿时自动退回旧公式。
    # off : 只用旧公式 (principal_px + cam_height_mm)。
    "perspective": "auto",
    # 焦距 (像素), 一般不用填: 优先用棋盘格完整标定的焦距 > 用 cam_height_mm 反推 > 由托盘四角自估 (相机倾斜 >=12°)
    "focal_px": None,
}

_AXES = {"tray_x": (1, 0), "-tray_x": (-1, 0), "tray_y": (0, 1), "-tray_y": (0, -1)}


def merge_cfg(cfg: Optional[Dict]) -> Dict:
    out = dict(TRAY_DEFAULTS)
    out.update(cfg or {})
    return out


def tray_ready(cfg: Dict) -> bool:
    return bool(cfg.get("enabled")) and float(cfg.get("inner_w_mm") or 0) > 0 and float(cfg.get("inner_h_mm") or 0) > 0


def inner_tray_pts(cfg: Dict) -> np.ndarray:
    """内框四角在托盘坐标里的位置, 顺序 TL TR BR BL。"""
    W, H = float(cfg["inner_w_mm"]), float(cfg["inner_h_mm"])
    return np.float32([[0, 0], [W, 0], [W, H], [0, H]])


def fixed_corners_px(cfg: Dict, frame_wh: Optional[Tuple[int, int]] = None) -> Optional[np.ndarray]:
    """
    fixed_px -> 内框四角原始像素 (TL TR BR BL); 没填或填错返回 None。
    frame_wh 与 fixed_px.image_size 不同时按比例缩放。
    """
    f = cfg.get("fixed_px") or {}
    if not isinstance(f, dict) or not f:
        return None
    try:
        if f.get("corners"):
            q = np.float32(f["corners"]).reshape(4, 2)
        else:
            x, y = float(f["x"] if "x" in f else f["x1"]), float(f["y"] if "y" in f else f["y1"])
            if "w" in f:
                w, h = float(f["w"]), float(f["h"])
            else:
                w, h = float(f["x2"]) - x, float(f["y2"]) - y
            if w <= 0 or h <= 0:
                raise ValueError("宽高必须 > 0")
            q = np.float32([[x, y], [x + w, y], [x + w, y + h], [x, y + h]])
    except Exception as e:
        logger.error("tray.fixed_px 格式错误 (%s): %s", e, f)
        return None
    size = f.get("image_size")
    if size and frame_wh and tuple(int(v) for v in size) != tuple(frame_wh):
        q = q * np.float32([frame_wh[0] / float(size[0]), frame_wh[1] / float(size[1])])
    return q


def outer_tray_pts(cfg: Dict) -> np.ndarray:
    """白色区域四角在托盘坐标里的位置, 顺序 TL TR BR BL。"""
    W, H, r = float(cfg["inner_w_mm"]), float(cfg["inner_h_mm"]), float(cfg.get("rim_mm") or 0)
    return np.float32([[-r, -r], [W + r, -r], [W + r, H + r], [-r, H + r]])


# ============================================================================ 检测
@dataclass
class TrayResult:
    ok: bool
    corners: Optional[np.ndarray] = None       # 4x2 去畸变像素 (壁顶/白色外边界), TL TR BR BL
    aspect_err: float = 0.0
    area_ratio: float = 0.0
    msg: str = ""
    sides: List[np.ndarray] = field(default_factory=list)   # 每条边参与拟合的点 (去畸变像素)


def white_mask(frame: np.ndarray, s_max: int = 70, v_min: int = 0) -> np.ndarray:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    s, v = hsv[..., 1], hsv[..., 2]
    low_s = s <= s_max
    if v_min <= 0:
        vals = v[low_s]
        if vals.size < 100:
            return np.zeros(v.shape, np.uint8)
        t, _ = cv2.threshold(vals.reshape(-1, 1), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        v_min = max(int(t), 90)
    return ((low_s & (v >= v_min)) * 255).astype(np.uint8)


def _largest_blob(mask: np.ndarray) -> Optional[np.ndarray]:
    h, w = mask.shape
    k = max(5, int(min(h, w) * 0.035) | 1)
    m = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cs:
        return None
    return max(cs, key=cv2.contourArea).reshape(-1, 2).astype(np.float32)


def _order(q: np.ndarray) -> np.ndarray:
    c = q.mean(0)
    ang = np.arctan2(q[:, 1] - c[1], q[:, 0] - c[0])
    q = q[np.argsort(ang)]
    i = int(np.argmin(q.sum(1)))
    return np.roll(q, -i, axis=0)


def _rough_quad(pts: np.ndarray) -> np.ndarray:
    hull = cv2.convexHull(pts.reshape(-1, 1, 2)).reshape(-1, 2)
    peri = cv2.arcLength(hull.reshape(-1, 1, 2), True)
    for f in np.linspace(0.01, 0.12, 23):
        ap = cv2.approxPolyDP(hull.reshape(-1, 1, 2), f * peri, True).reshape(-1, 2)
        if len(ap) == 4:
            return _order(ap.astype(np.float32))
    return _order(cv2.boxPoints(cv2.minAreaRect(hull.reshape(-1, 1, 2))).astype(np.float32))


def _side_points(pts: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    d = b - a
    L2 = float(d @ d)
    if L2 < 1:
        return np.empty((0, 2), np.float32)
    L = math.sqrt(L2)
    n = np.array([-d[1], d[0]]) / L
    rel = pts - a
    t = rel @ d / L2
    dist = np.abs(rel @ n)
    tol = max(3.0, 0.02 * L)
    return pts[(t > 0.12) & (t < 0.88) & (dist < tol)]


def _fit_line(sp: np.ndarray, a: np.ndarray, b: np.ndarray):
    if len(sp) >= 8:
        vx, vy, x0, y0 = cv2.fitLine(sp.reshape(-1, 1, 2), cv2.DIST_HUBER, 0, 0.01, 0.01).reshape(-1)
        return np.array([x0, y0]), np.array([vx, vy])
    d = b - a
    return a.astype(np.float64), d / (np.linalg.norm(d) + 1e-9)


def _intersect(p1, d1, p2, d2) -> Optional[np.ndarray]:
    A = np.array([[d1[0], -d2[0]], [d1[1], -d2[1]]], dtype=np.float64)
    if abs(np.linalg.det(A)) < 1e-6:
        return None
    s = np.linalg.solve(A, (p2 - p1).astype(np.float64))
    return p1 + s[0] * d1


def quad_from_points(pts: np.ndarray) -> Tuple[np.ndarray, List[np.ndarray]]:
    q = _rough_quad(pts)
    lines, sides = [], []
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        sp = _side_points(pts, a, b)
        sides.append(sp)
        lines.append(_fit_line(sp, a, b))
    out = []
    for i in range(4):
        c = _intersect(*lines[i - 1], *lines[i])
        out.append(q[i] if c is None else c)
    return np.float32(out), sides


def detect_tray(frame: np.ndarray, cfg: Dict,
                undistort: Optional[Callable[[np.ndarray], np.ndarray]] = None) -> TrayResult:
    """在一帧里找白色托盘, 返回去畸变像素下的四个角 (TL TR BR BL)。"""
    if frame is None or frame.size == 0:
        return TrayResult(False, msg="无图像")
    h, w = frame.shape[:2]
    mask = white_mask(frame, int(cfg.get("white_s_max", 70)), int(cfg.get("white_v_min", 0)))
    pts = _largest_blob(mask)
    if pts is None or len(pts) < 40:
        return TrayResult(False, msg="没有找到白色区域")
    area = cv2.contourArea(pts.reshape(-1, 1, 2)) / float(h * w)
    if area < float(cfg.get("min_area_ratio", 0.10)):
        return TrayResult(False, area_ratio=area, msg=f"白色区域只占画面 {area:.0%}, 不像托盘")
    if area > 0.97:
        return TrayResult(False, area_ratio=area, msg="整幅画面都是白色 (过曝或背景太亮, 托盘外围贴黑边)")
    if undistort is not None:
        pts = undistort(pts)
    corners, sides = quad_from_points(pts)
    top, bot = np.linalg.norm(corners[1] - corners[0]), np.linalg.norm(corners[2] - corners[3])
    lef, rig = np.linalg.norm(corners[3] - corners[0]), np.linalg.norm(corners[2] - corners[1])
    meas = (top + bot) / max(1e-6, lef + rig)
    o = outer_tray_pts(cfg)
    expect = (o[1, 0] - o[0, 0]) / max(1e-6, o[3, 1] - o[0, 1])
    err = abs(meas / expect - 1)
    res = TrayResult(True, corners, round(float(err), 4), round(float(area), 3), "", sides)
    tol = float(cfg.get("aspect_tol", 0.08))
    # 相机斜看: 对边长度明显不等 (梯形), 简单长宽比不再可靠, 放宽到 25%
    persp = max(abs(top - bot) / max(top, bot, 1e-6), abs(lef - rig) / max(lef, rig, 1e-6))
    if persp > 0.04:
        tol = max(tol, 0.25)
    if err > tol:
        res.ok = False
        res.msg = (f"长宽比 {meas:.3f} 与配置 {expect:.3f} 相差 {err:.0%} "
                   f"(尺寸填错 / 相机太斜 / 托盘边被挡住 / 托盘在画面里转了 >30°)")
    return res


# ============================================================================ 跟踪
class TrayTracker:
    def __init__(self, cfg: Dict):
        self.cfg = cfg
        self.hist: deque = deque(maxlen=int(cfg.get("history", 7)))
        self.pending: List[np.ndarray] = []
        self.stable: Optional[np.ndarray] = None
        if cfg.get("corners_px"):
            try:
                self.stable = np.float32(cfg["corners_px"]).reshape(4, 2)
            except Exception:
                self.stable = None
        self.last: Optional[TrayResult] = None
        self.fails = 0
        self.locked_live = False      # 本次运行是否已用实时画面锁定过
        self.event = ""

    def update(self, res: TrayResult) -> bool:
        """喂一次检测结果, 返回稳定角点是否变化。"""
        self.last = res
        self.event = ""
        if not res.ok:
            self.fails += 1
            return False
        self.fails = 0
        c = res.corners
        jump = float(self.cfg.get("jump_px", 4.0))
        ref = np.median(np.stack(self.hist), 0) if self.hist else None
        if ref is None and self.stable is not None and not self.locked_live:
            ref = self.stable
        if ref is None or float(np.abs(c - ref).max()) <= jump * (3 if not self.locked_live else 1):
            self.pending.clear()
            self.hist.append(c)
        else:
            self.pending.append(c)
            if len(self.pending) > 1 and float(np.abs(self.pending[-1] - self.pending[0]).max()) > jump:
                self.pending = [c]
            if len(self.pending) < int(self.cfg.get("confirm", 3)):
                return False
            moved = float(np.abs(np.median(np.stack(self.pending), 0) - ref).max())
            self.event = f"托盘/相机位置变化 {moved:.1f} px, 已重新锁定"
            logger.warning(self.event)
            self.hist.clear()
            self.hist.extend(self.pending)
            self.pending.clear()
        new = np.median(np.stack(self.hist), 0).astype(np.float32)
        changed = self.stable is None or float(np.abs(new - self.stable).max()) > 0.05
        self.stable = new
        self.locked_live = True
        return changed


# ============================================================================ 刚体
def axes_matrix(axes) -> np.ndarray:
    try:
        rx, ry = _AXES[str(axes[0]).strip()], _AXES[str(axes[1]).strip()]
    except Exception:
        raise ValueError(f"axes 写法不对: {axes} (每项为 tray_x/-tray_x/tray_y/-tray_y)")
    M = np.array([rx, ry], dtype=np.float64)
    if abs(np.linalg.det(M)) < 0.5:
        raise ValueError(f"axes {axes}: 机械臂 X/Y 不能对应托盘同一个方向")
    return M


def solve_rigid(tray_pts: np.ndarray, robot_pts: np.ndarray, axes=None) -> Dict:
    """
    求 robot = R·tray + t (R 为旋转或旋转+镜像, 不缩放)。
    1 点: R 取 axes; >=2 点: 两种镜像各做一次 Kabsch, 取残差小的。
    返回 {"R","t","rms","scale","angle_deg","mirror","per_point"}
    """
    P = np.asarray(tray_pts, np.float64).reshape(-1, 2)
    Q = np.asarray(robot_pts, np.float64).reshape(-1, 2)
    n = len(P)
    if n == 0:
        R = axes_matrix(axes or ["tray_x", "tray_y"])
        return {"R": R, "t": np.zeros(2), "rms": 0.0, "scale": 1.0, "per_point": [], "n": 0,
                "angle_deg": 0.0, "mirror": bool(np.linalg.det(R) < 0)}
    if n == 1:
        R = axes_matrix(axes or ["tray_x", "tray_y"])
        t = Q[0] - R @ P[0]
        return {"R": R, "t": t, "rms": 0.0, "scale": 1.0, "per_point": [0.0], "n": 1,
                "angle_deg": round(math.degrees(math.atan2(R[1, 0], R[0, 0])), 2),
                "mirror": bool(np.linalg.det(R) < 0)}
    best = None
    for mirror in (False, True):
        F = np.diag([1.0, -1.0]) if mirror else np.eye(2)
        Pm = P @ F.T
        pc, qc = Pm.mean(0), Q.mean(0)
        A, B = Pm - pc, Q - qc
        U, S, Vt = np.linalg.svd(A.T @ B)
        Rr = (U @ Vt).T
        if np.linalg.det(Rr) < 0:
            Vt[-1] *= -1
            Rr = (U @ Vt).T
        R = Rr @ F
        t = Q.mean(0) - R @ P.mean(0)
        e = np.linalg.norm((P @ R.T + t) - Q, axis=1)
        rms = float(np.sqrt((e ** 2).mean()))
        sp = np.linalg.norm(P - P.mean(0), axis=1).sum()
        scale = float(np.linalg.norm(Q - Q.mean(0), axis=1).sum() / sp) if sp > 1e-6 else 1.0
        if best is None or rms < best["rms"] - 1e-9:
            best = {"R": R, "t": t, "rms": rms, "scale": scale, "per_point": [round(float(x), 2) for x in e],
                    "n": n, "angle_deg": round(math.degrees(math.atan2(Rr[1, 0], Rr[0, 0])), 2),
                    "mirror": mirror}
    return best


def _poly_terms(xy: np.ndarray, order: int) -> np.ndarray:
    x, y = xy[:, 0] / 100.0, xy[:, 1] / 100.0
    cols = [np.ones_like(x), x, y]
    if order >= 2:
        cols += [x * x, x * y, y * y]
    return np.stack(cols, 1)


def fit_residual(pred: np.ndarray, true: np.ndarray, mode="auto") -> Optional[Dict]:
    """在机械臂坐标上拟合 pred -> true 的残差多项式 (镜头残余畸变/装配倾斜/机械臂误差一起补)。"""
    n = len(pred)
    if mode in ("none", None, False) or n < 5:
        return None
    order = 2 if n >= 9 else 1
    if isinstance(mode, int):
        order = min(order, mode)
    A = _poly_terms(np.asarray(pred, np.float64), order)
    d = np.asarray(true, np.float64) - np.asarray(pred, np.float64)
    cx, *_ = np.linalg.lstsq(A, d[:, 0], rcond=None)
    cy, *_ = np.linalg.lstsq(A, d[:, 1], rcond=None)
    return {"order": order, "cx": cx.tolist(), "cy": cy.tolist()}


def apply_residual(res: Optional[Dict], x: float, y: float) -> Tuple[float, float]:
    if not res:
        return x, y
    A = _poly_terms(np.array([[x, y]], np.float64), int(res["order"]))[0]
    return x + float(A @ np.array(res["cx"])), y + float(A @ np.array(res["cy"]))


# ============================================================================ 畸变
def estimate_k1(frame: np.ndarray, cfg: Dict) -> Dict:
    """
    铅垂线法: 找 k1 使托盘四条边最直。f 取 0.9*max(w,h), 光心取画面中心
    (k1 与 f 耦合, 只要保存时一起写入 camera_matrix 即自洽)。
    返回 {"ok", "k1", "camera_matrix", "dist_coeffs", "rms_before", "rms_after"}
    """
    h, w = frame.shape[:2]
    mask = white_mask(frame, int(cfg.get("white_s_max", 70)), int(cfg.get("white_v_min", 0)))
    pts = _largest_blob(mask)
    if pts is None or len(pts) < 100:
        return {"ok": False, "error": "没找到托盘"}
    f = 0.9 * max(w, h)
    K = np.array([[f, 0, w / 2.0], [0, f, h / 2.0], [0, 0, 1]], np.float64)

    def straightness(k1):
        D = np.array([k1, 0, 0, 0, 0], np.float64)
        p = cv2.undistortPoints(pts.reshape(-1, 1, 2), K, D, P=K).reshape(-1, 2)
        _, sides = quad_from_points(p)
        errs = []
        for sp in sides:
            if len(sp) < 20:
                continue
            vx, vy, x0, y0 = cv2.fitLine(sp.reshape(-1, 1, 2), cv2.DIST_L2, 0, 0.01, 0.01).reshape(-1)
            d = np.abs((sp[:, 0] - x0) * vy - (sp[:, 1] - y0) * vx)
            errs.append(d)
        if not errs:
            return 1e9
        e = np.concatenate(errs)
        return float(np.sqrt((e ** 2).mean()))

    lo, hi = -0.6, 0.6
    g = (math.sqrt(5) - 1) / 2
    a, b = hi - g * (hi - lo), lo + g * (hi - lo)
    fa, fb = straightness(a), straightness(b)
    for _ in range(40):
        if fa < fb:
            hi, b, fb = b, a, fa
            a = hi - g * (hi - lo)
            fa = straightness(a)
        else:
            lo, a, fa = a, b, fb
            b = lo + g * (hi - lo)
            fb = straightness(b)
    k1 = (lo + hi) / 2
    before, after = straightness(0.0), straightness(k1)
    return {"ok": True, "k1": round(k1, 5), "camera_matrix": K.tolist(), "dist_coeffs": [k1, 0, 0, 0, 0],
            "rms_before": round(before, 3), "rms_after": round(after, 3),
            "tip": "边缘直线度改善不到 0.3px 说明畸变很小, 可不写入" if before - after < 0.3 else ""}


# ============================================================================ 透视 / 相机位姿
def _focal_from_height(solve, f0: float, target_mm: float,
                       tol: float = 0.15) -> Tuple[float, bool, float]:
    """
    求使相机高度等于 target_mm 的焦距。

    高度对焦距**不是单调的**: 四个共面点 + 已知光心时平面位姿有两种解 (经典二义性),
    高度曲线是单峰的 (先升后降), 单调二分会在两端夹死并给出相差数倍的"解"。
    做法: 对数网格粗扫定位峰值, 在峰值左右两支各做黄金分割细化; 两支都落在容差内时
    取**倾角更小**的那支 (相机更接近正对, 也是旧二分实现的结果, 保持坐标不变)。
    返回 (f, 是否落在容差内, 该 f 下的高度)。
    """
    lo, hi = 0.15 * f0, 8.0 * f0
    n = 96
    fs = np.geomspace(lo, hi, n)
    hs = np.array([solve(float(f))["height_mm"] for f in fs], np.float64)
    ip = int(np.argmax(hs))
    g = (math.sqrt(5) - 1) / 2

    def cost(f):
        return abs(solve(float(f))["height_mm"] - target_mm)

    def refine(a: float, b: float) -> float:
        if b <= a:
            return (a + b) / 2.0
        c, d = b - g * (b - a), a + g * (b - a)
        fc, fd = cost(c), cost(d)
        for _ in range(48):
            if fc < fd:
                b, d, fd = d, c, fc
                c = b - g * (b - a)
                fc = cost(c)
            else:
                a, c, fc = c, d, fd
                d = a + g * (b - a)
                fd = cost(d)
        return (a + b) / 2.0

    spans = [(float(fs[0]), float(fs[ip])), (float(fs[ip]), float(fs[-1]))]
    cands = []
    for a, b in spans:
        f = refine(a, b)
        sol = solve(f)
        cands.append((float(f), float(sol["height_mm"]), float(sol["tilt_deg"])))
    good = [c for c in cands if abs(c[1] - target_mm) <= tol * target_mm]
    if good:
        f, h, _ = min(good, key=lambda c: c[2])
        return f, True, h
    f, h, _ = min(cands, key=lambda c: abs(c[1] - target_mm))
    return f, False, h


def estimate_pose(G: np.ndarray, principal: Tuple[float, float], frame_wh: Tuple[int, int],
                  focal: Optional[float] = None, z0: float = 0.0,
                  cam_height_mm: float = 0.0, min_tilt_deg: float = 12.0) -> Dict:
    """
    G: 托盘平面 (z=z0, 单位 mm) -> 去畸变像素 的单应 (3x3)。
    焦距来源: focal (已知) > cam_height_mm (按镜头高反推, 见 _focal_from_height;
    反算高度与填写值相差超过 15% 时返回 ok=False, 上层退回平面单应) > 由单应自估
    (矩形两边正交且等比例; 相机倾斜 < min_tilt_deg 时自估不可靠, 返回 ok=False)。
    返回 {"ok", "f", "tilt_deg", "height_mm"(相机到底面), "col"(每 mm 高度对单应第三列的增量),
          "z0", "G", "source", "msg"}
    """
    G = np.asarray(G, np.float64)
    cx, cy = float(principal[0]), float(principal[1])
    T = np.array([[1, 0, -cx], [0, 1, -cy], [0, 0, 1.0]])
    Ti = np.linalg.inv(T)
    Gp = T @ G
    f0 = float(max(frame_wh))

    def solve(f):
        Ki = np.diag([1.0 / f, 1.0 / f, 1.0])
        H = Ki @ Gp
        mu = math.sqrt(np.linalg.norm(H[:, 0]) * np.linalg.norm(H[:, 1]))
        r1, r2, t = H[:, 0] / mu, H[:, 1] / mu, H[:, 2] / mu
        if t[2] < 0:
            r1, r2, t, mu = -r1, -r2, -t, -mu
        U, _, Vt = np.linalg.svd(np.stack([r1, r2, np.cross(r1, r2)], 1))
        R = U @ Vt
        if np.linalg.det(R) < 0:
            R = U @ np.diag([1, 1, -1]) @ Vt
        C = -R.T @ t
        s = 1.0 if C[2] >= 0 else -1.0                  # 托盘坐标里朝向相机的 z 方向
        col = mu * (Ti @ np.diag([f, f, 1.0]) @ (R[:, 2] * s))
        tilt = math.degrees(math.acos(min(1.0, abs(float(R[2, 2])))))
        return {"f": float(f), "tilt_deg": round(tilt, 2), "height_mm": round(abs(float(C[2])) + z0, 1),
                "col": col, "z0": float(z0), "G": G, "ok": True}

    src = ""
    if focal and focal > 0:
        f, src = float(focal), "focal"
    else:
        a, b, c = Gp[0], Gp[1], Gp[2]
        sc = float(c[0] ** 2 + c[1] ** 2) + 1e-18
        A = np.array([(a[0] * a[1] + b[0] * b[1]) / f0 ** 2,
                      (a[0] ** 2 + b[0] ** 2 - a[1] ** 2 - b[1] ** 2) / f0 ** 2]) / sc
        B = np.array([c[0] * c[1], c[0] ** 2 - c[1] ** 2]) / sc
        u = -(A @ B) / max(float(A @ A), 1e-12)
        f_self = f0 / math.sqrt(u) if u > 0 else None
        if cam_height_mm and cam_height_mm > z0:
            f, ok_h, h_at_f = _focal_from_height(solve, f0, float(cam_height_mm))
            if not ok_h:
                # 反推不出与填写值自洽的焦距: 不能拿这个位姿去算坐标, 让上层退回平面单应
                return {"ok": False, "f": float(f), "height_mm": round(h_at_f, 1),
                        "tilt_deg": round(float(solve(f)["tilt_deg"]), 2),
                        "source": "cam_height",
                        "msg": (f"按镜头高 {cam_height_mm:.0f} mm 反推焦距失败: 这组四角/尺寸下可达到的镜头高"
                                f"最接近 {h_at_f:.0f} mm, 相差 {abs(h_at_f - cam_height_mm) / cam_height_mm:.0%}。"
                                f"检查镜头高是否量错、内框 W/H 是否填反、四角是否点在同一高度;"
                                f"广角镜头先做一次畸变校正 (畸变页「一键校正」)")}
            src = "cam_height"
        elif f_self and 0.3 * f0 < f_self < 4.0 * f0:
            f, src = f_self, "self"
        else:
            span = float(np.abs(G[2, :2]).max()) * 300.0 / max(abs(float(G[2, 2])), 1e-12)
            if span < 0.05:
                return {"ok": False, "msg": "相机基本正对托盘, 无法自估焦距 (填镜头高 cam_height_mm 即可)"}
            return {"ok": False, "msg": "托盘四角的透视与填写的内框 W/H 对不上, 无法自估焦距: "
                                        "检查 W/H 是否填反/填错、四角是否点准, 或直接填镜头高 cam_height_mm"}
    out = solve(f)
    out["source"] = src
    if src == "self" and out["tilt_deg"] < min_tilt_deg:
        return {"ok": False, "tilt_deg": out["tilt_deg"],
                "msg": f"相机倾角 {out['tilt_deg']}° 太小, 自估焦距不可靠 (填镜头高 cam_height_mm 即可)"}
    out["msg"] = (f"相机倾角 {out['tilt_deg']}°, 估计镜头高 {out['height_mm']} mm, "
                  f"焦距 {out['f']:.0f}px ({ {'focal': '已知', 'cam_height': '按镜头高反推', 'self': '托盘四角自估'}[src] })")
    return out


def height_homography(pose: Dict, h_mm: float) -> np.ndarray:
    """托盘坐标 (x, y, 高度 h_mm) -> 去畸变像素 的单应。"""
    G = np.array(pose["G"], np.float64).copy()
    G[:, 2] += (float(h_mm) - pose["z0"]) * pose["col"]
    return G


def order_corners(pts) -> np.ndarray:
    """任意顺序的 4 个点 -> TL TR BR BL (画面方向)。"""
    q = np.float32(pts).reshape(4, 2)
    return _order(q)


def snap_corners(frame: np.ndarray, quad: np.ndarray, cfg: Dict,
                 undistort: Optional[Callable[[np.ndarray], np.ndarray]] = None,
                 max_move: float = 0.06) -> Dict:
    """
    手点/粗给的内框四角 (原始像素, TL TR BR BL) -> 吸附到附近的白色区域边缘。
    只在手点四边形外扩一圈的范围内找白色; 任一角移动超过对角线的 max_move 就全部保留手点。
    白色区域的边界是「壁顶内沿」(远端看得到内壁, 近端被壁顶挡住), 所以吸附结果在壁顶高度。
    返回 {"ok", "corners"(原始像素), "moved_px", "msg"}
    """
    q = order_corners(quad)
    h, w = frame.shape[:2]
    diag = float(np.linalg.norm(q[2] - q[0]))
    c = q.mean(0)
    grow = q + (q - c) * 0.08
    region = np.zeros((h, w), np.uint8)
    cv2.fillPoly(region, [np.int32(grow)], 255)
    mask = white_mask(frame, int(cfg.get("white_s_max", 70)), int(cfg.get("white_v_min", 0)))
    mask = cv2.bitwise_and(mask, region)
    pts = _largest_blob(mask)
    if pts is None or len(pts) < 40:
        return {"ok": False, "corners": q.tolist(), "msg": "附近没有找到白色托盘区域, 保留手点位置"}
    if undistort is not None:
        und = undistort(pts)
        snapped_u, _ = quad_from_points(und)
        snapped = _distort_back(snapped_u, pts, und)
    else:
        snapped, _ = quad_from_points(pts)
    snapped = order_corners(snapped)
    moved = np.linalg.norm(snapped - q, axis=1)
    ok = bool((moved <= max_move * diag).all())
    out = snapped if ok else q                           # 四个角高度要一致: 要么全吸附, 要么全用手点
    msg = (f"已吸附, 四角移动 {np.round(moved, 1).tolist()} px" if ok else
           f"有角吸附后移动过大 ({np.round(moved, 1).tolist()} px), 全部保留手点位置")
    return {"ok": ok, "corners": np.round(out, 2).tolist(),
            "moved_px": np.round(moved, 2).tolist(), "msg": msg}


def _distort_back(q_und: np.ndarray, raw: np.ndarray, und: np.ndarray) -> np.ndarray:
    """去畸变坐标的角点 -> 原始像素: 用最近的轮廓点的局部偏移近似 (角点就在轮廓附近)。"""
    out = []
    for p in q_und:
        i = int(np.argmin(np.linalg.norm(und - p, axis=1)))
        out.append(p + (raw[i] - und[i]))
    return np.float32(out)
