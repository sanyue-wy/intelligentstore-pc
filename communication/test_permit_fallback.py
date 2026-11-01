"""方案B 兼容兜底单测: 固件不置 RES_FLAGS.PICK 时, 按"运动到位+稳定"发 PERMIT。

不连硬件: 以轻量 FakeClient 作为 self, 直接调用 IntelligentSortingClient.execute_sorting_task_ex 未绑定方法。
覆盖: (1) 兜底生效→成功发 PERMIT; (2) PERMIT_FALLBACK=0→超时失败; (3) 报警/错误态→不误发 PERMIT;
(4) 目标始终很远→不误发, 正常超时; (5) 有 PICK 时走原生路径 (回归保护)。
"""
import os
import time
import unittest

from communication.client import IntelligentSortingClient


class _St(object):
    """最小 HostStatus 替身: 只提供 execute_sorting_task_ex 用到的字段/属性。"""

    def __init__(self, x=0.0, y=0.0, progress=0, state=2, error=0,
                 res_flags=0x00, alarm=False, estop=False, grip_fail=False,
                 aborted=False, done=False, success=False, stage_desc="", error_desc=""):
        self.cur_x_mm = float(x)
        self.cur_y_mm = float(y)
        self.progress = int(progress)
        self.state = int(state)
        self.error = int(error)
        self.res_flags = int(res_flags)
        self.is_alarm = bool(alarm)
        self.is_estop = bool(estop)
        self.is_grip_fail = bool(grip_fail)
        self.is_aborted = bool(aborted)
        self.is_task_done = bool(done)
        self.is_success = bool(success)
        self.is_permit_required = bool(self.res_flags & 0x04)
        self.stage_desc = stage_desc
        self.error_desc = error_desc


class _FakeClient(object):
    def __init__(self, target, mode="no_pick", arrive_at=0.25):
        self.t0 = time.time()
        self.target = (float(target[0]), float(target[1]))
        self.mode = mode          # no_pick | pick | fault | far
        self.arrive_at = arrive_at
        self.permit = False
        self.permit_calls = []
        self.moved = False

    @property
    def latest_status(self):
        t = time.time() - self.t0
        if self.mode == "fault":
            return _St(x=0, y=0, state=4, error=0x0009, res_flags=0x01, alarm=True)
        if self.mode == "far":
            return _St(x=self.target[0] - 200, y=self.target[1] - 200, progress=10)
        if self.permit:
            return _St(x=self.target[0], y=self.target[1], progress=100,
                       res_flags=0x03, done=True, success=True)
        if self.mode == "pick":
            return _St(x=self.target[0], y=self.target[1], progress=100, res_flags=0x04)
        # no_pick: 前 arrive_at 秒在远处移动, 之后到目标附近但不置 PICK
        if t < self.arrive_at:
            return _St(x=self.target[0] - 200, y=self.target[1] - 200, progress=10)
        return _St(x=self.target[0], y=self.target[1], progress=60)

    def send_move(self, *a, **k):
        self.moved = True
        return True

    def send_clear(self):
        pass

    def send_cancel(self, *a):
        pass

    def send_permit(self, *a, **k):
        self.permit = True
        self.permit_calls.append(a)


class PermitFallbackTest(unittest.TestCase):
    def _run(self, client, tag, timeout_s=5.0):
        old = os.environ.get(tag, None)
        return old

    def setUp(self):
        self._env_backup = {k: os.environ.get(k) for k in (
            "PERMIT_FALLBACK", "PERMIT_ARRIVE_TOL_MM", "PERMIT_STABLE_S", "PERMIT_MIN_ELAPSED_S")}
        os.environ["PERMIT_ARRIVE_TOL_MM"] = "5.0"
        os.environ["PERMIT_STABLE_S"] = "0.15"
        os.environ["PERMIT_MIN_ELAPSED_S"] = "0.05"

    def tearDown(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _exec(self, client, target, timeout_s):
        return IntelligentSortingClient.execute_sorting_task_ex(
            client, task_id=1, target_x_mm=float(target[0]), target_y_mm=float(target[1]),
            timeout_s=timeout_s, permit_delay_s=0.0)

    def test_1_fallback_sends_permit(self):
        os.environ["PERMIT_FALLBACK"] = "1"
        c = _FakeClient((100.0, 50.0), mode="no_pick", arrive_at=0.25)
        t0 = time.time()
        res = self._exec(c, (100.0, 50.0), timeout_s=5.0)
        dt = time.time() - t0
        self.assertTrue(c.moved, "应已下发运动")
        self.assertTrue(c.permit, "兜底应发出 PERMIT")
        self.assertTrue(res.success, "兜底到位后应整体成功")
        self.assertLess(dt, 4.5)

    def test_2_disabled_times_out(self):
        os.environ["PERMIT_FALLBACK"] = "0"
        c = _FakeClient((100.0, 50.0), mode="no_pick", arrive_at=0.1)
        res = self._exec(c, (100.0, 50.0), timeout_s=0.6)
        self.assertFalse(c.permit, "关闭兜底后不应发 PERMIT")
        self.assertFalse(res.success)
        self.assertIn("前往取件点超时", res.message)

    def test_3_fault_no_permit(self):
        os.environ["PERMIT_FALLBACK"] = "1"
        c = _FakeClient((0.0, 0.0), mode="fault")
        res = self._exec(c, (0.0, 0.0), timeout_s=1.5)
        self.assertFalse(c.permit, "报警/故障态不得误发 PERMIT")
        self.assertFalse(res.success)

    def test_4_far_target_no_false_permit(self):
        os.environ["PERMIT_FALLBACK"] = "1"
        c = _FakeClient((100.0, 50.0), mode="far")
        res = self._exec(c, (100.0, 50.0), timeout_s=0.6)
        self.assertFalse(c.permit, "目标始终很远时不得误发 PERMIT")
        self.assertFalse(res.success)
        self.assertIn("前往取件点超时", res.message)

    def test_5_native_pick_path_still_works(self):
        os.environ["PERMIT_FALLBACK"] = "1"
        c = _FakeClient((100.0, 50.0), mode="pick")
        res = self._exec(c, (100.0, 50.0), timeout_s=3.0)
        self.assertTrue(c.permit, "原生 PICK 路径应照常发 PERMIT")
        self.assertTrue(res.success)


if __name__ == "__main__":
    unittest.main()
