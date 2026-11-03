# -*- coding: utf-8 -*-
"""
A2 (发送前越界拦截) + A3 (自检门禁) 单元测试。

覆盖 (计划 §A2/§A3 与验收 a~e):
  A2:
    - 闭区间边界: 恰好等于 x_min/x_max/y_min/y_max 放行, 越出一点点就拦;
    - limits=None / tray_margin_mm=None -> 不拦截, 但 detail 里如实写"未配置";
    - verdict.detail 必带三套坐标 (像素 u,v / 托盘 t_x,t_y / 机械臂 x_mm,y_mm) 与越界轴;
    - tray_margin_action = warn|drop 的行为差异;
    - scan() 返回里不含越界目标, 且 last_scan_rejects 有记录、保留目标带 diagnostics;
    - 拦截事件文字与 UI 的"空盘"事件可区分 (D2);
    - webui /comm 的 MOVE 越界时拒绝发送 (send_move 根本没被调用)。
  A3:
    - ref_points_fp: 四角/内参/分辨率/fixed_z_mm/axes/origin_mm 六类输入任一变化 -> 过期;
    - 每条门禁可单独 off|warn|fail; 缺省级别随 race_mode() (锁文件) 变化; IS_GATE_<ID> 可覆盖;
    - 比赛模式 + fail 级门禁 -> service.start() 抛异常 (进程非零退出), 无锁只提示放行;
    - self_check() 报出现场那 6 项且绝不显示"问题=无";
    - 全达标配置 (放临时目录, 不动仓库现场文件) -> 放行。

跑法: python3 -m unittest vision.test_vision_gates
锁文件: 一律用 IS_COMPETITION_LOCK 指到临时路径, 绝不碰真的 /tmp/is_competition.lock。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import tempfile
import unittest

import numpy as np

from MessegeCenter import MessageBus
from MessegeCenter.topics import VisionTarget

from vision import gates as G
from vision import limits as L
from vision.calibration import CoordinateCalibrator
from vision.service import VisionService


# ---------------------------------------------------------------------------- 工具
_LOCK_ENV = "IS_COMPETITION_LOCK"
_IS_PREFIXES = ("IS_CALIB_CONFIG", "IS_VISION_CONFIG", "IS_VISION_CLASSES", "IS_CLASSES_JSON",
                "IS_ROBOT_LIMITS", "IS_TRAY_MARGIN_MM", "IS_TRAY_MARGIN_ACTION",
                "IS_CAM_TYPE", "IS_CAM_EXPOSURE", "IS_CAM_GAIN", "IS_CAM_WB", "IS_CAM_W",
                "IS_CAM_H", "IS_CAM_DEVICE", "IS_CAM_FPS", "IS_CAM_FOURCC", "IS_CAM_SERIAL")

CORNERS = [[74.6, 87.1], [403.7, 6.9], [629.4, 243.4], [65.4, 411.9]]
CAM_MATRIX = [[444.0, 0.0, 320.0], [0.0, 444.0, 240.0], [0.0, 0.0, 1.0]]
DIST = [0.0, 0.0, 0.0, 0.0, 0.0]
# 与 axes=["tray_y","-tray_x"] + origin_mm=[0,190] 完全一致的 3 个参考点 (拟合误差=0)
REF_CONSISTENT = [{"tray_mm": [0, 0], "robot_mm": [0, 190], "src": "manual"},
                  {"tray_mm": [160, 0], "robot_mm": [0, 30], "src": "manual"},
                  {"tray_mm": [0, 160], "robot_mm": [160, 190], "src": "manual"}]
ROBOT_LIMITS = {"x_min": 0.0, "x_max": 240.0, "y_min": 0.0, "y_max": 240.0}
CAMERA_FP = {"exposure_us": 8000, "gain_db": 6.0, "wb_kelvin": 4600,
             "width": 640, "height": 480, "device": 0, "fourcc": "MJPG", "swap_rb": False}


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def base_calib(**over):
    """现场同款标定 (四角/axes/origin/分辨率/cam_height) + 一致参考点。"""
    d = {
        "tray": {
            "fixed_px": {"corners": copy.deepcopy(CORNERS), "image_size": [640, 480]},
            "enabled": True, "inner_w_mm": 160.0, "inner_h_mm": 160.0, "rim_mm": 0.0,
            "rim_height_mm": 15.0, "fixed_z_mm": 15.0, "axes": ["tray_y", "-tray_x"],
            "ref_points": copy.deepcopy(REF_CONSISTENT), "origin_mm": [0.0, 190.0],
            "focal_px": 444.0, "perspective": "auto",
        },
        "image_size": [640, 480],
        "cam_height_mm": 148.0,
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k] = dict(d[k], **v)
        else:
            d[k] = v
    return d


def base_classes(heights=None, imaging_fp=None):
    names = ["红色半球", "蓝色四面体", "绿色圆柱", "黄色立方"]
    cls = []
    for i, n in enumerate(names):
        h = 30 if heights is None else heights[i]
        cls.append({"id": i, "name": n, "shape_cn": n[2:], "color": None, "height_mm": h})
    hsv = {"hue_bins": [["red", 0, 10], ["red", 170, 179], ["blue", 95, 130]]}
    if imaging_fp is not None:
        hsv["imaging_fp"] = imaging_fp
    return {"color_names": {"red": "红色", "blue": "蓝色", "green": "绿色", "yellow": "黄色"},
            "classes": cls, "hsv": hsv}


def build_full_pass(root):
    """验收 c 的"全达标"三件套: 一致参考点 + 内参 + 限位 + 锁定成像 + 参考点指纹对上。"""
    cam = dict(CAMERA_FP)
    fp = G.imaging_fp(cam)
    vcfg = {"camera": dict(cam), "color": {"source": "hsv", "imaging_fp": fp},
            "record": {"enabled": False}}
    classes = base_classes(heights=[20, 24, 28, 32], imaging_fp=fp)
    calib = base_calib(camera_matrix=CAM_MATRIX, dist_coeffs=DIST,
                       robot_limits_mm=dict(ROBOT_LIMITS),
                       tray_margin_mm=None, tray_margin_action="warn",
                       gates={}, gate_thresholds={})
    calib["ref_points_fp"] = G.ref_points_fp(calib)
    calib["ref_points_verified_at"] = "2026-11-03 00:00:00"
    return (write_json(os.path.join(root, "calib_config.json"), calib),
            write_json(os.path.join(root, "vision_config.json"), vcfg),
            write_json(os.path.join(root, "classes.json"), classes))


class _EnvGuard(unittest.TestCase):
    """每个测试都把 IS_* / IS_COMPETITION_LOCK 清干净并复原, 测试互不串味。"""

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in
                       (_LOCK_ENV,) + _IS_PREFIXES + tuple(k for k in os.environ if k.startswith("IS_GATE_"))}
        self.tmp = tempfile.mkdtemp(prefix="vis_gate_ut_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(self._restore)

    def _restore(self):
        # A3.1: 先把本 fixture 管辖的所有键清掉 (含测试中途新设的), 再复原进来时的值 ——
        # 否则测试里新设的 IS_ROBOT_LIMITS 之类会泄漏给后面的测试。
        for k in list(os.environ):
            if k.startswith("IS_GATE_"):
                os.environ.pop(k, None)
        for k in (_LOCK_ENV,) + _IS_PREFIXES:
            os.environ.pop(k, None)
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v


# ---------------------------------------------------------------------------- A2: limits
class LimitCheckTest(_EnvGuard):
    """A2 单一检查函数: 闭区间 / 未配置 / 三套坐标 / warn-drop / 非法值。"""

    def setUp(self):
        super().setUp()
        self.lim = {"x_min": 0.0, "x_max": 160.0, "y_min": 0.0, "y_max": 190.0}

    def test_closed_interval_boundaries_pass(self):
        for x, y in ((0.0, 50.0), (160.0, 50.0), (50.0, 0.0), (50.0, 190.0),
                     (0.0, 0.0), (160.0, 190.0), (80.0, 95.0)):
            v = L.check_point(x, y, limits=self.lim)
            self.assertTrue(v.ok, "闭区间边界 (%s,%s) 应放行: %s" % (x, y, v.reason))
            self.assertEqual(v.trigger, "")

    def test_outside_blocked_with_axis(self):
        v = L.check_point(160.1, 50.0, limits=self.lim)
        self.assertFalse(v.ok)
        self.assertEqual(v.trigger, L.TRIGGER_ROBOT)
        self.assertEqual(v.axes, ["x"])
        self.assertIn("160.1", v.reason)
        v2 = L.check_point(50.0, -0.01, limits=self.lim)
        self.assertFalse(v2.ok)
        self.assertEqual(v2.axes, ["y"])
        v3 = L.check_point(999.0, 999.0, limits=self.lim)
        self.assertEqual(v3.axes, ["x", "y"])

    def test_unconfigured_never_blocks_but_reports(self):
        v = L.check_point(1e6, -1e6, limits=None)
        self.assertTrue(v.ok)
        self.assertFalse(v.detail["robot_limits_configured"])
        self.assertIn("未配置", v.detail["robot_limits_note"])
        vt = L.check_point(10.0, 10.0, limits=self.lim, tray_xy=(999.0, 999.0),
                           tray_size=(160.0, 160.0), tray_margin_mm=None)
        self.assertTrue(vt.ok)
        self.assertIn("未配置", vt.detail["tray_margin_note"])

    def test_detail_has_three_coordinate_sets(self):
        v = L.check_point(170.0, 20.0, limits=self.lim, tray_margin_mm=None,
                          pixel_uv=(12.5, 34.5), tray_xy=(170.0, 20.0), source="ut")
        d = v.detail
        self.assertEqual(d["robot_xy"], [170.0, 20.0])
        self.assertEqual(d["tray_xy"], [170.0, 20.0])
        self.assertEqual(list(d["pixel_uv"]), [12.5, 34.5])
        txt = v.text()
        for part in ("像素(12.5,34.5)", "托盘(170.0,20.0)", "机械臂(170.0,20.0)"):
            self.assertIn(part, txt)

    def test_warn_vs_drop(self):
        # 机械臂限位放到很宽, 只留托盘边距这一项, 才看得到 warn / drop 的差别
        wide = {"x_min": -1000.0, "x_max": 1000.0, "y_min": -1000.0, "y_max": 1000.0}
        kw = dict(limits=wide, tray_margin_mm=0.0, tray_xy=(200.0, 50.0),
                  tray_size=(160.0, 160.0), pixel_uv=(10, 20))
        w = L.check_point(200.0, 50.0, tray_margin_action="warn", **kw)
        d = L.check_point(200.0, 50.0, tray_margin_action="drop", **kw)
        self.assertTrue(w.ok, "warn 不该丢件: %s" % w.reason)
        self.assertTrue(w.warnings, "warn 应该留一条提示")
        self.assertFalse(d.ok, "drop 应该拦截")
        self.assertEqual(d.trigger, L.TRIGGER_TRAY)
        self.assertTrue([a for a in d.axes if a in ("t_x", "t_y")])
        # 托盘内 -> 两种 action 都放行
        inside = dict(limits=wide, tray_margin_mm=0.0, tray_xy=(80.0, 80.0),
                      tray_size=(160.0, 160.0), pixel_uv=(10, 20))
        self.assertTrue(L.check_point(80.0, 80.0, tray_margin_action="drop", **inside).ok)

    def test_invalid_coord_rejected(self):
        for bad in (float("nan"), float("inf"), None, "abc"):
            v = L.check_point(bad, 10.0, limits=self.lim)
            self.assertFalse(v.ok, "非法坐标 %r 应拒绝" % (bad,))
            self.assertEqual(v.trigger, L.TRIGGER_INVALID)

    def test_parse_limits_and_env_override(self):
        self.assertIsNone(L.parse_limits(None))
        self.assertIsNone(L.parse_limits({"x_min": 0, "x_max": 1}))            # 缺键
        self.assertIsNone(L.parse_limits({"x_min": 5, "x_max": 1, "y_min": 0, "y_max": 1}))
        self.assertEqual(L.parse_limits({"x_min": 0, "x_max": 1, "y_min": 2, "y_max": 3}),
                         {"x_min": 0.0, "x_max": 1.0, "y_min": 2.0, "y_max": 3.0})
        env = {"IS_ROBOT_LIMITS": "0,10,20,30", "IS_TRAY_MARGIN_MM": "2.5",
               "IS_TRAY_MARGIN_ACTION": "drop"}
        lim, margin, action = L.limits_from_config({"robot_limits_mm": None}, env=env)
        self.assertEqual((lim["x_max"], lim["y_min"]), (10.0, 20.0))
        self.assertEqual(margin, 2.5)
        self.assertEqual(action, "drop")
        # 非法 action 回落 warn
        _l, _m, a2 = L.limits_from_config({}, env={"IS_TRAY_MARGIN_ACTION": "explode"})
        self.assertEqual(a2, "warn")

    def test_reject_event_is_not_empty_tray(self):
        t = VisionTarget(target_id=3, label="红色半球", class_name="红色半球",
                         u_pixel=100.0, v_pixel=120.0, x_mm=300.0, y_mm=300.0)
        v = L.check_point(300.0, 300.0, limits=self.lim, pixel_uv=(100.0, 120.0),
                          tray_xy=(300.0, 300.0))
        line = L.format_reject(t, v)
        self.assertTrue(line.startswith("视觉限位拦截:"))
        for part in ("目标#3", "像素(100.0,120.0)", "托盘(300.0,300.0)", "机械臂(300.0,300.0)",
                     "触发=robot_limits"):
            self.assertIn(part, line)
        self.assertNotIn("托盘无可识别货物", line)          # UI 空盘事件文字
        all_line = L.format_rejected_all(2, [])
        self.assertIn("视觉限位拦截", all_line)
        self.assertIn("不是空盘", all_line)


# ---------------------------------------------------------------------------- A2: scan 过滤
class _FakeGrabber:
    def __init__(self, frame):
        self.frame = frame
        self.last_error = ""
        self.connected = True
        self.fps = 30.0
        self.camera = None
        self.reconnects = 0

    def latest(self):
        return self.frame, 1.0

    def fresh_frames(self, n, timeout_s, skip=0):
        return [self.frame.copy() for _ in range(max(1, int(n)))]

    def stop(self):
        pass


class _FakeDetector:
    def __init__(self, targets):
        self.targets = list(targets)
        self.rejected = {}
        self.roi = None

    def detect_multi(self, frames, match_px=25.0, min_hit_ratio=0.5):
        return list(self.targets), frames[-1]

    def detect(self, frame):
        return list(self.targets)

    def annotate(self, frame, targets):
        return frame.copy()


class ScanLimitFilterTest(_EnvGuard):
    """A2: scan() 返回前把越界目标剔掉, 返回结构不变, 证据留在 last_scan_rejects。"""

    def _service(self, targets, limits=None):
        svc = VisionService(MessageBus(), detector_factory=lambda cal: _FakeDetector(targets),
                            continuous_detect=False)
        self.addCleanup(svc.stop)
        svc.calibrator.robot_limits_mm = limits
        svc.calibrator.tray_margin_mm = None
        svc._grabber = _FakeGrabber(np.zeros((480, 640, 3), np.uint8))
        return svc

    @staticmethod
    def _t(u, v, x, y, label="red"):
        return VisionTarget(target_id=0, label=label, class_name=label, confidence=0.9,
                            u_pixel=u, v_pixel=v, x_mm=x, y_mm=y, bbox=(u - 10, v - 10, u + 10, v + 10))

    def test_scan_drops_out_of_range_and_keeps_rest(self):
        inside, outside = self._t(300.0, 200.0, 100.0, 100.0), self._t(320.0, 240.0, 999.0, 999.0)
        lim = {"x_min": 0.0, "x_max": 200.0, "y_min": 0.0, "y_max": 200.0}
        svc = self._service([inside, outside], limits=lim)
        targets, _ref = svc.scan(n_frames=2, skip_frames=0)
        self.assertEqual([t.label for t in targets], ["red"])
        self.assertEqual(targets[0].target_id, 1)
        self.assertAlmostEqual(targets[0].x_mm, 100.0)
        self.assertNotIn(999.0, [t.x_mm for t in targets])
        self.assertEqual(len(svc.last_scan_rejects), 1)
        rec = svc.last_scan_rejects[0]
        self.assertEqual(rec["trigger"], L.TRIGGER_ROBOT)
        self.assertEqual(rec["x_mm"], 999.0)
        self.assertEqual(sorted(rec["axes"]), ["x", "y"])
        self.assertEqual(svc.last_scan["limit_rejected"], 1)
        self.assertEqual(svc.last_scan["targets"], 1)
        self.assertTrue(targets[0].diagnostics.get("limit_checked"))
        self.assertTrue(targets[0].diagnostics.get("limit_ok"))

    def test_scan_closed_boundary_target_is_kept(self):
        edge = self._t(300.0, 200.0, 200.0, 200.0)
        lim = {"x_min": 0.0, "x_max": 200.0, "y_min": 0.0, "y_max": 200.0}
        svc = self._service([edge], limits=lim)
        targets, _ = svc.scan(n_frames=2, skip_frames=0)
        self.assertEqual(len(targets), 1)
        self.assertEqual(svc.last_scan_rejects, [])

    def test_scan_without_limits_behaves_like_baseline(self):
        a, b = self._t(300.0, 200.0, 999.0, 999.0), self._t(320.0, 240.0, -999.0, -999.0)
        svc = self._service([a, b], limits=None)
        targets, _ = svc.scan(n_frames=2, skip_frames=0)
        self.assertEqual(len(targets), 2)
        self.assertEqual(svc.last_scan_rejects, [])
        self.assertEqual(svc.last_scan["limit_rejected"], 0)

    def test_scan_all_rejected_logs_limit_event_not_empty_tray(self):
        a, b = self._t(300.0, 200.0, 999.0, 999.0), self._t(320.0, 240.0, 888.0, 888.0)
        lim = {"x_min": 0.0, "x_max": 10.0, "y_min": 0.0, "y_max": 10.0}
        svc = self._service([a, b], limits=lim)
        with self.assertLogs("vision.service", level=logging.WARNING) as cm:
            targets, _ = svc.scan(n_frames=2, skip_frames=0)
        log = "\n".join(cm.output)
        self.assertEqual(targets, [])
        self.assertIn("视觉限位拦截", log)
        self.assertIn("不是空盘", log)
        self.assertNotIn("托盘无可识别货物", log)
        self.assertEqual(len(svc.last_scan_rejects), 2)

    def test_calibrator_check_target_and_check_robot_xy(self):
        cal = CoordinateCalibrator()
        cal.robot_limits_mm = {"x_min": 0.0, "x_max": 10.0, "y_min": 0.0, "y_max": 10.0}
        cal.tray_margin_mm = None
        self.assertFalse(cal.check_robot_xy(500.0, 500.0).ok)
        self.assertTrue(cal.check_robot_xy(5.0, 5.0).ok)
        t = self._t(300.0, 200.0, 5.0, 5.0)
        self.assertTrue(cal.check_target(t).ok)
        t2 = self._t(300.0, 200.0, 500.0, 500.0)
        v = cal.check_target(t2)
        self.assertFalse(v.ok)
        # 三套坐标在 scan 侧也要齐 (像素来自目标, 托盘坐标由标定器换算)
        self.assertEqual(list(v.detail["robot_xy"]), [500.0, 500.0])
        self.assertEqual(list(v.detail["pixel_uv"]), [300.0, 200.0])
        self.assertIsNotNone(v.detail["tray_xy"])


# ---------------------------------------------------------------------------- A2: /comm MOVE
class _StubCommClient:
    is_connected = True

    def __init__(self):
        self.moves = []

    def send_move(self, tid, x, y, timeout_ms=10000, wait_ack=False):
        self.moves.append((tid, float(x), float(y)))
        return True


class CommMoveLimitTest(_EnvGuard):
    """A2: webui /comm 的 MOVE 在越界时拒绝发送 (协议不变, 帧根本没发出去)。"""

    def _dbg(self, limits):
        from webui.comm_debug import CommDebug
        cal = CoordinateCalibrator()
        cal.robot_limits_mm = limits
        cal.tray_margin_mm = None
        client = _StubCommClient()
        return CommDebug(client=client, calibrator=cal), client

    def test_out_of_range_move_is_not_sent(self):
        dbg, client = self._dbg({"x_min": 0.0, "x_max": 10.0, "y_min": 0.0, "y_max": 10.0})
        r = dbg.send({"cmd": "move", "tid": 0, "x": 900.0, "y": 900.0})
        self.assertFalse(r["ok"])
        self.assertIn("越界", r["error"])
        self.assertEqual(client.moves, [], "越界坐标竟然发给了下位机!")
        self.assertEqual(r["limit"]["trigger"], L.TRIGGER_ROBOT)

    def test_in_range_move_is_sent(self):
        dbg, client = self._dbg({"x_min": 0.0, "x_max": 10.0, "y_min": 0.0, "y_max": 10.0})
        r = dbg.send({"cmd": "move", "tid": 0, "x": 5.0, "y": 6.0})
        self.assertTrue(r["ok"])
        self.assertEqual(client.moves, [(0, 5.0, 6.0)])

    def test_unconfigured_limits_keep_baseline(self):
        dbg, client = self._dbg(None)
        r = dbg.send({"cmd": "move", "tid": 0, "x": 900.0, "y": 900.0})
        self.assertTrue(r["ok"])
        self.assertEqual(len(client.moves), 1)


# ---------------------------------------------------------------------------- A3: 指纹
class RefPointsFingerprintTest(_EnvGuard):
    """A3 (D1): 指纹只比指纹不比时间; 六类输入任一变化 -> 参考点判过期。"""

    def _base(self):
        return base_calib(camera_matrix=copy.deepcopy(CAM_MATRIX), dist_coeffs=list(DIST))

    def test_six_inputs_each_change_fingerprint(self):
        base = self._base()
        fp0 = G.ref_points_fp(base)
        self.assertEqual(len(fp0), 12)

        def mut(fn):
            d = copy.deepcopy(base)
            fn(d)
            return d

        cases = {
            "corners": lambda d: d["tray"]["fixed_px"]["corners"][0].__setitem__(0, 75.6),
            "camera_matrix": lambda d: d["camera_matrix"][0].__setitem__(0, 445.0),
            "dist_coeffs": lambda d: d["dist_coeffs"].__setitem__(0, -0.01),
            "image_size": lambda d: d["tray"]["fixed_px"].__setitem__("image_size", [1280, 960]),
            "fixed_z_mm": lambda d: d["tray"].__setitem__("fixed_z_mm", 16.0),
            "axes": lambda d: d["tray"].__setitem__("axes", ["tray_y", "tray_x"]),
            "origin_mm": lambda d: d["tray"].__setitem__("origin_mm", [0.0, 191.0]),
        }
        for name, fn in cases.items():
            self.assertNotEqual(G.ref_points_fp(mut(fn)), fp0, "%s 改了指纹却没变" % name)
        # 与时间无关: 只加 ref_points_verified_at 不改指纹
        d2 = copy.deepcopy(base)
        d2["ref_points_verified_at"] = "2030-01-01 00:00:00"
        self.assertEqual(G.ref_points_fp(d2), fp0)

    def test_gate_ref_points_missing_fp_then_sealed_then_expired(self):
        def fresh_base():
            d = base_calib(camera_matrix=copy.deepcopy(CAM_MATRIX), dist_coeffs=list(DIST))
            d["ref_points_fp"] = G.ref_points_fp(d)
            d["ref_points_verified_at"] = "2026-11-03 00:00:00"
            return d

        cpath = write_json(os.path.join(self.tmp, "calib_config.json"), base_calib(
            camera_matrix=copy.deepcopy(CAM_MATRIX), dist_coeffs=list(DIST)))
        vpath = write_json(os.path.join(self.tmp, "vision_config.json"),
                           {"camera": dict(CAMERA_FP), "record": {"enabled": False}})
        kpath = write_json(os.path.join(self.tmp, "classes.json"), base_classes(imaging_fp="x"))

        def ref_item():
            ctx = G.build_context(calib_path=cpath, vision_config_path=vpath,
                                  classes_path=kpath, env={})
            rep = G.evaluate(ctx, race=False, gates_cfg={"ref_points": "fail"})
            return {i.id: i for i in rep.items}["ref_points"]

        # 1) 有参考点但缺 ref_points_fp -> 按未标定处理 (fail 级被触发)
        it = ref_item()
        self.assertEqual(it.level, "fail")
        self.assertIn("ref_points_fp", it.text)

        # 2) 盖章 -> 通过
        write_json(cpath, fresh_base())
        self.assertEqual(ref_item().level, "ok", ref_item().text)

        # 3) 改四角/内参/分辨率/fixed_z/axes/origin 任一项 -> 过期
        cases = (("corners", lambda d: d["tray"]["fixed_px"]["corners"][1].__setitem__(1, 7.9)),
                 ("camera_matrix", lambda d: d["camera_matrix"][1].__setitem__(1, 445.0)),
                 ("dist_coeffs", lambda d: d["dist_coeffs"].__setitem__(1, 0.02)),
                 ("image_size", lambda d: d["tray"]["fixed_px"].__setitem__("image_size", [800, 600])),
                 ("fixed_z_mm", lambda d: d["tray"].__setitem__("fixed_z_mm", 16.0)),
                 ("axes", lambda d: d["tray"].__setitem__("axes", ["-tray_y", "-tray_x"])),
                 ("origin_mm", lambda d: d["tray"].__setitem__("origin_mm", [1.0, 190.0])))
        for name, mutate in cases:
            write_json(cpath, fresh_base())          # 每轮都从"已盖章且达标"开始
            self.assertEqual(ref_item().level, "ok", name)
            d = json.load(open(cpath, encoding="utf-8"))
            mutate(d)
            write_json(cpath, d)
            it = ref_item()
            self.assertEqual(it.level, "fail", "%s 改了却没判过期" % name)
            self.assertIn("过期", it.text, name)

    def test_imaging_fp_is_deterministic_and_camera_sensitive(self):
        fp = G.imaging_fp(dict(CAMERA_FP))
        self.assertEqual(fp, G.imaging_fp(dict(CAMERA_FP)))
        self.assertEqual(len(fp), 12)
        other = dict(CAMERA_FP)
        other["exposure_us"] = 8001
        self.assertNotEqual(G.imaging_fp(other), fp)


# ---------------------------------------------------------------------------- A3: 级别矩阵
class GateLevelMatrixTest(_EnvGuard):
    """A3 (d): 每条门禁可单独 off|warn|fail; 缺省随 race 变化; IS_GATE_<ID> 可覆盖。"""

    def test_defaults_debug_vs_race(self):
        dbg = G.resolve_levels({}, race=False, env={})
        race = G.resolve_levels({}, race=True, env={})
        self.assertEqual(sorted(dbg), sorted(G.GATE_IDS))
        for gid in G.GATE_IDS:
            self.assertEqual(dbg[gid], G.GATE_DEFAULT[gid][0], gid)
            self.assertEqual(race[gid], G.GATE_DEFAULT[gid][1], gid)

    def test_each_gate_individually_off_warn_fail(self):
        for gid in G.GATE_IDS:
            for lvl in ("off", "warn", "fail"):
                lv = G.resolve_levels({gid: lvl}, race=False, env={})
                self.assertEqual(lv[gid], lvl, "%s=%s 没生效" % (gid, lvl))
                # 只动这一条, 别的保持调试缺省
                for other in G.GATE_IDS:
                    if other != gid:
                        self.assertEqual(lv[other], G.GATE_DEFAULT[other][0], other)

    def test_env_override_wins(self):
        lv = G.resolve_levels({"ref_points": "warn"}, race=False,
                              env={"IS_GATE_REF_POINTS": "fail", "IS_GATE_POSE": "off"})
        self.assertEqual(lv["ref_points"], "fail")
        self.assertEqual(lv["pose"], "off")
        # 非法值 -> 回落缺省
        self.assertEqual(G.resolve_levels({}, race=False, env={"IS_GATE_POSE": "nope"})["pose"],
                         G.GATE_DEFAULT["pose"][0])

    def test_gates_cfg_from_calib_file_flows_into_report(self):
        calib = base_calib(camera_matrix=copy.deepcopy(CAM_MATRIX), dist_coeffs=list(DIST),
                           gates={"imaging_locked": "fail", "color_imaging": "off"})
        cpath = write_json(os.path.join(self.tmp, "calib_config.json"), calib)
        vpath = write_json(os.path.join(self.tmp, "vision_config.json"), {"camera": {}})
        kpath = write_json(os.path.join(self.tmp, "classes.json"), base_classes())
        ctx = G.build_context(calib_path=cpath, vision_config_path=vpath, classes_path=kpath, env={})
        rep = G.evaluate(ctx, race=False)
        items = {i.id: i for i in rep.items}
        self.assertEqual(len(rep.items), 11)
        self.assertEqual(items["imaging_locked"].level, "fail")
        self.assertEqual(items["color_imaging"].level, "off")
        self.assertIn("imaging_locked", [i.id for i in rep.fails])

    def test_self_check_merges_gates_and_never_says_none(self):
        svc = VisionService(MessageBus(), continuous_detect=False)
        rep = svc.self_check(log=False)
        joined = " | ".join(rep["problems"])
        self.assertTrue(rep["problems"], "自检不能一条问题都没有")
        self.assertNotEqual(joined.strip(), "无")
        for key in ("参考点", "颜色", "类别高度", "成像", "内参", "限位"):
            self.assertIn(key, joined, "现场那 6 项里缺: %s" % key)
        self.assertIn("gates", rep)
        self.assertEqual(len(rep["gates"]["items"]), 11)
        self.assertTrue(rep["gates"]["fails"] or rep["gates"]["warns"])
        # gate_report() 是只读接口
        gr = svc.gate_report()
        self.assertEqual(len(gr.items), 11)
        self.assertTrue(gr.fails, "现场配置应至少有 fail 级门禁项")

    # ---- A3.1: robot_limits 必须与运行期 (limits.py) 用同一个"有效限位来源" ----
    def _robot_limits_item(self, calib, race=False):
        """离线跑一次门禁, 取 robot_limits 那一项 (不经过 service, 不受现场文件影响)。"""
        cpath = write_json(os.path.join(self.tmp, "calib_config.json"), calib)
        vpath = write_json(os.path.join(self.tmp, "vision_config.json"), {"camera": {}})
        kpath = write_json(os.path.join(self.tmp, "classes.json"), base_classes())
        ctx = G.build_context(calib_path=cpath, vision_config_path=vpath,
                              classes_path=kpath, env={})
        rep = G.evaluate(ctx, race=race)
        return {i.id: i for i in rep.items}["robot_limits"]

    def test_robot_limits_env_only_is_not_reported_as_missing(self):
        """只有 IS_ROBOT_LIMITS (文件里 robot_limits_mm=null) 时不能报"未配置/不会拦截"。

        改前这里会打印"机械臂限位未配置 ... 发送前不会拦截越界坐标", 而同一进程里
        limits.py 真的会拦 (verifier 报告 #10 的 P2 假陈述)。
        """
        os.environ["IS_ROBOT_LIMITS"] = "100,121,0,200"
        it = self._robot_limits_item(base_calib())
        self.assertIn("IS_ROBOT_LIMITS", it.text, "文案必须点明限位来自环境变量")
        self.assertNotIn("未配置", it.text)
        self.assertNotIn("不会拦截", it.text)
        self.assertFalse(it.blocks)
        self.assertIn(it.level, ("ok", "warn"))
        self.assertEqual(it.detail["source"], "env")
        self.assertEqual(it.detail["parsed"],
                         {"x_min": 100.0, "x_max": 121.0, "y_min": 0.0, "y_max": 200.0})
        self.assertIsNone(it.detail["file_limits"])

    def test_robot_limits_env_overrides_file_like_limits_py(self):
        """文件与 env 同时存在时, 门禁的结论必须跟 limits.py 完全一致 (env > 文件)。"""
        os.environ["IS_ROBOT_LIMITS"] = "100,121,0,200"
        it = self._robot_limits_item(base_calib(
            robot_limits_mm={"x_min": 0, "x_max": 160, "y_min": 0, "y_max": 190}))
        self.assertFalse(it.blocks)
        self.assertEqual(it.detail["source"], "env")
        self.assertEqual(it.detail["parsed"]["x_max"], 121.0)
        self.assertEqual(it.detail["file_limits"]["x_max"], 160.0)
        self.assertIn("覆盖", it.text)
        # 与运行期同口径: 121.0 之内的点放行, 文件里的 160.0 不再被当作上限
        eff = it.detail["parsed"]
        self.assertTrue(L.check_point(120.0, 50.0, limits=eff).ok)
        self.assertFalse(L.check_point(130.0, 50.0, limits=eff).ok)

    def test_robot_limits_file_only_and_broken_env_falls_back(self):
        """只文件 -> source=file 且不提 env; env 写坏 -> limits.py 忽略它, 回落文件。"""
        it = self._robot_limits_item(base_calib(
            robot_limits_mm={"x_min": 0, "x_max": 160, "y_min": 0, "y_max": 190}))
        self.assertFalse(it.blocks)
        self.assertEqual(it.detail["source"], "file")
        self.assertNotIn("IS_ROBOT_LIMITS", it.text)

        os.environ["IS_ROBOT_LIMITS"] = "1,2"          # 不是 4 个数 -> 与 limits.py 一样当没设
        it2 = self._robot_limits_item(base_calib(
            robot_limits_mm={"x_min": 0, "x_max": 160, "y_min": 0, "y_max": 190}))
        self.assertEqual(it2.detail["source"], "file")
        self.assertIsNone(it2.detail["env_limits"])
        self.assertFalse(it2.blocks)

        # 文件与 env 都没有 -> 这时才允许说"未配置", 且要写清两种来源都查了
        # (robot_limits 缺省级别 = warn/调试 warn/比赛 warn, 所以这里断言 warn 而不是 blocks)
        os.environ.pop("IS_ROBOT_LIMITS", None)
        it3 = self._robot_limits_item(base_calib(), race=True)
        self.assertEqual(it3.level, "warn")
        self.assertIn("未配置", it3.text)
        self.assertIn("IS_ROBOT_LIMITS", it3.text)
        self.assertEqual(it3.detail["source"], "none")


# ---------------------------------------------------------------------------- A3: 启动门禁
class GateEnforcementTest(_EnvGuard):
    """A3 (b): 比赛模式 + fail -> start() 抛异常拒绝启动; 无锁(调试)只提示放行。"""

    def _svc(self, **over):
        svc = VisionService(MessageBus(), continuous_detect=False)
        self.addCleanup(svc.stop)
        for k, v in over.items():
            setattr(svc.calibrator, k, v)
        return svc

    def test_race_mode_with_fail_gate_refuses_start(self):
        lock = os.path.join(self.tmp, "run.lock")
        open(lock, "w").close()
        os.environ[_LOCK_ENV] = lock
        svc = self._svc()
        self.assertTrue(svc.gate_report().race)
        self.assertTrue(svc.gate_report().fails, "现场配置在比赛模式应有 fail 项")
        with self.assertRaises(RuntimeError) as cm:
            svc.start()
        self.assertIn("拒绝启动", str(cm.exception))
        self.assertFalse(svc.is_running)

    def test_no_lock_is_debug_mode(self):
        os.environ[_LOCK_ENV] = os.path.join(self.tmp, "absent.lock")
        svc = self._svc()
        rep = svc._enforce_gates()
        self.assertIsNotNone(rep)
        self.assertFalse(rep.race)

    def test_full_pass_config_passes_and_starts_gate_check(self):
        cpath, vpath, kpath = build_full_pass(self.tmp)
        lock = os.path.join(self.tmp, "race.lock")
        open(lock, "w").close()
        os.environ[_LOCK_ENV] = lock
        os.environ["IS_CALIB_CONFIG"] = cpath
        os.environ["IS_VISION_CONFIG"] = vpath
        os.environ["IS_VISION_CLASSES"] = kpath
        svc = VisionService(MessageBus(), continuous_detect=False)
        self.addCleanup(svc.stop)
        rep = svc.gate_report()
        self.assertTrue(rep.race)
        self.assertEqual([i.id for i in rep.fails], [], "全达标配置不该有拦截项: %s"
                         % [(i.id, i.text) for i in rep.fails])
        svc._enforce_gates()          # 不抛 = 放行

    def test_gate_failure_raises_before_touching_camera(self):
        lock = os.path.join(self.tmp, "run2.lock")
        open(lock, "w").close()
        os.environ[_LOCK_ENV] = lock
        svc = self._svc()
        with self.assertRaises(RuntimeError):
            svc.start()
        self.assertIsNone(svc._grabber, "被门禁拒绝时不该去开相机")


if __name__ == "__main__":
    unittest.main(verbosity=2)