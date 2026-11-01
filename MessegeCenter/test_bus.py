"""
消息中心单元测试
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # 直接 python3 test_bus.py 也能跑

from MessegeCenter.bus import MessageBus
from MessegeCenter.topics import Message


class TestMessageBus(unittest.TestCase):

    def test_publish_subscribe(self):
        bus = MessageBus()
        received = []

        def on_msg(m: Message):
            received.append(m)

        bus.subscribe("test/topic", on_msg)
        bus.publish("test/topic", {"val": 123}, sender="unit_test")

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].topic, "test/topic")
        self.assertEqual(received[0].payload["val"], 123)
        self.assertEqual(received[0].sender, "unit_test")

    def test_wildcard_matching(self):
        bus = MessageBus()
        comm_msgs = []
        all_msgs = []

        bus.subscribe("comm/*", lambda m: comm_msgs.append(m))
        bus.subscribe("*", lambda m: all_msgs.append(m))

        bus.publish("comm/status", "status_data")
        bus.publish("comm/cmd", "cmd_data")
        bus.publish("vision/target", "target_data")

        self.assertEqual(len(comm_msgs), 2)
        self.assertEqual(len(all_msgs), 3)

    def test_history(self):
        bus = MessageBus(max_history=10)
        for i in range(15):
            bus.publish("test", i)

        hist = bus.get_history()
        self.assertEqual(len(hist), 10)
        self.assertEqual(hist[-1].payload, 14)




class TestRaceLedger(unittest.TestCase):
    def test_rules(self):
        from MessegeCenter import MessageBus, RaceLedger
        from MessegeCenter.race import publish_state, publish_item, publish_event
        bus = MessageBus(self_dispatch=True)
        led = RaceLedger(bus, idle_limit_s=0.6, idle_warn_s=0.3).start()
        publish_state(bus, "running", run_id="t", expected_total=12)
        publish_item(bus, seq=1, label="红色正方体", category="cube|red|", slot=1, result="ok", total=1, image="a.jpg")
        publish_item(bus, seq=2, label="红色正方体", category="cube|red|", slot=2, result="ok", total=2, image="b.jpg")
        publish_item(bus, seq=None, label="蓝色圆柱", category="cyl|blue|", slot=2, result="grip_fail")
        publish_item(bus, seq=4, label="蓝色圆柱", category="cyl|blue|", slot=2, result="ok", total=3, image="")
        codes = [r.code for r in led.snapshot().rules]
        self.assertIn("same_category_two_slots", codes)
        self.assertIn("slot_mixed", codes)
        self.assertIn("seq_gap", codes)
        self.assertIn("display_incomplete", codes)
        import time
        time.sleep(1.3)
        self.assertIn("idle_15s", [r.code for r in led.snapshot().rules])
        publish_event(bus, "x")
        publish_state(bus, "finished", result="完成", total_done=3)
        s = led.snapshot()
        self.assertEqual((s.state, s.total_done, s.attempts, s.failures), ("finished", 3, 4, 1))
        self.assertIn("incomplete", [r.code for r in s.rules])
        led.stop()


if __name__ == "__main__":
    unittest.main()
