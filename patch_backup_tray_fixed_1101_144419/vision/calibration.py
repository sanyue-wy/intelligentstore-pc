"""
STM32 智能分拣系统 - 视觉标定模块 (Pixel -> Robot Coordinate Calibration)

相对旧版的定位精度改进:
  1. 支持 >=4 个点, 用 findHomography(最小二乘 / RANSAC) 拟合。
     旧版只能 4 点 getPerspectiveTransform, 任何一个点点歪都会整体偏;
     建议在托盘内均匀取 9 点 (3x3) 标定, 误差可降到 1mm 左右。
  2. 可选镜头畸变校正: calib_config.json 里给出 camera_matrix / dist_coeffs
     (用 tools/calib_intrinsics.py 棋盘格标定得到), 先 undistortPoints 再做单应。
     广角 USB 摄像头边缘畸变常有 3~8 像素, 不校正时托盘边缘工件会抓偏。
  3. 视差(高度)校正: 单应矩阵只对"标定平面"(托盘底面) 成立。工件有高度,
     从斜上方看, 顶面会沿径向向外偏。给出相机光心像素 (cx,cy) 与相机离
     托盘面的高度 cam_height_mm 后, 按
         p_plane = c + (p_img - c) * (H - h) / H
     把识别点拉回托盘平面。40mm 高工件、相机高 300mm 时, 偏离中心 150px
     的工件可修正约 20px (≈ 数毫米)。cam_height_mm=0 表示关闭。

配置文件 vision/calib_config.json 示例:
{
  "src_pixels":   [[u,v], ... >=4 个],
  "dst_robot_mm": [[x,y], ... 与上面一一对应],
  "camera_matrix": [[fx,0,cx],[0,fy,cy],[0,0,1]],   // 可选
  "dist_coeffs":   [k1,k2,p1,p2,k3],                 // 可选
  "cam_height_mm": 300.0,                            // 可选, 0 关闭视差校正
  "principal_px":  [320, 240],                       // 可选, 缺省取 camera_matrix 或画面中心
  "tray": {...}                                      // 可选, 托盘坐标系模式, 见 vision/tray.py
}

托盘模式 (tray.enabled=true 且填了内框尺寸, 推荐):
    像素 -> 去畸变 -> 高度视差 -> 托盘四角单应 -> 托盘坐标 mm
         -> 刚体 (axes + 参考点) -> 残差多项式 -> 机械臂坐标
    托盘四角每次 scan 自动重新检测 (update_tray), 相机动了也不用重新标定。
    src_pixels/dst_robot_mm 此时只是托盘四角的展示值。
"""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
from typing import List, Optional, Tuple

import cv2
import numpy as np

from .interfaces import VisionCalibrator
from . import tray as traymod

logger = logging.getLogger(__name__)


