"""
视觉模块单元测试
"""

import unittest
import time
import numpy as np
from MessegeCenter import MessageBus
from vision.calibration import CoordinateCalibrator
from vision.cameras import SyntheticCamera
from vision.detector import WorkpieceDetector
from vision.service import VisionService


class TestVision(unittest.TestCase):

    def test_coordinate_calibrator(self):
        calib = CoordinateCalibrator()
        # 测试图像中心点映射
        x_mm, y_mm = calib.pixel_to_robot(320.0, 240.0)
        self.assertIsInstance(x_mm, float)
        self.assertIsInstance(y_mm, float)

        # 逆变换回像素坐标
        u, v = calib.robot_to_pixel(x_mm, y_mm)
        self.assertAlmostEqual(u, 320.0, delta=2.0)
        self.assertAlmostEqual(v, 240.0, delta=2.0)

    def test_synthetic_camera(self):
        cam = SyntheticCamera(width=640, height=480)
        self.assertTrue(cam.open())
        ret, frame = cam.read()
        self.assertTrue(ret)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.shape, (480, 640, 3))
        cam.close()

    def test_workpiece_detector(self):
        cam = SyntheticCamera()
        cam.open()
        ret, frame = cam.read()
        self.assertTrue(ret)

        detector = WorkpieceDetector()
        targets = detector.detect(frame)
        # 仿真相机会绘制4个工件 (红绿蓝黄)
        self.assertGreaterEqual(len(targets), 1)

        target = targets[0]
        self.assertIn(target.color, ["red", "green", "blue", "yellow"])
        self.assertGreater(target.confidence, 0.5)

        annotated = detector.annotate(frame, targets)
        self.assertEqual(annotated.shape, frame.shape)
        cam.close()

    def test_vision_service(self):
        bus = MessageBus()
        service = VisionService(bus)
        service.set_camera_source(use_synthetic=True)
        service.start()

        time.sleep(0.15)
        frame, targets = service.get_latest_frame_and_targets()
        self.assertIsNotNone(frame)
        self.assertGreater(len(targets), 0)

        service.stop()


class _FlakyCam:
    """前 5 帧正常, 然后掉线 (read 失败), 重开后恢复 —— 模拟 USB 松动。"""
    opens = 0

    def __init__(self):
        self.n = 0
        self.ok = False

    def open(self):
        _FlakyCam.opens += 1
        self.ok = True
        return True

    def close(self):
        self.ok = False

    def is_opened(self):
        return self.ok

    def read(self):
        import numpy as np
        time.sleep(0.01)
        self.n += 1
        if _FlakyCam.opens == 1 and self.n > 5:
            return False, None
        return True, np.full((48, 64, 3), self.n % 255, np.uint8)

    def describe(self):
        return "flaky"


class TestVisionRobustness(unittest.TestCase):

    def test_grabber_reconnect(self):
        from vision.cameras import FrameGrabber
        _FlakyCam.opens = 0
        g = FrameGrabber(_FlakyCam, stale_s=0.3, max_fail=5)
        self.assertTrue(g.start(2.0))
        time.sleep(0.6)
        frames = g.fresh_frames(3, 2.0)
        g.stop()
        self.assertGreaterEqual(_FlakyCam.opens, 2)
        self.assertEqual(len(frames), 3)

    def test_fresh_frames_are_new(self):
        bus = MessageBus()
        s = VisionService(bus, continuous_detect=False)
        s.set_camera_source(use_synthetic=True)
        s.start()
        a = s.get_fresh_frames(2, 2.0)
        s.stop()
        self.assertEqual(len(a), 2)

    def test_scan_never_raises_without_camera(self):
        s = VisionService(MessageBus(), continuous_detect=False)
        self.assertEqual(s.scan(), ([], None))

    def test_scan_synthetic_yolo_fallback(self):
        import os
        from vision import create_vision_service
        os.environ["IS_YOLO_MODEL"] = "/nonexistent.onnx"
        try:
            s = create_vision_service(MessageBus(), backend="yolo", continuous_detect=False)
            s.set_camera_source(use_synthetic=True)
            s.start()
            ts, ref = s.scan(3)
            s.stop()
        finally:
            del os.environ["IS_YOLO_MODEL"]
        self.assertIsNotNone(ref)
        self.assertGreaterEqual(len(ts), 1)
        self.assertTrue(ts[0].category)          # HSV 兜底也要补 category/label
        self.assertTrue(ts[0].label)

    def test_replay_camera(self):
        import tempfile, os, cv2, numpy as np
        from vision.cameras import create_camera
        d = tempfile.mkdtemp()
        for i in range(3):
            cv2.imwrite(os.path.join(d, f"{i}.png"), np.full((40, 50, 3), i * 50, np.uint8))
        cam = create_camera({"type": "replay", "path": d, "fps": 100})
        self.assertTrue(cam.open())
        vals = [int(cam.read()[1][0, 0, 0]) for _ in range(4)]
        self.assertEqual(vals, [0, 50, 100, 0])

    def test_config_env_override(self):
        import os
        from vision.config import load_vision_config
        os.environ["IS_CAM_TYPE"] = "synthetic"
        os.environ["IS_YOLO_CONF"] = "0.55"
        try:
            c = load_vision_config()
        finally:
            del os.environ["IS_CAM_TYPE"], os.environ["IS_YOLO_CONF"]
        self.assertEqual(c.camera["type"], "synthetic")
        self.assertAlmostEqual(c.model["conf"], 0.55)

    def test_calibration_multi_point(self):
        import tempfile, os
        p = os.path.join(tempfile.mkdtemp(), "c.json")
        cal = CoordinateCalibrator(config_path=p)
        src = [(100, 100), (320, 100), (540, 100), (100, 240), (320, 240), (540, 240),
               (100, 380), (320, 380), (540, 380)]
        dst = [(u * 0.25, -v * 0.25) for u, v in src]
        cal.set_calibration_points(src, dst)
        cal.image_size = (640, 480)
        self.assertLess(cal.reproj_error_mm, 0.01)
        x, y = cal.pixel_to_robot(200, 300)
        self.assertAlmostEqual(x, 50.0, delta=0.05)
        self.assertAlmostEqual(y, -75.0, delta=0.05)
        cal.save_config()
        cal2 = CoordinateCalibrator(config_path=p)
        self.assertTrue(cal2.is_calibrated)
        self.assertIsNotNone(cal2.check_frame_size(1280, 720))


def _tray_image(k1=0.0):
    import cv2
    img = np.full((480, 640, 3), 30, np.uint8)
    s, ang, c = 1.5, np.deg2rad(3), np.array([320, 240])
    R = np.array([[np.cos(ang), -np.sin(ang)], [np.sin(ang), np.cos(ang)]])
    tp = lambda x, y: R @ (np.array([x - 140, y - 90]) * s) + c
    cv2.fillPoly(img, [np.int32([tp(-10, -10), tp(290, -10), tp(290, 190), tp(-10, 190)])], (235, 235, 235))
    for x, y, col in [(20, 20, (0, 0, 200)), (270, 100, (200, 0, 0)), (140, 185, (0, 200, 0))]:
        cv2.circle(img, tuple(int(v) for v in tp(x, y)), 18, col, -1)
    if k1:
        K = np.array([[576, 0, 320], [0, 576, 240], [0, 0, 1.]])
        ys, xs = np.mgrid[0:480, 0:640].astype(np.float32)
        und = cv2.undistortPoints(np.stack([xs.ravel(), ys.ravel()], 1).reshape(-1, 1, 2), K,
                                  np.array([k1, 0, 0, 0, 0.]), P=K).reshape(480, 640, 2)
        img = cv2.remap(img, und[..., 0], und[..., 1], cv2.INTER_LINEAR)
    return img, tp


