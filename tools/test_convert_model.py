#!/usr/bin/env python3
"""
A7 部署门禁 —— 纯逻辑单测 (不调 hb_mapper、不联网、不动 vision/models 真文件)。

跑:   cd <工程根> && python3 -m unittest tools.test_convert_model
覆盖:
  · 余弦相似度: 0.98 -> 不部署;  0.995 -> 部署  (阈值 cosine_min=0.99)
  · BPU/ONNX 一致率: 0.80 -> 不部署;  0.90 -> 部署  (阈值 consistency_min=0.85)
  · --force 绕过余弦/一致率门禁, 但不绕过 .bin 装不上 / ONNX 对拍不过
  · 校准图: 上限 calib_max_images=200, 下限 calib_min_images=20, hard_cases 优先且尽量每类覆盖
  · 决策为"不部署"时 (含 --dry-run) 一个模型文件都不移动/不覆盖 —— 用临时目录做证据
  · 阈值来自 tools/convert_config.json + 环境变量, 不写死在代码里
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_convert_model():
    """按文件路径加载 tools/convert_model.py (tools 不是包, 不能用 import tools.convert_model)。"""
    path = os.path.join(HERE, "convert_model.py")
    spec = importlib.util.spec_from_file_location("a7_convert_model_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


cm = _load_convert_model()


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot(d):
    """目录下 (相对路径 -> (大小, mtime_ns, sha256)) —— 用来证明"没动过"。"""
    out = {}
    for base, _dirs, files in os.walk(d):
        for name in files:
            p = os.path.join(base, name)
            st = os.stat(p)
            out[os.path.relpath(p, d)] = (st.st_size, st.st_mtime_ns, _sha(p))
    return out


def _protected():
    """A7 必须保护的工程文件: 判定不通过 / --dry-run 时一个都不能变。"""
    fs = [os.path.join(ROOT, "tools", "convert_config.json"),
          os.path.join(ROOT, "tools", "data.yaml"),
          os.path.join(ROOT, "vision", "classes.json")]
    md = os.path.join(ROOT, "vision", "models")
    if os.path.isdir(md):
        fs += [os.path.join(md, n) for n in sorted(os.listdir(md))]
    return [p for p in fs if os.path.isfile(p)]


def _snap_files(paths):
    out = {}
    for p in paths:
        st = os.stat(p)
        out[p] = (st.st_size, st.st_mtime_ns, _sha(p))
    return out


class TestDecide(unittest.TestCase):
    """decide(cfg, metrics) 是纯函数: 只看入参, 不读文件/不看硬件。"""

    def setUp(self):
        self.cfg = dict(cm.GATE_DEFAULTS)          # cosine_min=0.99 consistency_min=0.85

    def _m(self, **kw):
        m = {"force": False, "has_bin": True, "onnx_ok": True}
        m.update(kw)
        return m

    def test_cosine_098_not_deployed(self):
        d = cm.decide(self.cfg, self._m(cosine=0.98))
        self.assertFalse(d.deploy)
        self.assertTrue(any("余弦" in r for r in d.reasons), d.reasons)

    def test_cosine_0995_deployed(self):
        d = cm.decide(self.cfg, self._m(cosine=0.995))
        self.assertTrue(d.deploy, d.reasons)
        self.assertEqual(d.reasons, [])

    def test_cosine_exactly_threshold_is_deployed(self):
        d = cm.decide(self.cfg, self._m(cosine=0.99))
        self.assertTrue(d.deploy, d.reasons)

    def test_consistency_080_not_deployed(self):
        d = cm.decide(self.cfg, self._m(cosine=0.995, consistency=0.80))
        self.assertFalse(d.deploy)
        self.assertTrue(any("一致率" in r for r in d.reasons), d.reasons)

    def test_consistency_090_deployed(self):
        d = cm.decide(self.cfg, self._m(cosine=0.995, consistency=0.90))
        self.assertTrue(d.deploy, d.reasons)

    def test_force_bypasses_cosine(self):
        d = cm.decide(self.cfg, self._m(force=True, cosine=0.50))
        self.assertTrue(d.deploy, d.reasons)
        self.assertTrue(any("--force" in n for n in d.notes), d.notes)

    def test_force_bypasses_consistency(self):
        d = cm.decide(self.cfg, self._m(force=True, cosine=0.995, consistency=0.10))
        self.assertTrue(d.deploy, d.reasons)

    def test_force_does_not_bypass_bin_load_failure(self):
        d = cm.decide(self.cfg, self._m(force=True, cosine=0.995, bpu_load_ok=False,
                                       bpu_error="加载失败"))
        self.assertFalse(d.deploy, "force 不应让装不上的 .bin 通过")
        self.assertTrue(any("BPU" in r for r in d.reasons), d.reasons)

    def test_force_does_not_bypass_onnx_mismatch(self):
        d = cm.decide(self.cfg, self._m(force=True, onnx_ok=False))
        self.assertFalse(d.deploy)

    def test_missing_cosine_is_a_note_not_a_block(self):
        d = cm.decide(self.cfg, self._m(cosine=None))
        self.assertTrue(d.deploy, "日志里没解析到余弦时不做余弦门禁 (与基线一致)")
        self.assertTrue(any("余弦" in n for n in d.notes), d.notes)

    def test_no_bin_only_deploys_onnx(self):
        d = cm.decide(self.cfg, {"force": False, "has_bin": False, "onnx_ok": True})
        self.assertTrue(d.deploy)
        self.assertFalse(d.deploy_bin)

    def test_thresholds_come_from_cfg_not_hardcoded(self):
        """同一组度量: cfg 阈值不同 -> 结论不同, 说明阈值没有写死。"""
        m = self._m(cosine=0.60)
        self.assertFalse(cm.decide(self.cfg, m).deploy)
        loose = dict(self.cfg, cosine_min=0.50)
        self.assertTrue(cm.decide(loose, m).deploy)

    def test_decide_is_pure(self):
        """decide 不碰文件系统: 连续调用后, 工程里该保护的文件一个字节都没变。"""
        fs = _protected()
        before = _snap_files(fs)
        cm.decide(self.cfg, self._m(cosine=0.1))
        cm.decide(self.cfg, self._m(cosine=0.999, consistency=0.9))
        self.assertEqual(before, _snap_files(fs))


class TestGateCfg(unittest.TestCase):
    """阈值全部来自 tools/convert_config.json + 环境变量, 不许写死。"""

    def test_repo_config_declares_a7_defaults(self):
        p = os.path.join(ROOT, "tools", "convert_config.json")
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw["calib_max_images"], 200)
        self.assertEqual(raw["calib_min_images"], 20)
        self.assertEqual(raw["cosine_min"], 0.99)
        self.assertEqual(raw["consistency_min"], 0.85)
        self.assertEqual(raw["hard_cases_dir"], "dataset/hard_cases")
        self.assertEqual(raw["regression_dir"], "dataset/regression")

    def test_builtin_defaults(self):
        cfg = cm.load_gate_cfg(path="/nonexistent/a7.json")
        self.assertEqual(cfg["calib_max_images"], 200)
        self.assertEqual(cfg["calib_min_images"], 20)
        self.assertEqual(cfg["cosine_min"], 0.99)
        self.assertEqual(cfg["consistency_min"], 0.85)

    def test_file_overrides_defaults(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "cfg.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"calib_max_images": 7, "cosine_min": 0.9, "_说明": ["x"]}, f)
            cfg = cm.load_gate_cfg(path=p)
            self.assertEqual(cfg["calib_max_images"], 7)
            self.assertAlmostEqual(cfg["cosine_min"], 0.9)
            self.assertEqual(cfg["consistency_min"], 0.85)   # 没写的沿用默认
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_env_overrides_file(self):
        d = tempfile.mkdtemp()
        saved = {k: os.environ.get(k) for k in cm.GATE_ENV.values()}
        try:
            p = os.path.join(d, "cfg.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"calib_max_images": 7, "cosine_min": 0.9}, f)
            os.environ["IS_CALIB_MAX"] = "33"
            os.environ["IS_COSINE_MIN"] = "0.75"
            os.environ["IS_CONSISTENCY_MIN"] = "0.5"
            os.environ["IS_CALIB_MIN"] = "5"
            cfg = cm.load_gate_cfg(path=p)
            self.assertEqual(cfg["calib_max_images"], 33)
            self.assertAlmostEqual(cfg["cosine_min"], 0.75)
            self.assertAlmostEqual(cfg["consistency_min"], 0.5)
            self.assertEqual(cfg["calib_min_images"], 5)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            shutil.rmtree(d, ignore_errors=True)

    def test_bad_env_is_ignored(self):
        saved = os.environ.get("IS_CALIB_MAX")
        try:
            os.environ["IS_CALIB_MAX"] = "很多张"
            cfg = cm.load_gate_cfg(path="/nonexistent/a7.json")
            self.assertEqual(cfg["calib_max_images"], 200)
        finally:
            if saved is None:
                os.environ.pop("IS_CALIB_MAX", None)
            else:
                os.environ["IS_CALIB_MAX"] = saved

    def test_remote_keys_survive_the_new_gate_keys(self):
        """新增的门禁键不能把远程编译机配置挤掉 (load_remote_cfg 读取所有非 _ 键)。"""
        cfg = cm.load_remote_cfg(None)
        self.assertIn("remote", cfg)
        cfg2 = cm.load_gate_cfg()
        self.assertIn("cosine_min", cfg2)


class TestPlanCalib(unittest.TestCase):
    """plan_calib / calib_shortage / scan_hard_cases: 上限 200、下限 20、hard_cases 优先并每类覆盖。"""

    def setUp(self):
        self.cfg = dict(cm.GATE_DEFAULTS)

    def test_max_200(self):
        general = [f"/g/{i:04d}.jpg" for i in range(300)]
        picked, per = cm.plan_calib(self.cfg, general)
        self.assertEqual(len(picked), 200)
        self.assertEqual(per.get("?"), 200)

    def test_under_max_keeps_all(self):
        general = [f"/g/{i:04d}.jpg" for i in range(60)]
        picked, per = cm.plan_calib(self.cfg, general)
        self.assertEqual(len(picked), 60)

    def test_min_images(self):
        self.assertTrue(cm.calib_shortage(self.cfg, 19))
        self.assertFalse(cm.calib_shortage(self.cfg, 20))
        self.assertFalse(cm.calib_shortage(self.cfg, 200))

    def test_min_images_from_cfg(self):
        self.assertFalse(cm.calib_shortage(dict(self.cfg, calib_min_images=5), 10))
        self.assertTrue(cm.calib_shortage(dict(self.cfg, calib_min_images=50), 10))

    def test_hard_cases_take_priority_and_cover_every_class(self):
        hard = {"红色": ["/h/红1.jpg", "/h/红2.jpg", "/h/红3.jpg"],
                "黄色": ["/h/黄1.jpg", "/h/黄2.jpg", "/h/黄3.jpg"]}
        general = [f"/g/{i:04d}.jpg" for i in range(100)]
        picked, per = cm.plan_calib(dict(self.cfg, calib_max_images=4), general, hard)
        self.assertEqual(len(picked), 4)
        self.assertEqual(set(picked), {"/h/红1.jpg", "/h/红2.jpg", "/h/黄1.jpg", "/h/黄2.jpg"})
        self.assertEqual(per["红色"], 2)
        self.assertEqual(per["黄色"], 2)

    def test_hard_cases_do_not_starve_a_small_class(self):
        hard = {"稀有": ["/h/rare.jpg"], "常见": [f"/h/c{i}.jpg" for i in range(50)]}
        picked, per = cm.plan_calib(self.cfg, [], hard)
        self.assertIn("/h/rare.jpg", picked)
        self.assertEqual(per["稀有"], 1)
        self.assertEqual(per["常见"], 50)

    def test_hard_cases_then_general_fills_rest(self):
        hard = {"A": ["/h/a.jpg"]}
        general = [f"/g/{i:04d}.jpg" for i in range(5)]
        picked, per = cm.plan_calib(dict(self.cfg, calib_max_images=3), general, hard)
        self.assertEqual(picked, ["/h/a.jpg", "/g/0000.jpg", "/g/0001.jpg"])
        self.assertEqual(per["A"], 1)
        self.assertEqual(per["?"], 2)

    def test_no_duplicates(self):
        hard = {"A": ["/x/1.jpg"]}
        picked, _ = cm.plan_calib(self.cfg, ["/x/1.jpg", "/x/2.jpg"], hard)
        self.assertEqual(sorted(picked), ["/x/1.jpg", "/x/2.jpg"])

    def test_absent_hard_cases_dir(self):
        """[现场待验证] 板上 dataset/hard_cases 还不存在 —— 必须返回空, 且不报错。"""
        self.assertEqual(cm.scan_hard_cases("/nonexistent/dataset/hard_cases"), {})
        self.assertEqual(cm.scan_hard_cases(""), {})

    def test_plan_calib_is_pure(self):
        fs = _protected()
        before = _snap_files(fs)
        cm.plan_calib(self.cfg, ["/nope/a.jpg", "/nope/b.jpg"], {"A": ["/nope/c.jpg"]})
        cm.scan_hard_cases("/nonexistent/hard_cases")
        self.assertEqual(before, _snap_files(fs))


class TestClassAccuracy(unittest.TestCase):
    def test_accuracy(self):
        preds = [("/a/1.jpg", "红"), ("/a/2.jpg", "黄"), ("/a/3.jpg", None), ("/b/1.jpg", "红")]
        truth = {"/a/1.jpg": "红", "/a/2.jpg": "红", "/a/3.jpg": "黄", "/b/1.jpg": "黄"}
        r = cm.class_accuracy(preds, truth)
        self.assertEqual(r["total"], 4)
        self.assertEqual(r["correct"], 1)
        self.assertEqual(r["no_det"], 1)
        self.assertAlmostEqual(r["acc"], 0.25)
        self.assertEqual(r["per_class"]["红"], {"n": 2, "hit": 1})

    def test_empty(self):
        r = cm.class_accuracy([], {})
        self.assertEqual(r["total"], 0)
        self.assertIsNone(r["acc"])

    def test_absent_regression_dir(self):
        """[现场待验证] 板上 dataset/regression 还不存在 —— 必须返回空, 且不报错。"""
        self.assertEqual(cm.labeled_images("/nonexistent/dataset/regression"), [])


class TestNoDeployTouchesNothing(unittest.TestCase):
    """
    A7 核心: 决策为"不部署"时, 在任何文件移动/归档之前就退出, vision/models 一个文件都不动。
    用临时目录做证据 (不碰真 vision/models)。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="a7_test_")
        self.models = os.path.join(self.tmp, "models")
        self.ws = os.path.join(self.tmp, "ws")
        os.makedirs(self.models)
        os.makedirs(self.ws)
        # 在用的旧模型 (对应板上 best_1027_640x480.bin/.onnx)
        self.bin = os.path.join(self.models, "best_1027_640x480.bin")
        self.onnx = os.path.join(self.models, "best_1027_640x480.onnx")
        self.bin_json = os.path.join(self.models, "best_1027_640x480.json")
        with open(self.bin, "wb") as f:
            f.write(b"OLD-BIN" * 100)
        with open(self.onnx, "wb") as f:
            f.write(b"OLD-ONNX" * 100)
        with open(self.bin_json, "w", encoding="utf-8") as f:
            json.dump({"names": ["红色"], "source_sha256": "oldsha", "source_pt": "best_1027.pt"}, f)
        # 本次编译产物 (候选)
        self.new_onnx = os.path.join(self.ws, "model.onnx")
        self.new_bin = os.path.join(self.ws, "new.bin")
        with open(self.new_onnx, "wb") as f:
            f.write(b"NEW-ONNX" * 100)
        with open(self.new_bin, "wb") as f:
            f.write(b"NEW-BIN" * 100)
        self.paths = {"models_dir": self.models, "onnx_src": self.new_onnx, "onnx_dst": self.onnx,
                      "bin_src": self.new_bin, "bin_dst": self.bin, "keep_sha": "newsha"}
        self.snap_models = _snapshot(self.models)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assert_untouched(self):
        self.assertEqual(self.snap_models, _snapshot(self.models),
                         "不部署时不允许改动/新增/移动 models/ 下任何文件")
        self.assertFalse(os.path.exists(os.path.join(self.models, "old")),
                         "不部署时不允许出现 models/old/ (归档就是移动)")
        self.assertTrue(os.path.exists(self.bin))
        self.assertTrue(os.path.exists(self.onnx))

    def test_rejected_decision_apply_is_noop(self):
        cfg = cm.load_gate_cfg(path="/nonexistent/a7.json")
        d = cm.decide(cfg, {"force": False, "has_bin": True, "onnx_ok": True, "cosine": 0.98})
        self.assertFalse(d.deploy)
        r = cm.apply_decision(d, self.paths, {"names": ["红色"]})
        self.assertFalse(r["applied"])
        self.assertEqual(r["written"], [])
        self._assert_untouched()

    def test_rejected_decision_dry_run_is_noop(self):
        cfg = cm.load_gate_cfg(path="/nonexistent/a7.json")
        d = cm.decide(cfg, {"force": False, "has_bin": True, "onnx_ok": True, "consistency": 0.80})
        self.assertFalse(d.deploy)
        r = cm.apply_decision(d, self.paths, {"names": ["红色"]}, dry_run=True)
        self.assertFalse(r["applied"])
        self._assert_untouched()

    def test_accepted_decision_but_dry_run_is_still_noop(self):
        """即使判定通过, --dry-run 也只读不写。"""
        cfg = cm.load_gate_cfg(path="/nonexistent/a7.json")
        d = cm.decide(cfg, {"force": False, "has_bin": True, "onnx_ok": True, "cosine": 0.995})
        self.assertTrue(d.deploy)
        r = cm.apply_decision(d, self.paths, {"names": ["红色"]}, dry_run=True)
        self.assertFalse(r["applied"])
        self._assert_untouched()

    def test_force_deploys_and_archives_old(self):
        """对照: --force 时才会真的动文件 (证明"不动"不是因为功能坏了)。"""
        cfg = cm.load_gate_cfg(path="/nonexistent/a7.json")
        d = cm.decide(cfg, {"force": True, "has_bin": True, "onnx_ok": True, "cosine": 0.10})
        self.assertTrue(d.deploy)
        r = cm.apply_decision(d, self.paths, {"names": ["红色", "黄色"]})
        self.assertTrue(r["applied"])
        with open(self.bin, "rb") as f:
            self.assertEqual(f.read(), b"NEW-BIN" * 100)
        with open(self.onnx, "rb") as f:
            self.assertEqual(f.read(), b"NEW-ONNX" * 100)
        self.assertTrue(os.path.isdir(os.path.join(self.models, "old")))
        with open(self.bin_json, encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["names"], ["红色", "黄色"])
        self.assertTrue(meta["decision"]["deploy"])

    def test_apply_preserves_other_json_fields(self):
        """写模型旁 json 时不覆盖别人加的关键字段。"""
        cfg = cm.load_gate_cfg(path="/nonexistent/a7.json")
        with open(self.bin_json, "w", encoding="utf-8") as f:
            json.dump({"source_sha256": "newsha", "别人加的字段": 42, "names": ["红色"]}, f)
        d = cm.decide(cfg, {"force": True, "has_bin": True, "onnx_ok": True, "cosine": 0.99})
        cm.apply_decision(d, self.paths, {"names": ["红色"], "source_sha256": "newsha"})
        with open(self.bin_json, encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["别人加的字段"], 42)

    def test_retire_old_models_keeps_current_sha(self):
        keep = os.path.join(self.models, "keep.bin")
        with open(keep, "wb") as f:
            f.write(b"KEEP")
        with open(os.path.join(self.models, "keep.json"), "w", encoding="utf-8") as f:
            json.dump({"source_sha256": "newsha"}, f)
        moved = cm.retire_old_models("newsha", self.models)
        self.assertIn("best_1027_640x480.bin", moved)
        self.assertIn("best_1027_640x480.onnx", moved)
        self.assertTrue(os.path.exists(keep), "source_sha256 匹配的模型必须留下")
        self.assertTrue(os.path.exists(os.path.join(self.models, "old", "best_1027_640x480.bin")))

    def test_rejected_decision_prints_fallback_model(self):
        cfg = cm.load_gate_cfg(path="/nonexistent/a7.json")
        d = cm.decide(cfg, {"force": False, "has_bin": True, "onnx_ok": True, "cosine": 0.98})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cm.print_decision(d, dry_run=False)
        out = buf.getvalue()
        self.assertIn("仍在使用上一版模型", out)
        self.assertIn("不部署", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)