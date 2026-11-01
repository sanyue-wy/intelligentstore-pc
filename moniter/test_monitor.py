"""
程序监控模块单元测试
"""

import unittest
import time
from MessegeCenter import MessageBus
from moniter.health import HealthWatchdog
from moniter.alert import AlertManager
from moniter.service import MonitorService
from MessegeCenter.topics import ModuleHeartbeat


class TestMonitor(unittest.TestCase):

    def test_health_watchdog(self):
        watchdog = HealthWatchdog()
        # 上报一次 communication 模块心跳
        hb = ModuleHeartbeat(module_name="communication", status="OK", fps=20.0)
        watchdog.record_heartbeat(hb)

        status_map = watchdog.check_all_modules()
        self.assertIn("communication", status_map)
        self.assertTrue(status_map["communication"].is_alive)
        self.assertEqual(status_map["communication"].status, "OK")

    def test_alert_manager(self):
        alert_mgr = AlertManager()
        a = alert_mgr.raise_alert("ERROR", "vision", 404, "Camera not found")
        self.assertEqual(len(alert_mgr.get_active_alerts()), 1)

        alert_mgr.resolve_alert("vision", 404)
        self.assertEqual(len(alert_mgr.get_active_alerts()), 0)
        self.assertEqual(len(alert_mgr.get_history()), 1)

    def test_monitor_service(self):
        bus = MessageBus()
        service = MonitorService(bus)
        service.start(interval_s=0.1)
        time.sleep(0.25)
        service.stop()

        # 检查是否成功发出监控指标
        metrics_msgs = bus.get_history(topic_pattern="monitor/metrics")
        self.assertGreater(len(metrics_msgs), 0)


if __name__ == "__main__":
    unittest.main()