class TestTray(unittest.TestCase):
    def _cal(self, refs, axes=("tray_x", "tray_y")):
        import tempfile, os, json
        p = os.path.join(tempfile.mkdtemp(), "c.json")
        json.dump({"tray": {"enabled": True, "inner_w_mm": 280, "inner_h_mm": 180, "rim_mm": 10,
                            "axes": list(axes), "ref_points": refs}}, open(p, "w"))
        return CoordinateCalibrator(config_path=p), p

    def test_tray_chain(self):
        img, tp = _tray_image()
        rob = lambda x, y: [y + 100, -x + 50]
        cal, p = self._cal([{"tray_mm": [0, 0], "robot_mm": rob(0, 0)}, {"tray_mm": [280, 180], "robot_mm": rob(280, 180)}])
        for _ in range(3):
            st = cal.update_tray(img, force=True)
        self.assertTrue(st["ok"], st)
        for xt, yt in [(50, 50), (200, 150)]:
            x, y = cal.pixel_to_robot(*tp(xt, yt))
            self.assertLess(np.hypot(x - rob(xt, yt)[0], y - rob(xt, yt)[1]), 1.0)
        cal.save_config()
        cal2 = CoordinateCalibrator(config_path=p)          # 启动时先用保存的角点
        x, y = cal2.pixel_to_robot(*tp(200, 150))
        self.assertLess(np.hypot(x - 250, y + 150), 1.0)
        # 1 个参考点 + axes
        cal3, _ = self._cal([{"tray_mm": [0, 0], "robot_mm": rob(0, 0)}], axes=("tray_y", "-tray_x"))
        cal3.update_tray(img, force=True)
        x, y = cal3.pixel_to_robot(*tp(200, 150))
        self.assertLess(np.hypot(x - 250, y + 150), 1.0)

    def test_oneshot_undistort(self):
        """一键单张畸变校正: 合成 k1=-0.2 的棋盘格, 校正后格线误差 < 0.2px。"""
        import cv2
        from tools.calib_intrinsics import detect_board, calibrate_views
        w, h, sq = 640, 480, 40
        B = np.full((9 * sq + 80, 13 * sq + 80), 255, np.uint8)
        for r in range(9):
            for c in range(13):
                if (r + c) % 2 == 0:
                    B[40 + r * sq:40 + (r + 1) * sq, 40 + c * sq:40 + (c + 1) * sq] = 0
        src = np.float32([[0, 0], [B.shape[1], 0], [B.shape[1], B.shape[0]], [0, B.shape[0]]])
        Hm = cv2.getPerspectiveTransform(src, np.float32([[40, 40], [600, 35], [610, 440], [30, 445]]))
        und = cv2.warpPerspective(B, Hm, (w, h), borderValue=128)
        K = np.float64([[500, 0, w / 2], [0, 500, h / 2], [0, 0, 1]])
        D = np.float64([-0.2, 0, 0, 0, 0])
        xs, ys = np.meshgrid(np.arange(w), np.arange(h))
        m = cv2.undistortPoints(np.stack([xs, ys], -1).reshape(-1, 1, 2).astype(np.float32), K, D, P=K).reshape(h, w, 2)
        img = cv2.remap(und, m[..., 0], m[..., 1], cv2.INTER_LINEAR, borderValue=128)
        c, cr = detect_board(img)
        self.assertEqual(cr, (12, 8))
        r = calibrate_views([(c, cr)], (w, h))
        self.assertTrue(r["ok"], r)
        self.assertGreater(r["before_px"], 1.0)
        self.assertLess(r["after_px"], 0.2)

    def test_fixed_tray_px(self):
        """固定托盘: 画面左上角为原点的像素框 -> 托盘 mm, origin_mm 平移, 分辨率变化自动缩放。"""
        import json, tempfile, os
        from vision.calibration import CoordinateCalibrator
        d = tempfile.mkdtemp()
        p = os.path.join(d, "c.json")
        with open(p, "w") as f:
            json.dump({"tray": {"enabled": True, "inner_w_mm": 240, "inner_h_mm": 180, "origin_mm": [10, 20],
                                "fixed_px": {"x": 100, "y": 60, "w": 480, "h": 360, "image_size": [640, 480]}}}, f)
        c = CoordinateCalibrator(p)
        self.assertTrue(c.tray_fixed and c.is_calibrated)
        xt, yt = c.pixel_to_tray(340, 240)
        self.assertLess(np.hypot(xt - 120, yt - 90), 1e-3)
        x, y = c.pixel_to_robot(580, 420)
        self.assertLess(np.hypot(x - 250, y - 200), 1e-3)
        c.update_tray(np.zeros((960, 1280, 3), np.uint8))
        xt, yt = c.pixel_to_tray(1160, 840)
        self.assertLess(np.hypot(xt - 240, yt - 180), 1e-3)
        self.assertEqual(len(c.tray_overlay_px()["refs"]), 0)
        c.configure_tray({"fixed_px": {}})
        self.assertFalse(c.tray_fixed)
        self.assertIsNone(c.tray_H)

    def test_tray_tracker_ignores_glitch(self):
        img, _ = _tray_image()
        cal, _ = self._cal([{"tray_mm": [0, 0], "robot_mm": [0, 0]}])
        cal.update_tray(img, force=True)
        H0 = cal.tray_H.copy()
        st = cal.update_tray(np.zeros_like(img), force=True)     # 一帧黑图 (手挡住)
        self.assertTrue(np.allclose(H0, cal.tray_H))
        self.assertFalse(st["detect_ok"])

    def test_estimate_k1(self):
        from vision.tray import estimate_k1, merge_cfg
        img, _ = _tray_image(k1=-0.25)
        r = estimate_k1(img, merge_cfg({"inner_w_mm": 280, "inner_h_mm": 180, "rim_mm": 10}))
        self.assertAlmostEqual(r["k1"], -0.25, delta=0.03)


import cv2  # noqa: E402


def _tilted_cam(pitch_deg=25.0, yaw_deg=6.0, cam_h=380.0, W=240.0, H=180.0, f=520.0, size=(640, 480)):
    """斜看托盘的针孔相机。托盘坐标 x 右 y 下, 高度 h 朝相机。返回 proj(x, y, h) -> 像素。"""
    K = np.float64([[f, 0, size[0] / 2], [0, f, size[1] / 2], [0, 0, 1]])
    Rp = cv2.Rodrigues(np.float64([np.radians(-pitch_deg), 0, 0]))[0] @ \
        cv2.Rodrigues(np.float64([0, 0, np.radians(yaw_deg)]))[0]
    C = np.float64([W / 2, H / 2 + cam_h * np.tan(np.radians(pitch_deg)), -cam_h])
    t = -Rp @ C

    def proj(pts):
        P = np.float64(pts).reshape(-1, 3).copy()
        P[:, 2] = -P[:, 2]
        q = K @ (Rp @ P.T + t[:, None])
        return (q[:2] / q[2]).T
    return proj


class TestPerspective(unittest.TestCase):
    """相机斜看托盘: 四角单应 + 位姿, 高度视差按三维几何处理。"""

    def _cal(self, corners, extra=None, cam_h=0.0):
        import json, tempfile, os
        from vision.calibration import CoordinateCalibrator
        p = os.path.join(tempfile.mkdtemp(), "c.json")
        tray = {"enabled": True, "inner_w_mm": 240, "inner_h_mm": 180, "origin_mm": [0, 0],
                "fixed_px": {"corners": np.round(corners, 3).tolist(), "image_size": [640, 480]}}
        tray.update(extra or {})
        with open(p, "w") as f:
            json.dump({"tray": tray, "image_size": [640, 480], "cam_height_mm": cam_h}, f)
        return CoordinateCalibrator(p)

    def _err(self, cal, proj, h):
        rng = np.random.default_rng(0)
        pts = rng.uniform([20, 20], [220, 160], (25, 2))
        img = proj(np.c_[pts, np.full(25, h)])
        got = np.float64([cal.pixel_to_tray(u, v, h) for u, v in img])
        return float(np.linalg.norm(got - pts, axis=1).max())

    def test_pose_self_focal(self):
        proj = _tilted_cam()
        cal = self._cal(proj([[0, 0, 0], [240, 0, 0], [240, 180, 0], [0, 180, 0]]))
        st = cal.tray_status["perspective"]
        self.assertEqual(st["method"], "pose", st)
        self.assertAlmostEqual(st["f_px"], 520, delta=5)
        self.assertAlmostEqual(st["tilt_deg"], 25, delta=2)
        self.assertLess(self._err(cal, proj, 0), 0.05)
        self.assertLess(self._err(cal, proj, 18), 0.1)          # 旧公式在此约偏 8mm

    def test_pose_cam_height_and_robot(self):
        proj = _tilted_cam(pitch_deg=8, yaw_deg=0)               # 倾角小: 自估不可靠, 用镜头高反推焦距
        q = proj([[0, 0, 0], [240, 0, 0], [240, 180, 0], [0, 180, 0]])
        self.assertNotEqual(self._cal(q).tray_status["perspective"]["method"], "pose")
        cal = self._cal(q, cam_h=380)
        self.assertEqual(cal.tray_status["perspective"]["source"], "cam_height")
        self.assertLess(self._err(cal, proj, 18), 0.1)
        u, v = proj([[100, 50, 18]])[0]
        x, y = cal.pixel_to_robot(u, v, 18)                       # origin_mm + 默认轴向
        self.assertLess(np.hypot(x - 100, y - 50), 0.1)

    def test_rect_hint_and_off(self):
        proj = _tilted_cam()
        q = proj([[0, 0, 0], [240, 0, 0], [240, 180, 0], [0, 180, 0]])
        cal = self._cal(q, {"perspective": "off"}, cam_h=380)
        self.assertEqual(cal.tray_status["perspective"]["method"], "radial")
        self.assertIsNone(cal.tray_pose)
        cal.configure_tray({"fixed_px": {"x": 100, "y": 60, "w": 400, "h": 300}})
        self.assertIn("fixed_hint", cal.tray_status)

    def test_snap_corners(self):
        from vision.tray import snap_corners, merge_cfg
        proj = _tilted_cam()
        q = proj([[0, 0, 0], [240, 0, 0], [240, 180, 0], [0, 180, 0]])
        img = np.full((480, 640, 3), 30, np.uint8)
        cv2.fillPoly(img, [np.int32(np.round(q * 8))], (235, 235, 235), cv2.LINE_AA, shift=3)
        clicks = (q + np.float64([[5, -4], [-6, 3], [4, 5], [-3, -6]]))[[2, 0, 3, 1]]   # 乱序 + 偏 ~7px
        r = snap_corners(img, clicks, merge_cfg({}))
        self.assertTrue(r["ok"], r)
        self.assertLess(float(np.linalg.norm(np.float64(r["corners"]) - q, axis=1).max()), 1.0, r)

    def test_detect_tilted_tray(self):
        """自动检测模式: 梯形托盘不再被长宽比误判, 并按位姿换算。"""
        from vision.calibration import CoordinateCalibrator
        import json, tempfile, os
        proj = _tilted_cam(pitch_deg=22, yaw_deg=3)
        q = proj([[0, 0, 0], [240, 0, 0], [240, 180, 0], [0, 180, 0]])
        img = np.full((480, 640, 3), 30, np.uint8)
        cv2.fillPoly(img, [np.int32(np.round(q * 8))], (235, 235, 235), cv2.LINE_AA, shift=3)
        p = os.path.join(tempfile.mkdtemp(), "c.json")
        with open(p, "w") as f:
            json.dump({"tray": {"enabled": True, "inner_w_mm": 240, "inner_h_mm": 180, "origin_mm": [0, 0]}}, f)
        cal = CoordinateCalibrator(p)
        for _ in range(3):
            st = cal.update_tray(img, force=True)
        self.assertTrue(st["ok"], st)
        self.assertEqual(st["perspective"]["method"], "pose", st)
        self.assertLess(self._err(cal, proj, 18), 1.0)


