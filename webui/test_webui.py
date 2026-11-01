# -*- coding: utf-8 -*-
"""网页接口自动化测试: 口令、静态资源、页面渲染、通讯 mock、流程后端、执行页 HTTP。"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webui import base, pages            # noqa: E402
from webui.comm_debug import CommDebug   # noqa: E402
from webui.flow_debug import FlowDebug   # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def _post(url):
    req = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status, r.read()


class FakeTarget:
    def __init__(self, i, u, v, conf):
        self.id = i
        self.u_pixel = u
        self.v_pixel = v
        self.confidence = conf
        self.bbox = (u - 10, v - 10, u + 10, v + 10)
        self.hits = 3
        self.label = "red"
        self.color = "red"
        self.x_mm = 100 + i * 10
        self.y_mm = 50


class FakeSvc:
    def scan(self):
        return [FakeTarget(1, 100, 100, 0.8), FakeTarget(2, 300, 200, 0.7)], {"n": 2}


class StubApp:
    def state_dict(self):
        return {"state": "standby", "elapsed_s": 0.0, "total_done": 0, "items": [], "events": [],
                "precheck": [], "precheck_ok": True, "precheck_summary": "ok", "slots": {},
                "warnings": [], "screen_seq": 0, "scan_n": 0, "hint": ""}

    def frame_jpeg(self):
        return b"\xff\xd8\xff\xd9"

    def screen_png(self):
        return b"\x89PNG\r\n\x1a\n"

    def request(self, a):
        return True, "ok:" + a


class WebuiTest(unittest.TestCase):
    def test_token_ok(self):
        self.assertTrue(base.token_ok({"X-Token": "abc"}, "abc"))
        self.assertFalse(base.token_ok({}, "abc"))
        self.assertTrue(base.token_ok({}, ""))
        self.assertTrue(base.token_ok({"Cookie": "a=1; is_token=abc"}, "abc"))

    def test_static(self):
        self.assertIsNotNone(base.read_static("app.css"))
        self.assertIsNotNone(base.read_static("common.js"))
        self.assertIn("text/css", base.guess_type("app.css"))
        self.assertIsNone(base.read_static("../base.py"))

    def test_pages(self):
        for p in ("/comm", "/flow", "/run"):
            h = pages.page(p)
            self.assertIn("智能分拣调试台", h)
            self.assertIn("/static/app.css", h)
        self.assertIsNone(pages.page("/nope"))

    def test_comm_mock(self):
        c = CommDebug()
        r = c.connect_mock()
        self.assertTrue(r["ok"], r)
        s = c.snapshot()
        self.assertTrue(s["available"])
        self.assertTrue(s["connected"])
        d = c.send({"cmd": "clear"})
        self.assertIn("ok", d)
        self.assertIsInstance(c.list_ports(), list)
        self.assertTrue(c.disconnect()["ok"])

    def test_flow_scan_plan(self):
        svc = FakeSvc()
        c = CommDebug()
        c.connect_mock()
        fd = FlowDebug(svc, client=c.client,
                       target_to_dict=lambda t: {"id": t.id, "label": t.label, "conf": t.confidence,
                                                 "u": t.u_pixel, "v": t.v_pixel,
                                                 "x_mm": t.x_mm, "y_mm": t.y_mm})
        r = fd.scan()
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["targets"]), 2)
        r = fd.plan()
        self.assertTrue(r["ok"])
        self.assertIsNotNone(r["chosen"])
        s = fd.snapshot()
        self.assertTrue(s["connected"])
        self.assertTrue(s["targets"])
        fd.reset()
        self.assertEqual(fd.snapshot()["targets"], [])
        c.disconnect()

    def test_run_page_http(self):
        from UI.run_page import RunPage
        port = free_port()
        rp = RunPage(StubApp(), port=port, host="127.0.0.1")
        self.assertTrue(rp.start())
        time.sleep(0.3)
        try:
            st, body = _get("http://127.0.0.1:%d/" % port)
            self.assertEqual(st, 200)
            self.assertIn(b"<html", body)
            st, b = _get("http://127.0.0.1:%d/api/state" % port)
            self.assertEqual(st, 200)
            self.assertEqual(json.loads(b)["state"], "standby")
            st, b = _get("http://127.0.0.1:%d/screen" % port)
            self.assertEqual(st, 200)
            self.assertIn(b"screen.png", b)
            st, b = _post("http://127.0.0.1:%d/api/start" % port)
            self.assertEqual(st, 200)
            self.assertTrue(json.loads(b)["ok"])
            st, b = _get("http://127.0.0.1:%d/static/app.css" % port)
            self.assertEqual(st, 200)
        finally:
            rp.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
