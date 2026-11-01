"""比赛流程组件单元测试: python3 -m unittest UI.test_ui"""
import os
import tempfile
import time
import unittest

from MessegeCenter.topics import VisionTarget


def T(label="", color="red", shape="cube", conf=0.9, u=100, v=100, bbox=(90, 90, 110, 110)):
    return VisionTarget(target_id=1, color=color, shape=shape, confidence=conf, u_pixel=u, v_pixel=v,
                        x_mm=0, y_mm=0, label=label, category=f"{shape}|{color}|", bbox=bbox)


class TestPlanner(unittest.TestCase):
    def test_slots_capacity_and_names(self):
        from UI.planner import SorterPlanner, display_name
        p = SorterPlanner()
        a = T(color="red")
        self.assertEqual(display_name(a), "红色正方体")
        s = p.slot_for(a)
        for _ in range(5):
            p.mark_done(a)
        self.assertEqual(p.slot_for(T(color="red")), s)
        self.assertEqual(len(p.warnings), 1)          # 超 4 件只告警一次
        self.assertEqual(p.slots_summary()[str(s + 1)]["count"], 5)

    def test_assess(self):
        from UI.planner import SorterPlanner
        p = SorterPlanner()
        a, b = T(conf=0.9), T(conf=0.9, u=105, bbox=(95, 90, 115, 110))
        self.assertIn("重叠", p.assess(a, [a, b]))
        self.assertIn("置信度", p.assess(T(conf=0.2), [T(conf=0.2)]))
        c = T(u=300, bbox=(290, 90, 310, 110))
        self.assertIsNone(p.assess(c, [a, c]))
        self.assertIs(p.pick_next([a, b, c]), c)       # 优先抓不重叠的


class TestFailCaps(unittest.TestCase):
    def test_blocked_never_picked(self):
        from UI.planner import SorterPlanner
        p = SorterPlanner()
        a, b = T(u=100), T(u=300, bbox=(290, 90, 310, 110))
        self.assertIs(p.pick_next([a, b], blocked=[(100, 100)]), b)
        self.assertIsNone(p.pick_next([a], blocked=[(102, 101)]))     # 全被跳过 -> None, 不再退回失败件
        self.assertIs(p.pick_next([a], skip=[(100, 100)]), a)          # skip 只是暂避

    def test_spot_counter(self):
        from UI.run_competition import _count_spot_fail
        rec = []
        self.assertEqual(_count_spot_fail(rec, 100, 100, 25), 1)
        self.assertEqual(_count_spot_fail(rec, 110, 105, 25), 2)
        self.assertEqual(_count_spot_fail(rec, 300, 100, 25), 1)

    def test_slot_no_ack_aborts(self):
        from UI.controller import CompetitionController as SortController
        class C:
            calls = 0
            def send_storage_slot(self, s):
                C.calls += 1
                return False
            def execute_sorting_task_ex(self, **kw):
                raise AssertionError("盘没转到位不应抓取")
        ctl = SortController.__new__(SortController)
        ctl.client, ctl._task_id, ctl.timeout_s, ctl.cancel_flag, ctl.last_motion = C(), 0, 12, False, 0
        r = ctl.sort_one(10, 20, 2)
        self.assertFalse(r.success)
        self.assertTrue(r.slot_fail)
        self.assertEqual(C.calls, 2)


class TestTrigger(unittest.TestCase):
    def test_arm_fire_once(self):
        from UI.start_trigger import StartTrigger
        t = StartTrigger("web")
        t.start()
        self.assertFalse(t.fire("web"))               # 未待机不触发
        t.arm()
        time.sleep(0.35)
        self.assertTrue(t.fire("web"))
        self.assertFalse(t.fire("web"))               # 只触发一次
        self.assertTrue(t.wait(timeout=0.5))
        self.assertEqual(t.fired_by, "web")


class TestColorCalib(unittest.TestCase):
    def test_solve(self):
        from vision.color_calib import solve_hsv
        smp = [{"color": "red", "h": 178, "s": 200, "v": 150}, {"color": "red", "h": 2, "s": 210, "v": 160},
               {"color": "yellow", "h": 28, "s": 200, "v": 200}, {"color": "green", "h": 65, "s": 180, "v": 120},
               {"color": "blue", "h": 112, "s": 200, "v": 120}, {"color": "white", "h": 0, "s": 15, "v": 220}]
        hsv = solve_hsv(smp, {})
        def name(h):
            for n, lo, hi in hsv["hue_bins"]:
                if lo <= h <= hi:
                    return n
        self.assertEqual(name(0), "red")
        self.assertEqual(name(175), "red")
        self.assertEqual(name(30), "yellow")
        self.assertEqual(name(70), "green")
        self.assertEqual(name(115), "blue")
        covered = sorted(h for n, lo, hi in hsv["hue_bins"] for h in range(lo, hi + 1))
        self.assertEqual(covered, list(range(180)))     # 0~179 全覆盖且不重叠
        with self.assertRaises(ValueError):
            solve_hsv([{"color": "red", "h": 0, "s": 5, "v": 100}, {"color": "blue", "h": 110, "s": 200, "v": 100}], {})


class TestModelSelect(unittest.TestCase):
    def test_new_pt_prefers_its_own_conversion(self):
        import json
        import vision.yolo_engine as ye
        d = tempfile.mkdtemp()
        old = ye.MODEL_DIR
        ye.MODEL_DIR = d
        try:
            pt = os.path.join(d, "best.pt")
            open(pt, "wb").write(b"new-weights")
            sha = ye.file_sha16(pt)
            for name, s in (("old.onnx", "deadbeef"), ("best_640x480.onnx", sha)):
                open(os.path.join(d, name), "wb").write(b"x")
                json.dump({"source_sha256": s}, open(os.path.join(d, name[:-5] + ".json"), "w"))
            self.assertTrue(ye.resolve_model_path().endswith("best_640x480.onnx"))
            open(pt, "wb").write(b"newer-weights")            # 换了 .pt 但还没转换
            p = ye.resolve_model_path()
            self.assertIn("尚未转换", ye.MODEL_NOTICE)
            self.assertTrue(p.endswith(".pt") or p.endswith(".onnx"))
        finally:
            ye.MODEL_DIR = old


class TestRemoteArgv(unittest.TestCase):
    def test_quoting(self):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools"))
        import convert_model as cm
        w = cm.remote_argv({"remote": "u@1.2.3.4", "remote_type": "windows", "wsl_distro": "Ubuntu-22.04",
                            "remote_init": "", "ssh_port": 22}, "cd ~/x && hb_mapper --version")
        self.assertEqual(w[-8:-1], ["u@1.2.3.4", "wsl", "-d", "Ubuntu-22.04", "-e", "bash", "-lc"])
        self.assertEqual(w[-1], '"cd ~/x && hb_mapper --version"')
        l = cm.remote_argv({"remote": "u@h", "remote_type": "linux", "ssh_port": 22}, "echo ok")
        self.assertEqual(l[-3:], ["bash", "-lc", '"echo ok"'])


if __name__ == "__main__":
    unittest.main()