class TestReviewFixes(unittest.TestCase):
    def test_env_conf_out_of_range(self):
        import os
        from vision.yolo_engine import _env_thresh
        class O:
            notice = ""
        o = O()
        os.environ["IS_YOLO_CONF"] = "1.2"
        try:
            self.assertEqual(_env_thresh("IS_YOLO_CONF", 0.5, 0.40, 0.05, 0.95, o), 0.40)
            self.assertIn("IS_YOLO_CONF", o.notice)
            os.environ["IS_YOLO_CONF"] = "abc"
            self.assertEqual(_env_thresh("IS_YOLO_CONF", 0.5, 0.40, 0.05, 0.95, o), 0.40)
            os.environ["IS_YOLO_CONF"] = "0.6"
            self.assertEqual(_env_thresh("IS_YOLO_CONF", 0.5, 0.40, 0.05, 0.95, o), 0.6)
        finally:
            del os.environ["IS_YOLO_CONF"]

    def test_color_vote_gap_is_unknown(self):
        import cv2
        from vision.yolo_detector import YoloWorkpieceDetector as YoloDetector
        det = YoloDetector.__new__(YoloDetector)
        det.hsv_cfg = {"hue_bins": [["red", 0, 10]]}     # 绿色落在空隙里
        img = np.zeros((40, 40, 3), np.uint8)
        img[:] = cv2.cvtColor(np.uint8([[[60, 220, 200]]]), cv2.COLOR_HSV2BGR)[0, 0]
        self.assertEqual(det._vote_color(img, 0, 0, 40, 40, None), "unknown")

    def test_tray_event_not_permanent(self):
        svc = VisionService(bus=MessageBus(self_dispatch=True), continuous_detect=False)
        svc._tray_event = (time.time() - 100, "托盘/相机位置变化 12.3 px, 已重新锁定")
        rep = svc.self_check(log=False)
        self.assertFalse(any("位置变化" in p for p in rep["problems"]))
        svc._tray_event = (time.time(), "托盘/相机位置变化 12.3 px, 已重新锁定")
        rep = svc.self_check(log=False)
        self.assertTrue(any("位置变化" in p for p in rep["problems"]))

    def test_calibrator_concurrent(self):
        import threading
        cal = CoordinateCalibrator(config_path="/tmp/_no_such_calib.json")
        errs = []
        def reader():
            for _ in range(300):
                try:
                    cal.pixel_to_robot(320, 240)
                except Exception as e:
                    errs.append(e)
        def writer():
            for i in range(100):
                cal.set_calibration_points([(100, 100), (500, 100), (500, 400), (100, 400)],
                                           [(0, 0), (40 + i % 3, 0), (40, 30), (0, 30)])
        ts = [threading.Thread(target=reader) for _ in range(3)] + [threading.Thread(target=writer)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])


class TestPoseValidation(unittest.TestCase):
    """阶段 0: 镜头高反推焦距的收敛性与容差校验。

    现场四角 + 160x160 内框的实测配置。焦距-高度曲线是单峰的, 旧的单调二分会夹在边界
    上并仍返回 ok=True; 现在超出可达范围必须明确失败并退回平面单应。
    """

    CORNERS = np.float32([[179, 127], [483, 122], [624, 400], [46, 388]])

    def _G(self):
        from vision import tray as T
        inner = T.inner_tray_pts({"inner_w_mm": 160, "inner_h_mm": 160})
        return T.cv2.getPerspectiveTransform(np.float32(inner), self.CORNERS).astype(np.float64)

    def _cfg(self, cam_h):
        import json, tempfile, os
        p = os.path.join(tempfile.mkdtemp(), "c.json")
        tray = {"enabled": True, "inner_w_mm": 160.0, "inner_h_mm": 160.0,
                "fixed_px": {"corners": self.CORNERS.tolist(), "image_size": [640, 480]}}
        with open(p, "w") as f:
            json.dump({"tray": tray, "image_size": [640, 480], "cam_height_mm": cam_h}, f)
        return p

    def test_reachable_height_is_accepted(self):
        from vision import tray as T
        p = T.estimate_pose(self._G(), (320.0, 240.0), (640, 480), cam_height_mm=115.0)
        self.assertTrue(p["ok"], p.get("msg"))
        self.assertEqual(p["source"], "cam_height")
        self.assertLessEqual(abs(p["height_mm"] - 115.0), 0.15 * 115.0)
        # 115mm 在这组四角上对应两支解 (f≈396/倾角44° 与 f≈884/倾角65°), 取倾角小的那支,
        # 否则坐标会悄悄改变 (旧二分实现的结果是 44°)
        self.assertLess(p["tilt_deg"], 50, f"二义性取错分支: tilt={p['tilt_deg']} f={p['f']}")

    def test_height_too_small_is_rejected(self):
        from vision import tray as T
        p = T.estimate_pose(self._G(), (320.0, 240.0), (640, 480), cam_height_mm=10.0)
        self.assertFalse(p["ok"], "10mm 低于该四角可达的最小镜头高, 必须失败")
        self.assertIn("相差", p["msg"])
        self.assertGreater(p["height_mm"], 10.0)      # 报的是真实可达值, 不是填写值

    def test_height_too_large_is_rejected(self):
        from vision import tray as T
        p = T.estimate_pose(self._G(), (320.0, 240.0), (640, 480), cam_height_mm=5000.0)
        self.assertFalse(p["ok"], "5000mm 超出可达范围, 不能再静默返回 ok")

    def test_fallback_to_planar_when_pose_fails(self):
        cal = CoordinateCalibrator(self._cfg(10.0))
        st = cal.tray_status["perspective"]
        self.assertNotEqual(st["method"], "pose")
        self.assertIn("相差", st["msg"])
        self.assertIsNotNone(cal.tray_H)                    # 平面单应仍然可用
        x, y = cal.pixel_to_tray(320.0, 240.0)
        self.assertTrue(np.isfinite(x) and np.isfinite(y))

    def test_self_check_missing_ref_points_is_actionable(self):
        svc = VisionService(MessageBus())
        svc.calibrator = CoordinateCalibrator(self._cfg(115.0))
        rep = svc.self_check(log=False)
        txt = " ".join(rep["problems"])
        self.assertIn("参考点", txt)
        self.assertNotIn("不存在", txt, "标定文件存在时不能再报「不存在」")
        self.assertTrue(any("畸变" in h for h in rep["hints"]), "未做畸变校正应作为单独提示")


