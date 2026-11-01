"""
视觉模块单元测试
"""

import unittest
import time
import numpy as np
from MessegeCenter import MessageBus
from vision.calibration import CoordinateCalibrator
from vision.camera import SyntheticCamera
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


if __name__ == "__main__":
    unittest.main()
