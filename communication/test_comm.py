"""
单元测试: 协议打包解包、粘包处理、模拟分拣工作流、抓空异常流程与链路看门狗验证
严格遵循 HOST_COMM_GUIDE.md 规范
"""

import unittest
import time
import struct
from communication.protocol import (
    HOST_FRAME_LEN, HOST_SOF, HOST_VER, HOST_TYPE_CMD, HOST_TYPE_STATUS,
    CmdFlags, GripCmd, StatusFlags, SysState, GripperState, ResFlags,
    ResStage, ErrorMask, HostCmd, HostStatus, pack_cmd, unpack_status,
    build_move_cmd, build_permit_cmd, build_estop_cmd, build_clear_cmd,
    build_cancel_cmd, build_grip_cmd, build_poll_cmd, StreamFramer
)
from communication.client import (
    IntelligentSortingClient, IntelligentSortingHost, SortingTaskResult
)
from communication.mock_device import MockSTM32


class TestProtocol(unittest.TestCase):

    def test_cmd_pack_size(self):
        cmd = HostCmd(
            flags=CmdFlags.MOTION,
            seq=42,
            task_id=1001,
            grip_cmd=GripCmd.HOLD,
            timeout_ms=5000
        )
        cmd.target_x_mm = 40.5
        cmd.target_y_mm = -25.25
        frame = pack_cmd(cmd)

        self.assertEqual(len(frame), HOST_FRAME_LEN)
        self.assertEqual(frame[0:2], HOST_SOF)
        self.assertEqual(frame[2], HOST_VER)
        self.assertEqual(frame[3], HOST_TYPE_CMD)
        self.assertEqual(frame[4], CmdFlags.MOTION)

    def test_status_unpack(self):
        # 构造一个 32 字节状态帧
        data = struct.pack(
            "<2sBBBHIiiBBHHBBB4s",
            HOST_SOF,
            HOST_VER,
            HOST_TYPE_STATUS,
            StatusFlags.BUSY | StatusFlags.ALARM | StatusFlags.USB_OK,
            123,        # seq
            9999,       # task_id
            45000,      # cur_x = 45.0 mm
            -12340,     # cur_y = -12.34 mm
            SysState.RUNNING,
            GripperState.HELD,
            ErrorMask.PARAM | ErrorMask.TIMEOUT, # error
            42,         # ack_seq
            45,         # progress = 45%
            ResFlags.PICK,
            ResStage.WAIT_PERMIT,
            b"\x00" * 4
        )
        self.assertEqual(len(data), 32)
        st = unpack_status(data)
        self.assertIsNotNone(st)
        self.assertEqual(st.seq, 123)
        self.assertEqual(st.task_id, 9999)
        self.assertAlmostEqual(st.cur_x_mm, 45.0, places=3)
        self.assertAlmostEqual(st.cur_y_mm, -12.34, places=2)
        self.assertEqual(st.state, SysState.RUNNING)
        self.assertEqual(st.gripper, GripperState.HELD)
        self.assertTrue(st.is_busy)
        self.assertTrue(st.is_alarm)
        self.assertTrue(st.is_usb_ok)
        self.assertTrue(st.is_pick_ready)
        self.assertTrue(st.is_permit_required)
        self.assertEqual(st.ack_seq, 42)
        self.assertEqual(st.progress, 45)
        self.assertIn("参数超限", st.error_desc)
        self.assertIn("超时", st.error_desc)
        self.assertIn("去取件位", st.stage_desc)

    def test_error_mask_alignment(self):
        """验证错误码与 host_comm.h / HOST_COMM_GUIDE.md 完全对齐"""
        self.assertEqual(ErrorMask.PROTO, 0x0001)
        self.assertEqual(ErrorMask.PARAM, 0x0002)
        self.assertEqual(ErrorMask.QUEUE, 0x0004)
        self.assertEqual(ErrorMask.TIMEOUT, 0x0008)
        self.assertEqual(ErrorMask.NOTREACH, 0x0010)
        self.assertEqual(ErrorMask.GRIPFAIL, 0x0020)
        self.assertEqual(ErrorMask.GRIPSLIP, 0x0040)
        self.assertEqual(ErrorMask.PLACEFAIL, 0x0080)
        self.assertEqual(ErrorMask.ESTOP, 0x0100)
        self.assertEqual(ErrorMask.INTERNAL, 0x0200)
        self.assertEqual(ErrorMask.COMM, 0x0400)
        self.assertEqual(ErrorMask.FOREIGN_OBJ, 0x0800)

    def test_gripper_state_alignment(self):
        """验证夹爪状态与 host_comm.h 完全对齐 (0=OPEN, 1=CLOSED, 2=MOVING, 3=HELD, 4=OPENING, 5=EMPTY, 6=SLIP)"""
        self.assertEqual(GripperState.OPEN, 0)
        self.assertEqual(GripperState.CLOSED, 1)
        self.assertEqual(GripperState.MOVING, 2)
        self.assertEqual(GripperState.HELD, 3)
        self.assertEqual(GripperState.OPENING, 4)
        self.assertEqual(GripperState.EMPTY, 5)
        self.assertEqual(GripperState.SLIP, 6)

    def test_stream_framer(self):
        framer = StreamFramer()

        # 构造状态帧
        status_frame = struct.pack(
            "<2sBBBHIiiBBHHBBB4s",
            HOST_SOF, HOST_VER, HOST_TYPE_STATUS, 0, 1, 10, 0, 0,
            SysState.IDLE, GripperState.OPEN, 0, 1, 0, 0, 0, b"\x00"*4
        )

        # 模拟粘包与垃圾字节: 垃圾 + 完整帧 + 半帧 + 剩余半帧
        stream = b"\xFF\x00\x12" + status_frame + status_frame[:10]
        res1 = framer.feed(stream)
        self.assertEqual(len(res1), 1)

        res2 = framer.feed(status_frame[10:])
        self.assertEqual(len(res2), 1)