class TestClassMeta(unittest.TestCase):
    """classes.json 的元数据必须按「模型类名」索引。

    旧实现用 c["id"] (int) 当键, 上层却拿 d.name (str) 查, 结果 height_mm / thin_wall /
    decode_qr / info 全部失效 (高度恒为 0, 视差校正等于没做)。
    """

    def test_meta_keyed_by_model_class_name(self):
        import json, tempfile, os
        from vision.yolo_detector import YoloWorkpieceDetector

        class _StubEngine:
            class_names = ["红色镂空立方体", "黄色三棱柱"]
            is_ready = False
            last_error = "stub"

            def describe(self):
                return "stub"

        p = os.path.join(tempfile.mkdtemp(), "classes.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"classes": [{"id": 0, "shape_cn": "hollow_cube", "height_mm": 30},
                                   {"id": 1, "shape_cn": "tri_prism", "height_mm": 30}]}, f)
        det = YoloWorkpieceDetector(engine=_StubEngine(), meta_path=p)
        self.assertEqual(set(det.class_meta), {"红色镂空立方体", "黄色三棱柱"})
        self.assertEqual(det.class_meta["黄色三棱柱"]["shape_cn"], "tri_prism")
        self.assertEqual(det.class_meta["红色镂空立方体"]["height_mm"], 30)



import unittest.mock


class TestColorArbitration(unittest.TestCase):
    """A1: category 第一段取 shape_cn (不再随模型类名颜色变) + model_color/HSV 冲突仲裁。

    全部用例端到端走 `VisionService.scan()`: 真实 ReplayCamera + 真 YoloWorkpieceDetector,
    只在实例上覆盖 `_infer` 注入 Detection (不碰真实模型/相机)。
    """

    SHAPE = "hemisphere"
    NAMES = ["红色半球", "蓝色半球", "白色半球", "绿色半球"]
    MODEL_COLOR = {"红色半球": "red", "蓝色半球": "blue", "白色半球": "white", "绿色半球": "green"}
    HSV_CFG = {"black_v_max": 60, "white_s_max": 45, "white_v_min": 150,
               "hue_bins": [["red", 0, 10], ["red", 170, 179],
                            ["green", 40, 80], ["blue", 100, 130]]}
    # 帧上四块纯色 (BGR): 改框位置但颜色不变, 用来证明冲突记忆的键不含位置
    BOX_RED = (100, 100, 200, 200)
    BOX_RED2 = (300, 300, 400, 400)
    BOX_BLUE = (300, 100, 400, 200)
    BOX_GREEN = (100, 300, 200, 400)

    class _StubEngine:
        def __init__(self, names):
            self.class_names = list(names)
            self.is_ready = True
            self.last_error = ""

        def describe(self):
            return "stub-color-engine"

    def _setup(self, source="hsv", color_extra=None):
        import cv2
        import json
        import os
        import tempfile
        from vision.config import load_vision_config
        from vision.interfaces import Detection
        from vision.yolo_detector import YoloWorkpieceDetector

        tmp = tempfile.mkdtemp(prefix="a1_color_")
        frame = np.full((480, 640, 3), 40, np.uint8)
        frame[100:200, 100:200] = (0, 0, 255)      # 红 (框 A)
        frame[100:200, 300:400] = (255, 0, 0)      # 蓝
        frame[300:400, 100:200] = (0, 255, 0)      # 绿
        frame[300:400, 300:400] = (0, 0, 255)      # 红 (框 A2, 只换位置)
        cv2.imwrite(os.path.join(tmp, "frame.png"), frame)

        meta = {"color_names": dict([("red", "红色"), ("blue", "蓝色"),
                                     ("white", "白色"), ("green", "绿色")]),
                "hsv": dict(self.HSV_CFG),
                "classes": [{"id": i, "name": n, "shape_cn": self.SHAPE, "color": None,
                             "model_color": self.MODEL_COLOR[n], "height_mm": 30}
                            for i, n in enumerate(self.NAMES)]}
        meta_path = os.path.join(tmp, "classes.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        col = {"source": source}
        col.update(color_extra or {})
        cfg_path = os.path.join(tmp, "vision_config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"color": col, "record": {"enabled": False},
                       "camera": {"type": "replay", "path": tmp, "fps": 60,
                                  "loop": True, "stale_s": 5.0}},
                      f, ensure_ascii=False, indent=2)
        cfg = load_vision_config(cfg_path)
        det = YoloWorkpieceDetector(engine=self._StubEngine(self.NAMES),
                                    meta_path=meta_path, config=cfg)
        svc = VisionService(MessageBus(), detector_factory=lambda cal: det,
                            continuous_detect=False, config=cfg)
        # A3 (svc-gate) 起把"比赛模式启动前的现场门禁"加进了 VisionService.start(): 锁文件存在且
        # 有 fail 级门禁时抛异常拒启动。本机测试环境没有现场标定/成像锁定, 而这里要测的是
        # 「race_mode()==True 时颜色冲突仲裁怎么走」——门禁本身由 vision/test_vision_gates.py 覆盖,
        # 所以只在本测试类里把门禁这一层 stub 掉; scan() 仍走真实 service/detector/相机。
        patcher = unittest.mock.patch.object(VisionService, "_enforce_gates", lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        svc.start()
        self.addCleanup(svc.stop)
        self._det, self._Detection, self._tmp = det, Detection, tmp
        return svc

    def _detecting(self, name, conf=0.9, box=None):
        D = self._Detection
        box = box or self.BOX_RED
        self._det._infer = lambda frame: [D(0, name, conf, *box)]

    def _scan(self, svc, n=2):
        ts, _ref = svc.scan(n_frames=n, skip_frames=0)
        self.assertTrue(ts, "scan() 应至少返回 1 个目标 (replay 相机/注入是否失效?)")
        return ts

    def test_category_is_independent_of_model_color_word(self):
        """核心: 同一张图 + 同一 HSV, 把模型类从「红色半球」换成「蓝色半球」-> category 逐字符相同。"""
        svc = self._setup()
        self._detecting("红色半球")
        a = self._scan(svc)[0]
        self._detecting("蓝色半球")
        b = self._scan(svc)[0]
        self.assertEqual(a.category, "hemisphere|red|")
        self.assertEqual(b.category, a.category, "category 不该受模型类名里的颜色影响")

    def test_shape_takes_shape_cn_not_class_name(self):
        svc = self._setup()
        self._detecting("白色半球")
        t = self._scan(svc)[0]
        self.assertEqual(t.shape, "hemisphere")
        self.assertEqual(t.category.split("|")[0], "hemisphere")
        for w in ("红", "黄", "蓝", "白", "灰", "色"):
            self.assertNotIn(w, t.shape)
        self.assertEqual(t.class_name, "白色半球")

    def test_conflict_warns_and_records_diagnostics_in_debug_mode(self):
        svc = self._setup()
        self._detecting("蓝色半球")               # 类名色 blue, HSV 投票 red
        with self.assertLogs("vision.service", level="WARNING") as cm:
            t = self._scan(svc)[0]
        self.assertTrue(t.diagnostics.get("color_conflict"))
        self.assertEqual(t.diagnostics.get("model_color"), "blue")
        self.assertEqual(t.diagnostics.get("hsv_color"), "red")
        self.assertEqual(t.diagnostics.get("color_source"), "hsv")
        self.assertTrue(any("颜色冲突" in m for m in cm.output), cm.output)
        self.assertGreaterEqual(t.confidence, 0.45, "调试模式只告警, 不压置信度")
        self.assertEqual(t.color, "red")

    def test_race_mode_pushes_once_then_adopts_hsv(self):
        """比赛模式: 同一冲突组合首次压置信度 -> 抖料; 换位置后第二次采信 HSV (键不含位置)。"""
        import os
        import tempfile
        from unittest import mock
        lock = os.path.join(tempfile.mkdtemp(prefix="a1_lock_"), "is_competition.lock")
        with open(lock, "w"):
            pass
        with mock.patch.dict(os.environ, {"IS_COMPETITION_LOCK": lock}):
            svc = self._setup(source="model")     # model 来源, 颜色初始=类名色, 便于观察"改成 HSV"
            self._detecting("蓝色半球", box=self.BOX_RED)
            a = self._scan(svc)[0]
            self.assertLess(a.confidence, 0.45, "首次冲突必须压到 COMMIT_CONF(0.45) 以下")
            self.assertEqual(a.category, "hemisphere|blue|")
            self.assertTrue(a.diagnostics.get("color_conflict_first"))
            self._detecting("蓝色半球", box=self.BOX_RED2)   # 同一组合, 位置换了
            b = self._scan(svc)[0]
            self.assertGreaterEqual(b.confidence, 0.45, "同一组合第二次不再压置信度")
            self.assertEqual(b.color, "red")
            self.assertEqual(b.category, "hemisphere|red|")
            self.assertEqual(b.label, "红色hemisphere")

    def test_conflict_ttl_expiry_pushes_again(self):
        import os
        import tempfile
        from unittest import mock
        lock = os.path.join(tempfile.mkdtemp(prefix="a1_lock_"), "is_competition.lock")
        with open(lock, "w"):
            pass
        with mock.patch.dict(os.environ, {"IS_COMPETITION_LOCK": lock}):
            svc = self._setup(color_extra={"conflict_ttl_s": 0.001})  # TTL 1ms -> 两次 scan 之间必然过期
            self._detecting("蓝色半球")
            a = self._scan(svc)[0]
            b = self._scan(svc)[0]
            self.assertLess(a.confidence, 0.45)
            self.assertLess(b.confidence, 0.45, "TTL 到期后记忆被清, 应再压一次")

    def test_hsv_source_is_legacy_behaviour(self):
        svc = self._setup(source="hsv")
        self._detecting("红色半球")
        t = self._scan(svc)[0]
        self.assertEqual(t.color, "red")
        self.assertEqual(t.category, "hemisphere|red|")
        self.assertFalse(t.diagnostics.get("color_conflict"))

    def test_model_source_uses_model_color_when_consistent(self):
        svc = self._setup(source="model")
        self._detecting("红色半球")
        t = self._scan(svc)[0]
        self.assertEqual(t.diagnostics.get("color_source"), "model")
        self.assertFalse(t.diagnostics.get("color_conflict"))
        self.assertEqual(t.color, "red")
        self.assertEqual(t.category, "hemisphere|red|")

    def test_colour_missing_from_model_colour_still_uses_hsv(self):
        """model_color 里没有 green; 绿货被识别成「红色半球」类 -> 颜色仍取 HSV。"""
        svc = self._setup()
        self._detecting("红色半球", box=self.BOX_GREEN)
        t = self._scan(svc)[0]
        self.assertEqual(t.color, "green")
        self.assertEqual(t.category, "hemisphere|green|")
        self.assertTrue(t.diagnostics.get("color_conflict"))

    def test_shipped_classes_json_model_color_and_shape(self):
        import json
        import os
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "classes.json")
        with open(p, encoding="utf-8") as f:
            meta = json.load(f)
        shapes = {"hollow_cube", "tri_prism", "hemisphere", "cone",
                  "penta_prism", "tetrahedron", "cylinder"}
        cn = meta.get("color_names") or {}
        self.assertTrue(meta.get("classes"))
        for c in meta["classes"]:
            self.assertIn(c.get("shape_cn"), shapes, c)
            self.assertIn("model_color", c, "%s 缺 model_color" % c.get("name"))
            if c["model_color"] is not None:
                self.assertIn(c["model_color"], cn, c)
            self.assertIn(c.get("color"), (None,) + tuple(cn), c)