def _locked(fn):
    """标定状态读写串行化: 托盘矩阵/刚体/残差要么全旧要么全新, 机械臂不会拿到半更新的坐标。"""
    @functools.wraps(fn)
    def wrap(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    return wrap


class CoordinateCalibrator(VisionCalibrator):

    def __init__(self, config_path: Optional[str] = None):
        self._lock = threading.RLock()
        self.config_path = config_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "calib_config.json")
        self.transform_matrix: Optional[np.ndarray] = None
        self.inverse_matrix: Optional[np.ndarray] = None
        self.reproj_error_mm: float = 0.0

        self.src_pixels = np.float32([[160, 120], [480, 120], [480, 360], [160, 360]])
        self.dst_robot_mm = np.float32([[20, 30], [60, 30], [60, -30], [20, -30]])

        self.camera_matrix: Optional[np.ndarray] = None
        self.dist_coeffs: Optional[np.ndarray] = None
        self.cam_height_mm: float = float(os.environ.get("IS_CAM_HEIGHT_MM", 0) or 0)
        self.principal_px: Optional[Tuple[float, float]] = None
        self.image_size: Optional[Tuple[int, int]] = None   # 标定时的画面 (宽, 高)
        self.is_calibrated: bool = False                    # 是否来自真实标定文件

        # ---- 托盘坐标系模式
        self.tray_cfg: dict = traymod.merge_cfg(None)
        self.tray_tracker: Optional[traymod.TrayTracker] = None
        self.tray_H: Optional[np.ndarray] = None            # 去畸变+视差后像素 -> 托盘 mm
        self.tray_rigid: Optional[dict] = None
        self.tray_residual: Optional[dict] = None
        self.tray_status: dict = {"enabled": False}
        self._tray_frame_n = 0

        self.compute_matrix()
        if not self.load_config():
            logger.warning("未找到标定文件 %s, 使用默认占位标定 (坐标不可信!)", self.config_path)

    # ------------------------------------------------------------------ 拟合
    def check_frame_size(self, w: int, h: int) -> Optional[str]:
        """画面尺寸与标定时不一致 -> 返回告警文本 (换分辨率/换相机后必须重新标定)。"""
        if self.image_size and tuple(self.image_size) != (int(w), int(h)):
            return (f"当前画面 {w}x{h} 与标定时 {self.image_size[0]}x{self.image_size[1]} 不一致, "
                    f"坐标不可信, 请重新标定")
        return None

    @_locked
    def compute_matrix(self):
        src = self._undistort(self.src_pixels.reshape(-1, 2))
        dst = self.dst_robot_mm.reshape(-1, 2).astype(np.float32)
        if len(src) == 4:
            H = cv2.getPerspectiveTransform(src.astype(np.float32), dst)
        else:
            H, _ = cv2.findHomography(src, dst, cv2.RANSAC if len(src) >= 8 else 0, 2.0)
            if H is None:
                raise ValueError("标定点退化 (共线?), 无法求解单应矩阵")
        self.transform_matrix = H
        self.inverse_matrix = np.linalg.inv(H)
        proj = cv2.perspectiveTransform(src.reshape(-1, 1, 2).astype(np.float32), H).reshape(-1, 2)
        self.reproj_error_mm = float(np.sqrt(((proj - dst) ** 2).sum(1)).mean())
        if len(src) > 4:
            logger.info("标定完成: %d 点, 平均重投影误差 %.2f mm", len(src), self.reproj_error_mm)

    @_locked
    def set_calibration_points(self, src_pts: List[Tuple[float, float]],
                               dst_pts: List[Tuple[float, float]]):
        if len(src_pts) < 4 or len(src_pts) != len(dst_pts):
            raise ValueError("标定至少需要 4 组一一对应的点")
        self.src_pixels = np.float32(src_pts)
        self.dst_robot_mm = np.float32(dst_pts)
        self.compute_matrix()
        self.is_calibrated = True

    # ------------------------------------------------------------------ 校正
    def _undistort(self, pts: np.ndarray) -> np.ndarray:
        pts = np.asarray(pts, dtype=np.float32).reshape(-1, 1, 2)
        if self.camera_matrix is None or self.dist_coeffs is None:
            return pts.reshape(-1, 2)
        return cv2.undistortPoints(pts, self.camera_matrix, self.dist_coeffs,
                                   P=self.camera_matrix).reshape(-1, 2)

    def _principal(self) -> Optional[Tuple[float, float]]:
        if self.principal_px is not None:
            return self.principal_px
        if self.camera_matrix is not None:
            return float(self.camera_matrix[0, 2]), float(self.camera_matrix[1, 2])
        return None

    def correct_parallax(self, u: float, v: float, height_mm: float) -> Tuple[float, float]:
        """把高度为 height_mm 处的像点投回托盘平面 (需 cam_height_mm 与光心)。"""
        H = self.cam_height_mm
        c = self._principal()
        if H <= 0 or height_mm <= 0 or c is None or height_mm >= H:
            return u, v
        k = (H - height_mm) / H
        return c[0] + (u - c[0]) * k, c[1] + (v - c[1]) * k

    # ------------------------------------------------------------------ 映射
    @_locked
    def pixel_to_robot(self, u: float, v: float, height_mm: float = 0.0) -> Tuple[float, float]:
        if self.transform_matrix is None:
            self.compute_matrix()
        p = self._undistort(np.array([[u, v]], dtype=np.float32))[0]
        pu, pv = self.correct_parallax(float(p[0]), float(p[1]), height_mm)
        dst = cv2.perspectiveTransform(np.array([[[pu, pv]]], dtype=np.float32), self.transform_matrix)
        x, y = float(dst[0, 0, 0]), float(dst[0, 0, 1])
        if self.tray_mode:
            x, y = traymod.apply_residual(self.tray_residual, x, y)
        return round(x, 2), round(y, 2)

    @_locked
    def robot_to_pixel(self, x_mm: float, y_mm: float) -> Tuple[float, float]:
        if self.inverse_matrix is None:
            self.compute_matrix()
        dst = cv2.perspectiveTransform(np.array([[[x_mm, y_mm]]], dtype=np.float32), self.inverse_matrix)
        u, v = float(dst[0, 0, 0]), float(dst[0, 0, 1])
        if self.camera_matrix is not None and self.dist_coeffs is not None:
            # 无畸变像素 -> 实际像素
            K = self.camera_matrix
            n = np.array([[(u - K[0, 2]) / K[0, 0], (v - K[1, 2]) / K[1, 1], 1.0]], dtype=np.float64)
            img, _ = cv2.projectPoints(n, np.zeros(3), np.zeros(3), K, self.dist_coeffs)
            u, v = float(img[0, 0, 0]), float(img[0, 0, 1])
        return round(u, 1), round(v, 1)

    # ------------------------------------------------------------------ 持久化
    @_locked
    def save_config(self):
        data = {}
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        if self.tray_mode:
            t = dict(data.get("tray") or {})
            t.update({k: self.tray_cfg[k] for k in ("enabled", "inner_w_mm", "inner_h_mm", "rim_mm",
                                                    "rim_height_mm", "axes", "ref_points")})
            if self.tray_tracker is not None and self.tray_tracker.stable is not None:
                t["corners_px"] = np.round(self.tray_tracker.stable, 2).tolist()
            data["tray"] = t
        data.update({"src_pixels": self.src_pixels.tolist(),
                     "dst_robot_mm": self.dst_robot_mm.tolist(),
                     "reproj_error_mm": round(self.reproj_error_mm, 3)})
        if self.image_size:
            data["image_size"] = list(self.image_size)
        if self.cam_height_mm:
            data["cam_height_mm"] = self.cam_height_mm
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error("保存标定失败: %s", e)

    @_locked
    def load_config(self) -> bool:
        if not os.path.exists(self.config_path):
            return False
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("camera_matrix") is not None:
                self.camera_matrix = np.array(data["camera_matrix"], dtype=np.float64)
                self.dist_coeffs = np.array(data.get("dist_coeffs", [0, 0, 0, 0, 0]), dtype=np.float64)
            if "cam_height_mm" in data and not os.environ.get("IS_CAM_HEIGHT_MM"):
                self.cam_height_mm = float(data["cam_height_mm"] or 0)
            if data.get("principal_px"):
                self.principal_px = tuple(float(x) for x in data["principal_px"])
            if data.get("image_size"):
                self.image_size = tuple(int(x) for x in data["image_size"])
            self.tray_cfg = traymod.merge_cfg(data.get("tray"))
            if traymod.tray_ready(self.tray_cfg):
                self.tray_tracker = traymod.TrayTracker(self.tray_cfg)
                self.tray_status = {"enabled": True, "ok": False, "msg": "等待第一帧画面检测托盘"}
                if self.tray_tracker.stable is not None:
                    self._compute_tray_matrix()
                return True
            self.src_pixels = np.float32(data["src_pixels"])
            self.dst_robot_mm = np.float32(data["dst_robot_mm"])
            self.compute_matrix()
            self.is_calibrated = True
            return True
        except Exception as e:
            logger.error("标定文件解析失败: %s", e)
            return False

    # ================================================================== 托盘模式
    @property
    def tray_mode(self) -> bool:
        return self.tray_tracker is not None

    def _plane_px(self, pts: np.ndarray, height_mm: float) -> np.ndarray:
        """去畸变像素 -> 视差校正到托盘底面 (逐点)。"""
        pts = np.asarray(pts, np.float32).reshape(-1, 2)
        if height_mm <= 0:
            return pts
        return np.float32([self.correct_parallax(float(u), float(v), height_mm) for u, v in pts])

    @_locked
    def update_tray(self, frame: Optional[np.ndarray], force: bool = False) -> dict:
        """
        在 frame 里重新检测托盘 (scan 时每次调用; 连续预览时每 redetect_every 帧一次)。
        托盘位置变化才重算映射。返回 tray_status。
        """
        if not self.tray_mode or frame is None:
            return self.tray_status
        self._tray_frame_n += 1
        every = max(1, int(self.tray_cfg.get("redetect_every", 15)))
        if not force and self.tray_tracker.locked_live and self._tray_frame_n % every:
            return self.tray_status
        h, w = frame.shape[:2]
        if self.image_size is None:
            self.image_size = (w, h)
        if self.cam_height_mm > 0 and self.principal_px is None and self.camera_matrix is None:
            self.principal_px = (w / 2.0, h / 2.0)
        undist = (lambda p: self._undistort(p)) if self.camera_matrix is not None else None
        res = traymod.detect_tray(frame, self.tray_cfg, undist)
        changed = self.tray_tracker.update(res)
        if changed:
            self._compute_tray_matrix()
        tr = self.tray_tracker
        st = self.tray_status
        st.update({"enabled": True, "ok": tr.stable is not None and tr.fails < 5,
                   "live": tr.locked_live, "detect_ok": res.ok, "msg": res.msg,
                   "aspect_err": res.aspect_err, "area_ratio": res.area_ratio, "fails": tr.fails})
        if tr.event:
            st["event"] = tr.event
        return st

    def _compute_tray_matrix(self):
        cfg = self.tray_cfg
        corners = self._plane_px(self.tray_tracker.stable, float(cfg.get("rim_height_mm") or 0))
        tray_pts = traymod.outer_tray_pts(cfg)
        self.tray_H = cv2.getPerspectiveTransform(corners.astype(np.float32), tray_pts)
        refs = [r for r in cfg.get("ref_points") or [] if r.get("tray_mm") and r.get("robot_mm")]
        P = np.float64([r["tray_mm"] for r in refs]).reshape(-1, 2)
        Q = np.float64([r["robot_mm"] for r in refs]).reshape(-1, 2)
        try:
            rig = traymod.solve_rigid(P, Q, cfg.get("axes"))
        except ValueError as e:
            logger.error("托盘标定: %s", e)
            rig = traymod.solve_rigid(P[:0], Q[:0], None)
        self.tray_rigid = rig
        A = np.eye(3)
        A[:2, :2], A[:2, 2] = rig["R"], rig["t"]
        pred = (P @ rig["R"].T + rig["t"]) if len(P) else P
        self.tray_residual = traymod.fit_residual(pred, Q, cfg.get("residual", "auto")) if len(P) else None
        self.transform_matrix = A @ self.tray_H
        self.inverse_matrix = np.linalg.inv(self.transform_matrix)
        self.src_pixels = corners.astype(np.float32)
        self.dst_robot_mm = (tray_pts.astype(np.float64) @ rig["R"].T + rig["t"]).astype(np.float32)
        after = []
        for p, q in zip(pred, Q):
            x, y = traymod.apply_residual(self.tray_residual, float(p[0]), float(p[1]))
            after.append(float(np.hypot(x - q[0], y - q[1])))
        self.reproj_error_mm = float(np.mean(after)) if after else 0.0
        self.is_calibrated = len(P) >= 1
        self.tray_status.update({
            "ref_points": len(P), "rigid_rms_mm": round(rig["rms"], 2), "angle_deg": rig.get("angle_deg"),
            "mirror": rig.get("mirror"), "scale": round(rig.get("scale", 1.0), 4),
            "residual": (self.tray_residual or {}).get("order", 0),
            "per_point_mm": [round(e, 2) for e in after]})
        if len(P) >= 2 and abs(rig.get("scale", 1.0) - 1) > 0.03:
            self.tray_status["warn"] = (f"参考点间距与托盘尺寸不符 (比例 {rig['scale']:.3f}), "
                                        f"检查 inner_w/h_mm、rim_mm 或参考点坐标")

    @_locked
    def pixel_to_tray(self, u: float, v: float, height_mm: float = 0.0) -> Tuple[float, float]:
        """像素 -> 托盘坐标 mm (托盘模式)。"""
        if self.tray_H is None:
            raise RuntimeError("托盘尚未定位")
        p = self._undistort(np.array([[u, v]], dtype=np.float32))[0]
        pu, pv = self.correct_parallax(float(p[0]), float(p[1]), height_mm)
        d = cv2.perspectiveTransform(np.array([[[pu, pv]]], np.float32), self.tray_H)
        return float(d[0, 0, 0]), float(d[0, 0, 1])

    @_locked
    def tray_to_pixel(self, pts_mm) -> np.ndarray:
        """托盘坐标 (底面) -> 原始图像像素 (含畸变), 画叠加层/生成 ROI 用。"""
        if self.tray_H is None:
            return np.empty((0, 2), np.float32)
        p = cv2.perspectiveTransform(np.float32(pts_mm).reshape(-1, 1, 2), np.linalg.inv(self.tray_H)).reshape(-1, 2)
        if self.camera_matrix is not None and self.dist_coeffs is not None:
            K = self.camera_matrix
            n = np.stack([(p[:, 0] - K[0, 2]) / K[0, 0], (p[:, 1] - K[1, 2]) / K[1, 1], np.ones(len(p))], 1)
            p, _ = cv2.projectPoints(n.astype(np.float64), np.zeros(3), np.zeros(3), K, self.dist_coeffs)
            p = p.reshape(-1, 2)
        return p.astype(np.float32)

    @_locked
    def tray_roi_px(self) -> List[List[float]]:
        """托盘内框外扩 roi_margin_mm 的像素多边形 (沿边加密, 畸变后仍贴合)。"""
        if self.tray_H is None:
            return []
        W, H = float(self.tray_cfg["inner_w_mm"]), float(self.tray_cfg["inner_h_mm"])
        m = float(self.tray_cfg.get("roi_margin_mm", 6))
        cs = [(-m, -m), (W + m, -m), (W + m, H + m), (-m, H + m)]
        pts = []
        for i in range(4):
            a, b = np.array(cs[i]), np.array(cs[(i + 1) % 4])
            for t in np.linspace(0, 1, 6, endpoint=False):
                pts.append(a + (b - a) * t)
        return self.tray_to_pixel(pts).round(1).tolist()

    @_locked
    def set_tray_ref_points(self, pixel_pts: List[Tuple[float, float]], robot_pts: List[Tuple[float, float]],
                            keep_manual: bool = True) -> dict:
        """网页标定: 点击像素 + 机械臂真实坐标 -> 换算成托盘坐标存为参考点 (相机再动也有效)。"""
        refs = [r for r in self.tray_cfg.get("ref_points") or [] if keep_manual and r.get("src") != "click"]
        for (u, v), (x, y) in zip(pixel_pts, robot_pts):
            xt, yt = self.pixel_to_tray(u, v)
            refs.append({"tray_mm": [round(xt, 2), round(yt, 2)], "robot_mm": [float(x), float(y)], "src": "click"})
        self.tray_cfg["ref_points"] = refs
        self._compute_tray_matrix()
        return self.tray_status

    @_locked
    def configure_tray(self, updates: dict) -> dict:
        """网页调试台修改托盘参数 (尺寸/壁厚/axes/参考点/开关), 立即生效; 调用方再 save_config()。"""
        old_corners = self.tray_tracker.stable if self.tray_tracker is not None else None
        self.tray_cfg = traymod.merge_cfg({**self.tray_cfg, **(updates or {})})
        if not traymod.tray_ready(self.tray_cfg):
            self.tray_tracker = None
            self.tray_H = None
            self.tray_status = {"enabled": False, "msg": "托盘模式关闭 (或内框尺寸未填)"}
            try:
                self.compute_matrix()
            except Exception:
                pass
            return self.tray_status
        if self.tray_tracker is None:
            self.tray_tracker = traymod.TrayTracker(self.tray_cfg)
            if old_corners is not None:
                self.tray_tracker.stable = old_corners
            self.tray_status = {"enabled": True, "ok": False, "msg": "等待画面检测托盘"}
        else:
            self.tray_tracker.cfg = self.tray_cfg
        if self.tray_tracker.stable is not None:
            self._compute_tray_matrix()
        return self.tray_status

    @_locked
    def tray_overlay_px(self) -> dict:
        """画面叠加: 白色外边界、内框、原点和 +x/+y 方向 (原始像素)。"""
        if self.tray_H is None:
            return {}
        W, H = float(self.tray_cfg["inner_w_mm"]), float(self.tray_cfg["inner_h_mm"])
        L = min(W, H) * 0.25
        return {"outer": self.tray_to_pixel(traymod.outer_tray_pts(self.tray_cfg)).tolist(),
                "inner": self.tray_to_pixel([[0, 0], [W, 0], [W, H], [0, H]]).tolist(),
                "axes": self.tray_to_pixel([[0, 0], [L, 0], [0, L]]).tolist(),
                "refs": self.tray_to_pixel([r["tray_mm"] for r in self.tray_cfg.get("ref_points") or []]
                                           or np.empty((0, 2))).tolist()}

    @_locked
    def set_intrinsics(self, camera_matrix, dist_coeffs, persist: bool = False):
        """更新内参/畸变 (棋盘格或托盘直边估计), 托盘需在新的去畸变坐标下重新锁定。"""
        self.camera_matrix = None if camera_matrix is None else np.array(camera_matrix, np.float64)
        self.dist_coeffs = None if dist_coeffs is None else np.array(dist_coeffs, np.float64).reshape(-1)
        if self.tray_tracker is not None:
            self.tray_tracker.hist.clear()
            self.tray_tracker.pending.clear()
            self.tray_tracker.stable = None
            self.tray_tracker.locked_live = False
            self.tray_H = None
            self.is_calibrated = False
            self.tray_status.update(ok=False, msg="内参已更新, 等待重新检测托盘")
        else:
            self.compute_matrix()
        if persist:
            data = {}
            if os.path.exists(self.config_path):
                try:
                    with open(self.config_path, encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            if self.camera_matrix is None:
                data.pop("camera_matrix", None)
                data.pop("dist_coeffs", None)
            else:
                data["camera_matrix"] = self.camera_matrix.tolist()
                data["dist_coeffs"] = self.dist_coeffs.tolist()
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