class TestClientWithMock(unittest.TestCase):

    def setUp(self):
        self.client = IntelligentSortingClient()
        self.client.connect_mock()

    def tearDown(self):
        self.client.disconnect()

    def test_guide_constants_compatibility(self):
        """验证类属性与 HOST_COMM_GUIDE.md 第 4 节规范一致"""
        self.assertEqual(self.client.FLAG_MOTION, 0x01)
        self.assertEqual(self.client.FLAG_GRIP, 0x02)
        self.assertEqual(self.client.FLAG_ESTOP, 0x04)
        self.assertEqual(self.client.FLAG_CLEAR, 0x08)
        self.assertEqual(self.client.FLAG_CANCEL, 0x10)
        self.assertEqual(self.client.FLAG_PERMIT, 0x20)
        self.assertEqual(self.client.RES_DONE, 0x01)
        self.assertEqual(self.client.RES_OK, 0x02)
        self.assertEqual(self.client.RES_PICK, 0x04)
        self.assertEqual(self.client.ERR_GRIPFAIL, 0x0020)

    def test_link_alive_and_watchdog(self):
        time.sleep(0.1)
        self.assertTrue(self.client.is_connected)
        self.assertTrue(self.client.is_link_alive)
        self.assertLess(self.client.link_age_s, 0.5)

    def test_estop_and_clear(self):
        time.sleep(0.1) # 等待仿真启动
        ok = self.client.send_estop()
        self.assertTrue(ok)
        time.sleep(0.1)
        st = self.client.latest_status
        self.assertIsNotNone(st)
        self.assertTrue(st.is_estop)

        # 清除急停
        self.client.send_clear()
        time.sleep(0.1)
        st = self.client.latest_status
        self.assertFalse(st.is_estop)
        self.assertEqual(st.state, SysState.IDLE)

    def test_automated_sorting_workflow(self):
        time.sleep(0.1)
        progresses = []
        def on_prog(msg, pct):
            progresses.append((msg, pct))

        res = self.client.execute_sorting_task_ex(
            task_id=201,
            target_x_mm=30.0,
            target_y_mm=15.0,
            timeout_s=10.0,
            progress_callback=on_prog
        )
        self.assertTrue(res.success)
        self.assertGreater(len(progresses), 0)
        self.assertEqual(progresses[-1][1], 100)

    def test_grip_fail_workflow(self):
        """测试抓空/抓取失败异常流程 (对齐 HOST_COMM_GUIDE.md 第 3.2 节)"""
        time.sleep(0.1)
        # 配置下位机仿真器发生抓空
        self.client._mock_dev.simulate_grip_fail = True

        res = self.client.execute_sorting_task_ex(
            task_id=202,
            target_x_mm=25.0,
            target_y_mm=10.0,
            timeout_s=10.0
        )
        self.assertFalse(res.success)
        self.assertTrue(res.is_grip_fail)
        self.assertEqual(res.error, ErrorMask.GRIPFAIL)
        self.assertIn("抓空", res.message)

    def test_task_cancellation(self):
        """测试任务取消"""
        time.sleep(0.1)
        # 发送移动指令开始新任务
        self.client.send_move(task_id=301, x_mm=50.0, y_mm=50.0)
        time.sleep(0.1)

        # 发送取消
        ok = self.client.send_cancel(task_id=301)
        self.assertTrue(ok)
        time.sleep(0.1)

        st = self.client.latest_status
        self.assertIsNotNone(st)
        self.assertTrue(st.is_aborted)

    def test_aux_mechanisms_control(self):
        """测试 Z 轴升降、托盘震动、储物盘旋转分度控制"""
        # 1. Z 轴升降
        ok = self.client.send_z_down()
        self.assertTrue(ok)
        time.sleep(0.05)
        st = self.client.latest_status
        self.assertEqual(st.z_state, 1)  # DOWN
        self.assertEqual(st.z_desc, "下潜取件位")

        ok = self.client.send_z_up()
        self.assertTrue(ok)
        time.sleep(0.05)
        st = self.client.latest_status
        self.assertEqual(st.z_state, 0)  # UP
        self.assertEqual(st.z_desc, "抬起安全位")

        # 2. 托盘震动与复位
        ok = self.client.send_tray_shake()
        self.assertTrue(ok)
        time.sleep(0.05)
        st = self.client.latest_status
        self.assertEqual(st.tray_state, 1)  # MOVING

        ok = self.client.send_tray_home()
        self.assertTrue(ok)
        time.sleep(0.05)
        st = self.client.latest_status
        self.assertEqual(st.tray_state, 0)  # IDLE

        # 3. 储物格每次旋转 60 度 (逆时针为正)
        init_slot = self.client.latest_status.storage_slot
        ok = self.client.send_storage_step(ccw=True)  # 逆时针 +1 格
        self.assertTrue(ok)
        time.sleep(0.05)
        st = self.client.latest_status
        self.assertEqual(st.storage_slot, (init_slot + 1) % 6)

        ok = self.client.send_storage_step(ccw=False) # 顺时针 -1 格
        self.assertTrue(ok)
        time.sleep(0.05)
        st = self.client.latest_status
        self.assertEqual(st.storage_slot, init_slot)

        ok = self.client.send_storage_slot(4) # 指定槽位 4
        self.assertTrue(ok)
        time.sleep(0.05)
        st = self.client.latest_status
        self.assertEqual(st.storage_slot, 4)


if __name__ == "__main__":
    unittest.main()