class TestInfoAndQr(unittest.TestCase):
    """A4: 信息维度 (require_info) + QR 解码加强。全部用例端到端走 VisionService.scan()。

    真 ReplayCamera + 真 YoloWorkpieceDetector, 只在实例上覆盖 `_infer` 注入 Detection、
    或把 `det._qr` 换成假解码器 —— 不碰真实模型/相机, 也不需要 cv2 编出二维码
    (板上无 opencv-contrib, cv2.QRCodeEncoder 不可用, 所以"命中"用假解码器证明流程)。
    """

    SHAPE = "hemisphere"
    NAMES = ["红色半球", "蓝色半球"]
    BOX = (100, 100, 200, 200)          # 红块 (HSV 投票 = red)
    BOX2 = (400, 300, 500, 400)         # 远离 BOX, 融合后是第 2 个目标

    class _StubEngine:
        def __init__(self, names):
            self.class_names = list(names)
            self.is_ready = True
            self.last_error = ""

        def describe(self):
            return "stub-info-engine"

    class _FakeQR:
        """假解码器: 记录真实调用次数, 第一次就"命中"; 可选每次调用耗时 (验证预算)。"""

        def __init__(self, payload="QR-123", delay_s=0.0):
            self.payload = payload
            self.delay_s = delay_s
            self.calls = 0

        def detectAndDecode(self, img):
            self.calls += 1
            if self.delay_s:
                time.sleep(self.delay_s)
            return (self.payload, None, None)

    def _setup(self, require_info=False, qr_budget_ms=500, classes_extra=None):
        import cv2
        import json
        import os
        import shutil
        import tempfile
        from vision.config import load_vision_config
        from vision.interfaces import Detection
        from vision.yolo_detector import YoloWorkpieceDetector

        tmp = tempfile.mkdtemp(prefix="a4_info_")
        self.addCleanup(shutil.rmtree, tmp, True)
        frame = np.full((480, 640, 3), 40, np.uint8)
        frame[100:200, 100:200] = (0, 0, 255)      # 红 (BGR)
        cv2.imwrite(os.path.join(tmp, "frame.png"), frame)

        cls = []
        for i, n in enumerate(self.NAMES):
            c = {"id": i, "name": n, "shape_cn": self.SHAPE, "color": None,
                 "model_color": "red", "height_mm": 30}
            c.update((classes_extra or {}).get(n) or {})
            cls.append(c)
        meta = {"color_names": {"red": "红色"},
                "hsv": {"hue_bins": [["red", 0, 10], ["red", 170, 179]]},
                "classes": cls}
        meta_path = os.path.join(tmp, "classes.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        cfg_path = os.path.join(tmp, "vision_config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"require_info": require_info,
                       "record": {"enabled": False},
                       "detect": {"qr_budget_ms": qr_budget_ms},
                       "camera": {"type": "replay", "path": tmp, "fps": 60,
                                  "loop": True, "stale_s": 5.0}},
                      f, ensure_ascii=False, indent=2)
        cfg = load_vision_config(cfg_path)
        det = YoloWorkpieceDetector(engine=self._StubEngine(self.NAMES),
                                    meta_path=meta_path, config=cfg)
        svc = VisionService(MessageBus(), detector_factory=lambda cal: det,
                            continuous_detect=False, config=cfg)
        svc.start()
        self.addCleanup(svc.stop)
        self._det, self._Detection, self._tmp = det, Detection, tmp
        return svc

    def _detecting(self, name, conf=0.9, boxes=None):
        D = self._Detection
        boxes = boxes or [self.BOX]
        self._det._infer = lambda frame: [D(0, name, conf, *b) for b in boxes]

    def _scan(self, svc, n=2):
        ts, _ref = svc.scan(n_frames=n, skip_frames=0)
        self.assertTrue(ts, "scan() 应至少返回 1 个目标")
        return ts

    # ---------------------------------------------------------------- 预算
    def test_qr_budget_zero_never_decodes(self):
        """qr_budget_ms=0 -> 本 scan 完全不进解码循环, 但仍如实标 exhausted。"""
        svc = self._setup(require_info=True, qr_budget_ms=0,
                          classes_extra={"红色半球": {"decode_qr": True}})
        fake = self._FakeQR()
        self._det._qr = ("aruco", fake)
        self._detecting("红色半球")
        t = self._scan(svc)[0]
        self.assertEqual(fake.calls, 0, "预算 0 时不该调用解码器")
        self.assertFalse(t.diagnostics["qr_attempted"])
        self.assertEqual(t.diagnostics["qr_ms"], 0.0)
        self.assertTrue(t.diagnostics["qr_budget_exhausted"])
        self.assertEqual(t.info, "unknown", "解不出信息必须是 unknown")
        self.assertEqual(t.category, "hemisphere|red|unknown")
        self.assertLess(t.confidence, 0.45, "require_info 解不出 -> 压到 COMMIT_CONF 以下")

    def test_qr_budget_is_per_scan_not_per_item(self):
        """预算按整次 scan 计: 第 1 个目标耗尽预算后, 同一次 scan 的第 2 个目标不再尝试。"""
        svc = self._setup(qr_budget_ms=10, classes_extra={"红色半球": {"decode_qr": True}})
        fake = self._FakeQR(delay_s=0.05)          # 每次调用 50ms >> 10ms 预算
        self._det._qr = ("aruco", fake)
        self._detecting("红色半球", boxes=[self.BOX, self.BOX2])
        ts = self._scan(svc)
        self.assertEqual(len(ts), 2, "两个相距很远的框应融合成 2 个目标")
        self.assertEqual(fake.calls, 1, "预算耗尽后不再调用解码器")
        self.assertTrue(ts[0].diagnostics["qr_attempted"])
        self.assertTrue(ts[0].diagnostics["qr_budget_exhausted"])
        self.assertFalse(ts[1].diagnostics["qr_attempted"], "同一次 scan 的后续目标不再尝试解码")
        self.assertTrue(ts[1].diagnostics["qr_budget_exhausted"])

    def test_budget_resets_next_scan(self):
        """预算按 scan 重置: 下一次 scan 又能解码 (不是一次耗尽永久失效)。"""
        svc = self._setup(classes_extra={"红色半球": {"decode_qr": True}})
        fake = self._FakeQR()
        self._det._qr = ("aruco", fake)
        self._detecting("红色半球")
        self._scan(svc)
        self._scan(svc)
        self.assertEqual(fake.calls, 2, "每次 scan 应重新获得预算")

    # ---------------------------------------------------- 解码器如实上报
    def test_missing_decoder_is_reported_not_silent(self):
        """板上两个解码器都没有时: qr_decoder=none + attempted=False, 不许静默降级后假装试过。"""
        svc = self._setup(require_info=True, classes_extra={"红色半球": {"decode_qr": True}})
        self._det._qr = ("none", None)             # 模拟 cv2 里两个解码器都不可用
        self._detecting("红色半球")
        t = self._scan(svc)[0]
        self.assertEqual(t.diagnostics["qr_decoder"], "none")
        self.assertFalse(t.diagnostics["qr_attempted"])
        self.assertTrue(t.diagnostics["qr_budget_exhausted"])
        self.assertEqual(t.info, "unknown")

    def test_decoder_name_is_recorded(self):
        """真 cv2 解码器: diagnostics 必须写出实际用的那个 (aruco / qrcode / none), 不静默。"""
        svc = self._setup(classes_extra={"红色半球": {"decode_qr": True}})
        self._detecting("红色半球")
        t = self._scan(svc)[0]
        self.assertIn(t.diagnostics["qr_decoder"], ("aruco", "qrcode", "none"))
        self.assertIsInstance(t.diagnostics["qr_ms"], float)
        self.assertNotIn("wechat", str(t.diagnostics["qr_decoder"]))

    # ---------------------------------------------------- info 声明/兼容
    def test_info_dict_and_template_set(self):
        """info 为 dict: text 作声明值, template_set 带进 diagnostics, 命中的码拼在后面。"""
        svc = self._setup(classes_extra={"红色半球": {"info": {"text": "T1",
                                                              "template_set": "S1",
                                                              "decode_qr": True}}})
        fake = self._FakeQR("QR-123")
        self._det._qr = ("aruco", fake)
        self._detecting("红色半球")
        t = self._scan(svc)[0]
        self.assertEqual(t.info, "T1:QR-123")
        self.assertEqual(t.category, "hemisphere|red|T1:QR-123")
        self.assertEqual(t.diagnostics["template_set"], "S1")
        self.assertEqual(t.diagnostics["qr_decoder"], "aruco")
        self.assertTrue(t.diagnostics["qr_attempted"])
        self.assertEqual(fake.calls, 1, "命中即停: 只调一次解码器")

    def test_legacy_top_level_decode_qr_still_works(self):
        """兼容旧写法: info 里没有 decode_qr 时回退顶层 decode_qr。"""
        svc = self._setup(classes_extra={"红色半球": {"info": {"text": "T2"},
                                                      "decode_qr": True}})
        fake = self._FakeQR("QR-999")
        self._det._qr = ("aruco", fake)
        self._detecting("红色半球")
        t = self._scan(svc)[0]
        self.assertEqual(t.info, "T2:QR-999")
        self.assertEqual(fake.calls, 1)

    def test_info_decode_qr_wins_over_top_level(self):
        """info.decode_qr 优先: 显式 false 时, 即使顶层 decode_qr=true 也不解码。"""
        svc = self._setup(require_info=True,
                          classes_extra={"红色半球": {"info": {"text": "T3", "decode_qr": False},
                                                      "decode_qr": True}})
        fake = self._FakeQR()
        self._det._qr = ("aruco", fake)
        self._detecting("红色半球")
        t = self._scan(svc)[0]
        self.assertEqual(fake.calls, 0, "info.decode_qr=False 必须压过顶层 true")
        self.assertEqual(t.info, "T3", "声明值已满足 require_info, 不该写成 unknown")
        self.assertGreaterEqual(t.confidence, 0.45)
        self.assertFalse(t.diagnostics["qr_attempted"])

    def test_no_declaration_means_no_qr_attempt(self):
        """没有任何 info/decode_qr 声明 -> 完全不碰二维码 (与 A1 及以前行为一致)。"""
        svc = self._setup()
        fake = self._FakeQR()
        self._det._qr = ("aruco", fake)
        self._detecting("红色半球")
        t = self._scan(svc)[0]
        self.assertEqual(fake.calls, 0)
        self.assertFalse(t.diagnostics["qr_attempted"])
        self.assertEqual(t.info, "")
        self.assertEqual(t.category, "hemisphere|red|")

    # ------------------------------------------------- require_info 语义
    def test_require_info_satisfied_by_declaration(self):
        svc = self._setup(require_info=True, classes_extra={"红色半球": {"info": {"text": "T4"}}})
        self._detecting("红色半球")
        t = self._scan(svc)[0]
        self.assertEqual(t.info, "T4")
        self.assertEqual(t.category, "hemisphere|red|T4")
        self.assertGreaterEqual(t.confidence, 0.45, "信息解出时不该压置信度")
        self.assertNotIn("info_missing", t.diagnostics)

    def test_require_info_false_is_identical_to_a1(self):
        """回归: require_info=false (默认) 时, 输出与 A1 逐字段一致 (info 保持空串, 不压置信度)。"""
        svc = self._setup(require_info=False)
        self._detecting("红色半球", conf=0.9)
        t = self._scan(svc)[0]
        self.assertEqual(t.info, "", "默认关时 info 必须还是空串 (不是 unknown)")
        self.assertEqual(t.category, "hemisphere|red|")
        self.assertEqual(t.confidence, 0.9, "默认关时绝不压置信度")
        # 无颜色冲突 -> service 不重算 label, 保持 _build_target 的 classes.json name
        self.assertEqual(t.label, "红色半球")
        self.assertEqual(t.color, "red")
        self.assertNotIn("info_missing", t.diagnostics)
        self.assertFalse(t.diagnostics["qr_attempted"])

    def test_require_info_unknown_keeps_three_segments(self):
        """require_info=true 解不出 -> 第三段必须是 unknown 且仍是三段 (不许空串)。"""
        svc = self._setup(require_info=True)
        self._detecting("红色半球")
        t = self._scan(svc)[0]
        self.assertEqual(len(t.category.split("|")), 3)
        self.assertEqual(t.category.split("|")[2], "unknown")
        self.assertTrue(t.diagnostics["info_missing"])

    # ---------------------------------------------------- category_core
    def test_category_core_always_shape_color(self):
        for req in (False, True):
            with self.subTest(require_info=req):
                svc = self._setup(require_info=req)
                self._detecting("红色半球")
                t = self._scan(svc)[0]
                core = t.diagnostics["category_core"]
                self.assertEqual(core, "hemisphere|red|")
                self.assertEqual(core, "%s|%s|" % (t.shape, t.color))
                self.assertEqual(core.count("|"), 2, "category_core 只有两段 + 尾随 |")
                self.assertTrue(core.endswith("|"))
                self.assertTrue(t.category.startswith(core))

    def test_category_core_present_in_hsv_fallback(self):
        """引擎不可用时 (HSV 回退) 每个目标也要有 category_core。"""
        svc = self._setup()
        self._det.engine.is_ready = False        # 走 _hsv_fallback
        ts = self._scan(svc)
        for t in ts:
            self.assertEqual(t.diagnostics.get("category_core"), "%s|%s|" % (t.shape, t.color))




