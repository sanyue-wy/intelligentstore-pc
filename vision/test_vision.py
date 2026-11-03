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


if __name__ == "__main__":
    unittest.main()