class TestMultiFrameMatch(unittest.TestCase):
    """A5: 多帧融合匹配半径自适应 max(match_px_min, match_ratio × min(框宽, 框高))。

    直接对 detect_multi() 注入 `_infer` (每帧一批 Detection) —— 融合/匹配逻辑本身是被测对象,
    不碰真实模型与相机。
    """

    BOX = (100, 100, 180, 180)          # 80x80, 中心 (140,140)
    BOX_60 = (160, 100, 240, 180)       # 同尺寸, 中心 (200,140): 中心距 60px
    BOX_JIT = (115, 100, 195, 180)      # 同尺寸, 中心 (155,140): 抖动 15px
    BIG = (100, 100, 400, 400)          # 300x300
    BIG_60 = (160, 100, 460, 400)       # 同尺寸, 中心距 60px

    class _StubEngine:
        def __init__(self, names):
            self.class_names = list(names)
            self.is_ready = True
            self.last_error = ""

        def describe(self):
            return "stub-match-engine"

    def _det(self, match_ratio=None):
        import json
        import os
        import shutil
        import tempfile
        from vision.calibration import CoordinateCalibrator
        from vision.config import load_vision_config
        from vision.interfaces import Detection
        from vision.yolo_detector import YoloWorkpieceDetector

        tmp = tempfile.mkdtemp(prefix="a5_match_")
        self.addCleanup(shutil.rmtree, tmp, True)
        names = ["红色半球", "蓝色半球"]
        meta_path = os.path.join(tmp, "classes.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"color_names": {"red": "红色", "blue": "蓝色"},
                       "hsv": {"hue_bins": [["red", 0, 10], ["red", 170, 179]]},
                       "classes": [{"id": i, "name": n, "shape_cn": "hemisphere",
                                    "color": None, "model_color": "red", "height_mm": 30}
                                   for i, n in enumerate(names)]},
                      f, ensure_ascii=False)
        scn = {"min_hit_ratio": 0.5}
        if match_ratio is not None:
            scn["match_ratio"] = match_ratio
        cfg_path = os.path.join(tmp, "vision_config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"record": {"enabled": False}, "scan": scn,
                       "camera": {"type": "replay", "path": tmp, "fps": 60}},
                      f, ensure_ascii=False)
        cfg = load_vision_config(cfg_path)
        self._Detection = Detection
        return YoloWorkpieceDetector(CoordinateCalibrator(),
                                     engine=self._StubEngine(names),
                                     meta_path=meta_path, config=cfg)

    def _d(self, name, box, conf=0.9):
        D = self._Detection
        return D(0 if name == "红色半球" else 1, name, conf, *box)

    def _run(self, det, per_frame, min_hit_ratio=0.5):
        seq = [list(batch) for batch in per_frame]      # 每帧一批检测 (顺序消费)
        det._infer = lambda frame: seq.pop(0) if seq else []
        frames = [np.zeros((480, 640, 3), np.uint8) for _ in per_frame]
        ts, _ref = det.detect_multi(frames, min_hit_ratio=min_hit_ratio)
        return ts

    # ------------------------------------------------------------ 半径公式
    def test_match_radius_formula(self):
        det = self._det()
        self.assertAlmostEqual(det._match_radius(self._d("红色半球", self.BOX), 25.0), 28.0)
        self.assertAlmostEqual(det._match_radius(self._d("红色半球", self.BIG), 25.0), 105.0)
        self.assertAlmostEqual(det._match_radius(self._d("红色半球", (100, 100, 120, 120)), 25.0), 12.0)

    def test_match_ratio_zero_falls_back_to_match_px(self):
        """旧参数兜底: match_ratio<=0 时用调用方传的 match_px (service.scan() 仍在传它)。"""
        det = self._det(match_ratio=0)
        self.assertAlmostEqual(det._match_radius(self._d("红色半球", self.BIG), 25.0), 25.0)

    # ------------------------------------------------------ 计划里的三条验收
    def test_60px_apart_with_80px_box_stays_two(self):
        """中心距 60px / 框 80px: 半径 28px < 60px -> 保持 2 个目标 (没有误并)。"""
        det = self._det()
        ts = self._run(det, [[self._d("红色半球", self.BOX),
                              self._d("红色半球", self.BOX_60)]] * 5)
        self.assertEqual(len(ts), 2)
        self.assertEqual(sorted(t.hits for t in ts), [5, 5])

    def test_15px_jitter_merges_to_one(self):
        """抖动 15px: 半径 28px > 15px -> 合并成 1 个目标。"""
        det = self._det()
        ts = self._run(det, [[self._d("红色半球", self.BOX)],
                             [self._d("红色半球", self.BOX)],
                             [self._d("红色半球", self.BOX_JIT)],
                             [self._d("红色半球", self.BOX_JIT)],
                             [self._d("红色半球", self.BOX)]])
        self.assertEqual(len(ts), 1)
        self.assertEqual(ts[0].hits, 5)

    def test_one_object_two_classes_takes_majority(self):
        """一物两类: 5 帧里 2 帧 A / 3 帧 B -> 1 个目标且取多数 B (匹配与类别无关)。"""
        det = self._det()
        ts = self._run(det, [[self._d("红色半球", self.BOX)],
                             [self._d("红色半球", self.BOX_JIT)],
                             [self._d("蓝色半球", self.BOX)],
                             [self._d("蓝色半球", self.BOX_JIT)],
                             [self._d("蓝色半球", self.BOX)]])
        self.assertEqual(len(ts), 1)
        self.assertEqual(ts[0].class_name, "蓝色半球", "类别应按加权投票取多数")
        self.assertEqual(ts[0].hits, 5)

    def test_large_box_widens_radius_and_merges_60px(self):
        """大件半径按比例放宽: 300px 框 -> 105px > 60px, 交替出现的两个框合成 1 个目标
        (旧的固定 25px 会拆成 2 个)。"""
        det = self._det()
        ts = self._run(det, [[self._d("红色半球", self.BIG)], [self._d("红色半球", self.BIG_60)]] * 3)
        self.assertEqual(len(ts), 1, "自适应半径应把 60px 内的同一件合并")
        self.assertEqual(ts[0].hits, 6)



class TestTrayWhiteBalance(unittest.TestCase):
    """A6: 托盘白基准归一化 (默认关)。

    取样 = 托盘内框 (这里用 detect.roi) 内 & 低饱和 & 高亮度 & **不在任何检测框内**;
    增益 = 白斑三通道均值 / 各通道均值 (把偏色的"白"拉回中性), 限幅
    [color.wb_gain_min, color.wb_gain_max]; 像素不足 wb_min_pixels -> 沿用上次增益;
    关掉 -> 与 A5 行为逐字段一致。增益只乘在「颜色投票前的裁剪图」上, 送模型/精定位的帧不变。
    """

    SHAPE = "hemisphere"
    NAMES = ["红色半球"]
    BOX = (100, 100, 200, 200)          # 物体框 (白基准取样要排除它)
    ROI = [[0, 0], [640, 0], [640, 480], [0, 480]]
    WHITE = (220, 205, 190)             # BGR: 托盘"白"偏蓝
    OBJ = (185, 170, 150)               # BGR: 物体像素 (偏蓝, 未归一化判成蓝)
    HSV = {"black_v_max": 60, "white_s_max": 45, "white_v_min": 150,
           "hue_bins": [["blue", 100, 130]]}
    EXPECT_GAIN = [0.932, 1.0, 1.079]   # B=220 G=205 R=190 -> 目标亮度 205
    BLUE = ("blue", "light_blue")

    class _StubEngine:
        def __init__(self, names):
            self.class_names = list(names)
            self.is_ready = True
            self.last_error = ""

        def describe(self):
            return "stub-wb-engine"

    def _frame(self):
        f = np.zeros((480, 640, 3), np.uint8)
        f[:, :] = self.WHITE
        f[self.BOX[1]:self.BOX[3], self.BOX[0]:self.BOX[2]] = self.OBJ
        return f

    def _files(self, prefix, color=None, hsv=None, roi=None, replay=False):
        """tmp classes.json + vision_config.json + 无托盘的 calib.json (不碰现场标定)。"""
        import json
        import os
        import shutil
        import tempfile
        from vision.config import load_vision_config

        tmp = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(shutil.rmtree, tmp, True)
        meta_path = os.path.join(tmp, "classes.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"color_names": {"red": "红色", "blue": "蓝色", "white": "白色"},
                       "hsv": dict(hsv or self.HSV),
                       "classes": [{"id": 0, "name": self.NAMES[0], "shape_cn": self.SHAPE,
                                    "color": None, "model_color": "red", "height_mm": 30}]},
                      f, ensure_ascii=False, indent=2)
        cfgd = {"record": {"enabled": False},
                "detect": {"roi": self.ROI if roi is None else roi},
                "color": dict(color or {})}
        if replay:
            cfgd["camera"] = {"type": "replay", "path": tmp, "fps": 60, "loop": True, "stale_s": 5.0}
        cfg_path = os.path.join(tmp, "vision_config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfgd, f, ensure_ascii=False, indent=2)
        # 标定: 用仓库现场标定为底, 只把托盘模式关掉 (本测试要自己控 ROI, 也要确定性)
        calib_path = os.path.join(tmp, "calib.json")
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "calib_config.json"), encoding="utf-8") as f:
            calib = json.load(f)
        calib["tray"] = {"enabled": False}
        with open(calib_path, "w", encoding="utf-8") as f:
            json.dump(calib, f, ensure_ascii=False)
        return tmp, meta_path, load_vision_config(cfg_path), calib_path

    def _det(self, color=None, hsv=None, roi=None):
        """直接构造 detector (桩引擎, 不启 service): 测 _update_tray_wb/_vote_color/wb_info。"""
        from vision.yolo_detector import YoloWorkpieceDetector

        _tmp, meta_path, cfg, calib_path = self._files("a6_wb_", color=color, hsv=hsv, roi=roi)
        return YoloWorkpieceDetector(CoordinateCalibrator(config_path=calib_path),
                                     engine=self._StubEngine(self.NAMES),
                                     meta_path=meta_path, config=cfg)

    def _service(self, color=None):
        """端到端: replay 相机 + 真 detector, 只覆盖 _infer 注入 Detection。"""
        import cv2
        import os
        from vision.interfaces import Detection
        from vision.yolo_detector import YoloWorkpieceDetector

        tmp, meta_path, cfg, calib_path = self._files("a6_wb_svc_", color=color, replay=True)
        cv2.imwrite(os.path.join(tmp, "frame.png"), self._frame())
        det = YoloWorkpieceDetector(CoordinateCalibrator(config_path=calib_path),
                                    engine=self._StubEngine(self.NAMES),
                                    meta_path=meta_path, config=cfg)
        svc = VisionService(MessageBus(), detector_factory=lambda cal: det,
                            continuous_detect=False, config=cfg)
        svc.start()
        self.addCleanup(svc.stop)
        det._infer = lambda frame: [Detection(0, self.NAMES[0], 0.9, *self.BOX)]
        return svc, det

    @staticmethod
    def _json(ts):
        import json

        return json.dumps([{"label": t.label, "category": t.category, "color": t.color,
                            "shape": t.shape, "conf": t.confidence, "u": t.u_pixel,
                            "v": t.v_pixel, "x_mm": t.x_mm, "y_mm": t.y_mm,
                            "class": t.class_name, "info": t.info, "hits": t.hits,
                            "angle": t.angle_deg, "bbox": list(t.bbox)} for t in ts],
                          ensure_ascii=False, sort_keys=True, default=str)

    # ---------------------------------------------------------------- IS_* 覆盖
    def test_wb_env_overrides(self):
        """IS_TRAY_WB / _GAIN_MIN / _GAIN_MAX / _MIN_PIXELS 覆盖配置段与 detector 属性。"""
        import json
        import os
        from vision.config import load_vision_config
        from vision.yolo_detector import YoloWorkpieceDetector

        tmp, meta_path, _cfg, calib_path = self._files("a6_wb_env_")
        env = {"IS_VISION_CONFIG": os.path.join(tmp, "vision_config.json"),
               "IS_CALIB_CONFIG": calib_path,
               "IS_TRAY_WB": "1",
               "IS_TRAY_WB_GAIN_MIN": "0.8",
               "IS_TRAY_WB_GAIN_MAX": "1.2",
               "IS_TRAY_WB_MIN_PIXELS": "333"}
        with unittest.mock.patch.dict(os.environ, env):
            cfg = load_vision_config()
            det = YoloWorkpieceDetector(CoordinateCalibrator(config_path=calib_path),
                                        engine=self._StubEngine(self.NAMES),
                                        meta_path=meta_path, config=cfg)
        self.assertEqual(cfg.color["source"], "hsv")
        self.assertTrue(cfg.color["tray_white_balance"])
        self.assertEqual((cfg.color["wb_gain_min"], cfg.color["wb_gain_max"],
                          cfg.color["wb_min_pixels"]), (0.8, 1.2, 333))
        self.assertTrue(det.wb_enabled)
        self.assertEqual((det.wb_gain_min, det.wb_gain_max, det.wb_min_pixels), (0.8, 1.2, 333))
        self.assertEqual(det.wb_info()["min_pixels"], 333)
        with unittest.mock.patch.dict(os.environ, dict(env, IS_TRAY_WB="0")):
            cfg_off = load_vision_config()
        self.assertFalse(cfg_off.color["tray_white_balance"])
        self.assertEqual(cfg_off.color["wb_min_pixels"], 333)   # 限幅类覆盖不受开关影响

    # ---------------------------------------------------------------- 关闭 = 零影响
    def test_disabled_is_identical_to_baseline(self):
        """无 color 段 (A5 行为) 与显式 tray_white_balance=false 的 scan() 输出逐字段相同。"""
        svc_a, _det_a = self._service(color=None)
        svc_b, _det_b = self._service(color={"tray_white_balance": False})
        ts_a = svc_a.scan(n_frames=2, skip_frames=0)[0]
        ts_b = svc_b.scan(n_frames=2, skip_frames=0)[0]
        self.assertTrue(ts_a and ts_b)
        self.assertEqual(self._json(ts_a), self._json(ts_b))
        for t in list(ts_a) + list(ts_b):
            self.assertNotIn("wb_reason", t.diagnostics)
            self.assertNotIn("wb_gain", t.diagnostics)

    def test_enabled_normalises_only_the_colour_vote(self):
        """打开后: 颜色由蓝变白 (category 第二段跟着变), 但送模型的帧逐像素不变。"""
        svc, det = self._service(color={"tray_white_balance": True})
        seen = []
        inner = det._infer
        det._infer = lambda frame: (seen.append(np.array(frame, copy=True)), inner(frame))[1]
        ts = svc.scan(n_frames=2, skip_frames=0)[0]
        self.assertTrue(ts)
        self.assertEqual(ts[0].color, "white")
        self.assertEqual(ts[0].category, "%s|white|" % self.SHAPE)
        self.assertEqual(ts[0].diagnostics.get("wb_reason"), "ok")
        self.assertGreater(ts[0].diagnostics.get("wb_pixels", 0), 200)
        self.assertTrue(seen)
        for f in seen:
            self.assertTrue(np.array_equal(f, self._frame()))

    # ---------------------------------------------------------------- 增益计算
    def test_gain_from_tray_white_patch(self):
        det = self._det(color={"tray_white_balance": True})
        f = self._frame()
        det._update_tray_wb(f, [self.BOX])
        info = det.wb_info()
        self.assertEqual(info["reason"], "ok")
        self.assertGreater(info["pixels"], det.wb_min_pixels)
        for got, exp in zip(info["gain"], self.EXPECT_GAIN):
            self.assertAlmostEqual(got, exp, places=2)
        self.assertEqual(info["means"], [220.0, 205.0, 190.0])
        det2 = self._det()                       # 关着: 不取样、不写增益
        det2._update_tray_wb(f, [self.BOX])
        self.assertEqual(det2.wb_info()["reason"], "disabled")
        self.assertIsNone(det2.wb_info()["gain"])

    def test_vote_flips_only_when_enabled(self):
        det = self._det(color={"tray_white_balance": True})
        f = self._frame()
        self.assertIn(det._vote_color(f, self.BOX[0], self.BOX[1], self.BOX[2], self.BOX[3], None),
                      self.BLUE)             # 未归一化: 偏色的"白"被算成蓝
        det._update_tray_wb(f, [self.BOX])
        self.assertEqual(det._vote_color(f, self.BOX[0], self.BOX[1], self.BOX[2], self.BOX[3], None),
                         "white")
        self.assertTrue(np.array_equal(f, self._frame()))    # 原帧没被就地修改

    def test_too_few_pixels_keeps_previous_gain(self):
        det = self._det(color={"tray_white_balance": True})
        f = self._frame()
        det._update_tray_wb(f, [self.BOX])
        first = list(det.wb_info()["gain"])
        self.assertEqual(det.wb_info()["reason"], "ok")
        det.wb_min_pixels = 10 ** 6
        det._update_tray_wb(f, [self.BOX])
        info = det.wb_info()
        self.assertEqual(info["reason"], "too_few_pixels")
        self.assertEqual(info["gain"], first)                # 沿用上次增益
        self.assertEqual(det._vote_color(f, self.BOX[0], self.BOX[1], self.BOX[2], self.BOX[3], None),
                         "white")

    def test_missing_tray_region_skips(self):
        det = self._det(color={"tray_white_balance": True}, roi=[])
        det.calibrator = None                                # 既无托盘内框也无 ROI
        f = self._frame()
        det._update_tray_wb(f, [self.BOX])
        info = det.wb_info()
        self.assertEqual(info["reason"], "no_tray")
        self.assertIsNone(info["gain"])
        self.assertIn(det._vote_color(f, self.BOX[0], self.BOX[1], self.BOX[2], self.BOX[3], None),
                      self.BLUE)

    def test_gain_is_clamped_to_config_limits(self):
        det = self._det(color={"tray_white_balance": True},
                        hsv=dict(self.HSV, white_s_max=250, white_v_min=100))
        f = np.zeros((480, 640, 3), np.uint8)
        f[:, :] = (20, 200, 255)                             # 极端偏色: B 很暗 -> 增益要撞上限
        f[self.BOX[1]:self.BOX[3], self.BOX[0]:self.BOX[2]] = self.OBJ
        det._update_tray_wb(f, [self.BOX])
        gain = det.wb_info()["gain"]
        self.assertEqual(det.wb_info()["reason"], "ok")
        self.assertAlmostEqual(gain[0], det.wb_gain_max, places=3)
        self.assertAlmostEqual(gain[2], det.wb_gain_min, places=3)
        for g in gain:
            self.assertGreaterEqual(g, det.wb_gain_min)
            self.assertLessEqual(g, det.wb_gain_max)

    def test_gain_limits_are_validated_in_config(self):
        _tmp, _meta, cfg, _calib = self._files("a6_wb_cfg_",
                                               color={"wb_gain_min": 1.9, "wb_gain_max": 1.4})
        self.assertTrue(any("wb_gain_min" in w for w in cfg.warnings))

    def test_roi_is_used_when_no_tray(self):
        det = self._det(color={"tray_white_balance": True})
        self.assertIsNotNone(det._tray_region())             # detect.roi 兜底
        det.set_roi([])
        self.assertIsNone(det._tray_region())


if __name__ == "__main__":
    unittest.main()
