#!/usr/bin/env python3
"""
一键模型转换: 训练好的 YOLOv8 .pt  ->  RDK X5 BPU .bin  (同步类别、校验精度、自动切换)

====================== 板上用法 (推荐: SSH 进 X5, 换 .pt, 一条命令) ======================
    1. 把训练好的 best.pt 覆盖 vision/models/best.pt (文件名随意, 取 models/ 里最新的 .pt)
    2. ./convert.sh
       -> 板上导出 ONNX、与 .pt 逐框对拍、同步 classes.json, 立即可用 (CPU 推理)
       -> 自动把 ONNX + 校准数据发给笔记本 (WSL) 编译, 取回 .bin, 板上与 ONNX 对比, 一致就切到 BPU
    笔记本只需一次性配置 (见 tools/GUIDE.md "笔记本编译机"):
       ./convert.sh --setup-remote 用户名@笔记本IP            (Windows + WSL, 默认)
       ./convert.sh --setup-remote 用户名@笔记本IP --remote-type linux
    为什么要笔记本: 地瓜编译器 hb_mapper 只有 x86_64 版本 (pip 包 rdkx5-yolo-mapper 只提供
    manylinux x86_64 wheel), ARM 的 X5 上无法编译。笔记本不在线时停在 ONNX, 下次再 ./convert.sh 补编译。

====================== 电脑上用法 (x86 Linux / WSL 直接编译) ======================
    python3 tools/convert_model.py best.pt --calib 现场图片目录 --deploy root@192.168.1.10:/板上工程目录
    python3 tools/convert_model.py best.pt --onnx-only

步骤 (每一步失败都会停下并说明原因):
    1. 读 .pt 的类别名, 检查任务类型/类别数
    2. 按地瓜官方 Model Zoo 方式导出 "6 头" ONNX (每个 stride: cls(1,H,W,nc) + bbox(1,H,W,64), NHWC)
    3. ONNX 解码 与 ultralytics 原生预测逐框对拍
    4. 同步 vision/classes.json (顺序跟模型走, 已有中文名/颜色/高度保留) 和 tools/data.yaml
    5. 判定 (下面 6~9 步的度量) —— **判定通过之前不碰 vision/models 任何文件**
    6. 用真实现场图片生成量化校准数据 (letterbox114 -> RGB -> CHW uint8);
       有 dataset/hard_cases 时优先抽取并尽量每类覆盖, 上限/下限见 tools/convert_config.json
    7. 编译: 本机 hb_mapper / OE docker / 远程笔记本 (march=bayes-e, nv12, data_scale=1/255)
    8. 解析余弦相似度, < cosine_min (默认 0.99) 则**默认不部署** (--force 可强制)
    9. 有 BPU 时与 ONNX 结果对比, 一致率 < consistency_min (默认 0.85) 同样不部署
   10. 判定通过后才动 vision/models: 旧模型移到 models/old/, 放入 ONNX/.bin 与元数据
   11. --deploy 时 scp 到板子 (电脑上用)

    判定不通过时: 一个文件都不写/不移动, 打印 "仍在使用上一版模型: <路径>" 后退出 (码 3)。
    这样不会出现 "旧模型已移走、新模型未部署" 的空档。
    阈值全部走 tools/convert_config.json, 并可用环境变量覆盖:
      calib_max_images=IS_CALIB_MAX  calib_min_images=IS_CALIB_MIN
      cosine_min=IS_COSINE_MIN       consistency_min=IS_CONSISTENCY_MIN

imgsz 默认取 vision_config.json 的相机分辨率 (640x480 -> 480 640, 无灰边, 比 640x640 少 25% 计算)。
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import io

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
VISION = os.path.join(ROOT, "vision")
MODELS = os.path.join(VISION, "models")
CLASSES_JSON = os.path.join(VISION, "classes.json")
DATA_YAML = os.path.join(ROOT, "tools", "data.yaml")
CONVERT_CFG = os.path.join(ROOT, "tools", "convert_config.json")
DATASET = os.path.join(ROOT, "dataset")
HARD_CASES_DIR = os.path.join(DATASET, "hard_cases")
REGRESSION_DIR = os.path.join(DATASET, "regression")
SSH_DIR = os.path.join(ROOT, "tools", ".convert_ssh")
SSH_KEY = os.path.join(SSH_DIR, "id_ed25519")
DEFAULT_DOCKER = "openexplorer/ai_toolchain_ubuntu_20_x5_cpu:v1.2.8"
IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp")
ON_BOARD = platform.machine().lower() in ("aarch64", "arm64")
TOTAL_STEPS = 9


def step(n, msg):
    print(f"\n\033[1;36m[{n}/{TOTAL_STEPS}] {msg}\033[0m", flush=True)


def die(msg, code=1):
    print(f"\n\033[1;31m✘ {msg}\033[0m", flush=True)
    sys.exit(code)


def warn(msg):
    print(f"\033[1;33m! {msg}\033[0m", flush=True)


def ok(msg):
    print(f"\033[1;32m✔ {msg}\033[0m", flush=True)


def letterbox(img, w, h):
    ih, iw = img.shape[:2]
    s = min(w / iw, h / ih)
    nw, nh = int(round(iw * s)), int(round(ih * s))
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
    c = np.full((h, w, 3), 114, np.uint8)
    x, y = (w - nw) // 2, (h - nh) // 2
    c[y:y + nh, x:x + nw] = r
    return c


def list_images(d):
    if not d or not os.path.isdir(d):
        return []
    return sorted(f for f in glob.glob(os.path.join(d, "**", "*"), recursive=True)
                  if f.lower().endswith(IMG_EXT) and "/ann/" not in f.replace("\\", "/"))


def default_image_dirs():
    dirs = [os.path.join(ROOT, "tools", "calib_images")]
    try:
        from vision.config import load_vision_config
        d = load_vision_config().record.get("dir", "records/scans")
        dirs.append(os.path.join(d if os.path.isabs(d) else os.path.join(ROOT, d), "raw"))
    except Exception:
        dirs.append(os.path.join(ROOT, "records", "scans", "raw"))
    return dirs


def default_imgsz():
    try:
        from vision.config import load_vision_config
        cam = load_vision_config().camera
        w, h = int(cam.get("width", 640)), int(cam.get("height", 480))
    except Exception:
        w, h = 640, 480
    s = 640.0 / max(w, h)                         # 32 的倍数, 长边 640
    return [int(round(h * s / 32) * 32), int(round(w * s / 32) * 32)]


def sha16(path):
    from vision.yolo_engine import file_sha16
    return file_sha16(path)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ============================================================================ 1.5 部署门禁 (A7)
# 阈值来源: tools/convert_config.json  <  环境变量  <  命令行。改配置即可调参, 不许写死在代码里。
GATE_ENV = {"calib_max_images": "IS_CALIB_MAX", "calib_min_images": "IS_CALIB_MIN",
            "cosine_min": "IS_COSINE_MIN", "consistency_min": "IS_CONSISTENCY_MIN"}
GATE_DEFAULTS = {"calib_max_images": 200, "calib_min_images": 20,
                 "cosine_min": 0.99, "consistency_min": 0.85,
                 "hard_cases_dir": "dataset/hard_cases", "regression_dir": "dataset/regression"}
GATE_FLOAT = ("cosine_min", "consistency_min")
#: 写进模型旁 json 时由本脚本维护的字段; 其它字段 (别人加的) 原样保留
META_KEYS = {"names", "imgsz", "source_pt", "source_sha256", "created", "layout", "input",
             "cosine", "bpu_check", "regression", "decision"}


def load_gate_cfg(path=None):
    """门禁阈值: 默认值 -> 配置文件 -> 环境变量 (环境变量优先)。纯读配置, 不联网。"""
    cfg = dict(GATE_DEFAULTS)
    try:
        with open(path or CONVERT_CFG, encoding="utf-8") as f:
            raw = json.load(f)
        for k in GATE_DEFAULTS:
            if raw.get(k) is not None:
                cfg[k] = raw[k]
    except FileNotFoundError:
        pass
    except Exception as e:
        warn(f"{path or CONVERT_CFG} 解析失败, 用默认门禁阈值: {e}")
    for k, env in GATE_ENV.items():
        v = os.environ.get(env)
        if v is None or v == "":
            continue
        try:
            cfg[k] = float(v) if k in GATE_FLOAT else int(float(v))
        except ValueError:
            warn(f"环境变量 {env}={v!r} 不是数字, 忽略 (沿用 {cfg[k]})")
    return cfg


class DeployDecision:
    """纯决策结果 (不碰文件系统)。deploy=False 时调用方必须一个文件都不写、不移动。"""

    def __init__(self, deploy, deploy_bin=False, reasons=None, notes=None):
        self.deploy = bool(deploy)
        self.deploy_bin = bool(deploy_bin)
        self.reasons = list(reasons or [])
        self.notes = list(notes or [])

    def __bool__(self):
        return self.deploy

    def __repr__(self):
        return (f"DeployDecision(deploy={self.deploy}, deploy_bin={self.deploy_bin}, "
                f"reasons={self.reasons}, notes={self.notes})")


def decide(cfg, m):
    """
    纯函数 (不读文件/不联网/不看硬件): 由门禁阈值 + 度量决定是否部署。

    cfg: load_gate_cfg() 的结果 (cosine_min / consistency_min)
    m:   force         bool          --force (绕过余弦/一致率门禁, 仍不免除 .bin 加载失败)
         onnx_ok       bool          ONNX 解码与源模型对拍是否通过
         has_bin       bool          本次是否有 .bin 可部署 (无编译器 / --onnx-only 时 False)
         bpu_load_ok   bool | None   .bin 是否能在板端加载并验证 (None = 没做这项检查)
         bpu_error     str
         cosine        float | None  所有输出里最低的余弦相似度 (None = 日志里没解析到)
         consistency   float | None  BPU 与 ONNX 的一致率 hit/tot (None = 没做这项检查)
    """
    force = bool(m.get("force"))
    reasons, notes = [], []
    if not m.get("onnx_ok", True):
        reasons.append("ONNX 解码与源模型对拍未通过")
    if not m.get("has_bin"):
        notes.append("本次没有 .bin (无编译器 / --onnx-only), 只部署 ONNX")
        return DeployDecision(not reasons, deploy_bin=False, reasons=reasons, notes=notes)
    if m.get("bpu_load_ok") is False:
        reasons.append("BPU .bin 无法加载或验证: %s" % (m.get("bpu_error") or "未知原因"))
    cosine = m.get("cosine")
    if cosine is None:
        notes.append("未能从编译日志解析余弦相似度, 本次不做余弦门禁")
    elif cosine < cfg["cosine_min"]:
        if force:
            notes.append(f"余弦相似度 {cosine:.4f} < {cfg['cosine_min']}, --force 强制部署")
        else:
            reasons.append(f"余弦相似度 {cosine:.4f} < {cfg['cosine_min']}")
    cons = m.get("consistency")
    if cons is None:
        notes.append("未做 BPU/ONNX 一致率检查")
    elif cons < cfg["consistency_min"]:
        if force:
            notes.append(f"BPU/ONNX 一致率 {cons:.3f} < {cfg['consistency_min']}, --force 强制部署")
        else:
            reasons.append(f"BPU 与 ONNX 一致率 {cons:.3f} < {cfg['consistency_min']}")
    ok_ = not reasons
    return DeployDecision(ok_, deploy_bin=ok_, reasons=reasons, notes=notes)


def scan_hard_cases(d):
    """dataset/hard_cases: 子目录名当类别名 -> {类名: [图片]}; 顶层散图归入 ""。没有目录返回 {}。"""
    if not d or not os.path.isdir(d):
        return {}
    out = {}
    for sd in sorted(x for x in glob.glob(os.path.join(d, "*")) if os.path.isdir(x)):
        fs = list_images(sd)
        if fs:
            out[os.path.basename(sd)] = fs
    top = [f for f in list_images(d) if os.path.dirname(f) == os.path.normpath(d)]
    if top:
        out.setdefault("", []).extend(top)
    return out


def plan_calib(cfg, general, hard_by_class=None):
    """
    纯函数 (只处理路径, 不读图): 选校准图。
    有 hard_cases 时优先, 按类别轮询 -> 尽量每类覆盖; 再用普通图补足; 最多 cfg["calib_max_images"]。
    返回 (选中列表, {类别: 选中张数})。张数不足下限由调用方判断 (见 calib_shortage)。
    """
    max_n = max(1, int(cfg.get("calib_max_images", 200)))
    picked, seen, per_class = [], set(), {}

    def take(f, cls):
        if f in seen or len(picked) >= max_n:
            return False
        seen.add(f)
        picked.append(f)
        per_class[cls or "?"] = per_class.get(cls or "?", 0) + 1
        return True

    buckets = [(c, list(hard_by_class[c])) for c in sorted(hard_by_class or {})]
    i = 0
    while len(picked) < max_n and any(i < len(b) for _, b in buckets):
        for c, b in buckets:
            if i < len(b):
                take(b[i], c)
        i += 1
    for f in general:
        take(f, "")
    return picked, per_class


def calib_shortage(cfg, n):
    """校准图片是否少于下限 (calib_min_images, 默认 20)。纯函数。"""
    return int(n) < int(cfg.get("calib_min_images", 20))


def labeled_images(d):
    """dataset/regression/<类别名>/ 下的图片 -> [(路径, 类别名)]。没有目录返回 []。"""
    if not d or not os.path.isdir(d):
        return []
    pairs = []
    for sd in sorted(x for x in glob.glob(os.path.join(d, "*")) if os.path.isdir(x)):
        cls = os.path.basename(sd)
        for f in list_images(sd):
            pairs.append((f, cls))
    return pairs


def class_accuracy(preds, truth):
    """
    纯函数: preds=[(路径, 预测类别名 or None)], truth={路径: 真值类别名}。
    返回 {"total","correct","no_det","acc","per_class"}; 没有样本时 acc=None。
    """
    per, correct, no_det = {}, 0, 0
    for f, pred in preds:
        gt = truth.get(f)
        if gt is None:
            continue
        st = per.setdefault(gt, {"n": 0, "hit": 0})
        st["n"] += 1
        if pred is None:
            no_det += 1
        elif pred == gt:
            st["hit"] += 1
            correct += 1
    total = sum(v["n"] for v in per.values())
    return {"total": total, "correct": correct, "no_det": no_det,
            "acc": round(correct / total, 4) if total else None, "per_class": per}


def load_manifest_safe(model_path):
    """读模型旁 json; 读不到就返回 {} (不抛异常, 便于纯逻辑复用)。"""
    if not model_path:
        return {}
    try:
        from vision.yolo_engine import load_model_manifest
        return load_model_manifest(model_path)
    except Exception:
        try:
            with open(os.path.splitext(model_path)[0] + ".json", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def current_model_path():
    """当前实际在用的模型 (供 "仍在使用上一版模型: <path>" 打印)。只读。"""
    try:
        from vision.yolo_engine import resolve_model_path
        return resolve_model_path()
    except Exception:
        return None


def print_decision(d, dry_run=False):
    """把决策 (通过/不通过) 打印出来。不写文件。"""
    if d.deploy:
        print("  判定: 可以部署" + (" (.bin + ONNX)" if d.deploy_bin else " (只部署 ONNX)"))
    else:
        warn("门禁不通过, 本次不部署 (vision/models 下文件未改动)")
    for r in d.reasons:
        print(f"  · 不通过: {r}")
    for n in d.notes:
        print(f"  · {n}")
    if not d.deploy:
        print(f"  仍在使用上一版模型: {current_model_path() or '(vision/models 下没有可加载模型)'}")
        print("  候选模型留在 tools/build/ 下, 修好上面问题后重跑 ./convert.sh (或 --force 强制部署)")


def apply_decision(d, paths, meta, dry_run=False):
    """
    只有决策通过 (且非 dry-run) 才动 models/: 归档旧模型 -> 放 ONNX -> 放 .bin -> 写元数据。
    否则一个文件都不动, 返回 {"applied": False, "moved": [], "written": []}。
    paths: {"models_dir","onnx_src","onnx_dst","bin_src","bin_dst","keep_sha"}
    """
    res = {"applied": False, "moved": [], "written": []}
    if dry_run or not d.deploy:
        return res
    models_dir = paths.get("models_dir") or MODELS
    os.makedirs(models_dir, exist_ok=True)
    res["moved"] = retire_old_models(paths["keep_sha"], models_dir)
    meta = dict(meta)
    meta["decision"] = {"deploy": True, "deploy_bin": bool(d.deploy_bin), "notes": list(d.notes)}
    for src, dst in ((paths.get("onnx_src"), paths.get("onnx_dst")),
                     (paths.get("bin_src"), paths.get("bin_dst"))):
        if not src or not dst or not os.path.exists(src):
            continue
        shutil.copy(src, dst)
        res["written"].append(dst)
        old = {k: v for k, v in load_manifest_safe(dst).items() if k not in META_KEYS}
        old.update(meta)
        write_json(os.path.splitext(dst)[0] + ".json", old)
        res["written"].append(os.path.splitext(dst)[0] + ".json")
    res["applied"] = True
    return res


def _abs_dir(p):
    """配置/命令行给的目录: 相对路径按工程根解析。"""
    p = p or ""
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def _fmt_per_class(per_class):
    if not per_class:
        return "(无)"
    return ", ".join(f"{'未分类' if k == '?' else k}={v}" for k, v in sorted(per_class.items()))


def apply_classes(names, no_sync=False):
    """同步 vision/classes.json / tools/data.yaml (原文件先备份成 .bak)。"""
    if no_sync:
        print("  classes.json 同步: 跳过 (--no-sync-classes)")
        return
    added, unused = sync_classes(names)
    print("  classes.json 同步完成 (原文件备份为 classes.json.bak)")
    if added:
        warn(f"新类别 {added} 已用默认元数据补齐: 请在 classes.json 填 shape_cn(中文名)/color/height_mm")
    if unused:
        print(f"  模型中已不存在的类别移到 _unused_classes: {unused}")


# ============================================================================ 2. 导出
def export_onnx(pt, names, h, w, out_path, opset=11):
    import torch
    from ultralytics import YOLO
    from ultralytics.nn.modules.head import Detect

    def bpu_forward(self, x):          # 与地瓜官方 export_monkey_patch.py 的 Detect_forward 相同
        res = []
        for i in range(self.nl):
            res.append(self.cv3[i](x[i]).permute(0, 2, 3, 1).contiguous())   # cls  (1,H,W,nc) logits
            res.append(self.cv2[i](x[i]).permute(0, 2, 3, 1).contiguous())   # bbox (1,H,W,64) DFL
        return res

    model = YOLO(pt).model.float().eval()
    for m in model.modules():
        if type(m) is Detect:
            m.forward = bpu_forward.__get__(m, Detect)
    outs = []
    for s in (8, 16, 32):
        outs += [f"cls_s{s}", f"bbox_s{s}"]
    kw = dict(opset_version=opset, input_names=["images"], output_names=outs, do_constant_folding=True)
    dummy = torch.zeros(1, 3, h, w)
    with torch.no_grad():
        try:
            torch.onnx.export(model, dummy, out_path, dynamo=False, **kw)
        except TypeError:
            torch.onnx.export(model, dummy, out_path, **kw)
    import onnx
    mo = onnx.load(out_path)
    try:
        from onnxsim import simplify
        mo, _ = simplify(mo)
    except Exception:
        pass
    for k, v in (("names", str({i: n for i, n in enumerate(names)})), ("imgsz", str([h, w]))):
        p = mo.metadata_props.add()
        p.key, p.value = k, v
    onnx.save(mo, out_path)


# ============================================================================ 3. 对拍
def _match(a, b, iou_thr):
    """a, b: Detection 列表。返回 b 中能在 a 里找到同类且 IoU>thr 的个数。"""
    hit = 0
    for d in b:
        for e in a:
            if e.cls_id != d.cls_id:
                continue
            ix = max(0, min(d.x2, e.x2) - max(d.x1, e.x1)) * max(0, min(d.y2, e.y2) - max(d.y1, e.y1))
            u = d.w * d.h + e.w * e.h - ix
            if u > 0 and ix / u > iou_thr:
                hit += 1
                break
    return hit


def verify_onnx(pt, onnx_path, names, images, conf=0.35):
    from ultralytics import YOLO
    from vision.yolo_engine import YoloEngine
    from vision.interfaces import Detection
    eng = YoloEngine(model_path=onnx_path, class_names=names, conf_thresh=conf,
                     iou_thresh=0.7, agnostic_nms=False, max_det=100, backend="onnx")
    if not eng.is_ready:
        die(f"ONNX 加载失败: {eng.last_error}")
    ref = YOLO(pt)
    tot = hit = 0
    for f in images[:8]:
        img = cv2.imread(f)
        if img is None:
            continue
        a = eng.infer(img)
        r = ref.predict(img, imgsz=[eng.input_h, eng.input_w], conf=conf, iou=0.7, verbose=False)[0]
        b = [Detection(int(c), "", 1.0, *[float(v) for v in xy])
             for c, xy in zip(r.boxes.cls.tolist(), r.boxes.xyxy.tolist())]
        tot += len(b)
        hit += _match(a, b, 0.9)
    return tot, hit


def verify_bin(bin_path, onnx_path, names, images, conf=0.35):
    """板上: BPU .bin 与 ONNX 结果对比 (量化误差 + 前处理 NV12 是否正确)。"""
    from vision.yolo_engine import YoloEngine
    eb = YoloEngine(model_path=bin_path, class_names=names, conf_thresh=conf, backend="bpu")
    if not eb.is_ready:
        return None, f"BPU 加载失败: {eb.last_error}"
    eo = YoloEngine(model_path=onnx_path, class_names=names, conf_thresh=conf, backend="onnx")
    if not eo.is_ready:
        return None, f"ONNX 加载失败: {eo.last_error}"
    tot = hit = 0
    ms = []
    for f in images[:10]:
        img = cv2.imread(f)
        if img is None:
            continue
        a = eb.infer(img)
        ms.append(sum(eb.last_ms.values()))
        b = eo.infer(img)
        tot += max(len(a), len(b))
        hit += _match(a, b, 0.7)
    return (tot, hit, float(np.mean(ms)) if ms else 0.0), ""


def run_regression(onnx_path, names, reg_dir=None):
    """有 dataset/regression 时, 用候选 ONNX 跑一遍类别正确率 (只读, 不写任何文件)。"""
    pairs = labeled_images(reg_dir or REGRESSION_DIR)
    if not pairs:
        return None
    try:
        from vision.yolo_engine import YoloEngine
        eng = YoloEngine(model_path=onnx_path, class_names=names, conf_thresh=0.35,
                         iou_thresh=0.7, agnostic_nms=False, max_det=100, backend="onnx")
        if not eng.is_ready:
            warn(f"回归测试跳过: ONNX 加载失败 {eng.last_error}")
            return None
        preds = []
        for f, _ in pairs:
            img = cv2.imread(f)
            if img is None:
                continue
            dets = eng.infer(img)
            best = max(dets, key=lambda x: x.conf) if dets else None
            preds.append((f, best.name if best is not None else None))
        res = class_accuracy(preds, dict(pairs))
        print(f"  类别正确率 {res['correct']}/{res['total']} = {res['acc']}"
              f" (未检出 {res['no_det']} 张)")
        return res
    except Exception as e:
        warn(f"回归测试跳过: {e}")
        return None


# ============================================================================ 4. 类别同步
def sync_classes(names):
    data = {}
    if os.path.exists(CLASSES_JSON):
        with open(CLASSES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        shutil.copy(CLASSES_JSON, CLASSES_JSON + ".bak")
    old = {c["id"]: c for c in data.get("classes", []) if isinstance(c, dict)}
    old.update({c["id"]: c for c in data.get("_unused_classes", []) if isinstance(c, dict) and c["id"] not in old})
    new_list, added = [], []
    for n in names:
        if n in old:
            new_list.append(old[n])
        else:
            added.append(n)
            new_list.append({"id": n, "shape_cn": n, "color": None, "height_mm": 30})
    unused = [c for k, c in old.items() if k not in names]
    data["classes"] = new_list
    if unused:
        data["_unused_classes"] = unused
    elif "_unused_classes" in data:
        del data["_unused_classes"]
    write_json(CLASSES_JSON, data)
    try:
        lines = open(DATA_YAML, encoding="utf-8").read().split("names:")[0]
        with open(DATA_YAML, "w", encoding="utf-8") as f:
            f.write(lines + "names:\n" + "".join(f"  {i}: {n}\n" for i, n in enumerate(names)))
    except Exception:
        pass
    return added, [c["id"] for c in unused]


def retire_old_models(keep_sha, models_dir=None):
    """不是当前 .pt 转出来的模型 (以及更早的 .pt) 移到 models/old/, 避免误加载。"""
    models_dir = models_dir or MODELS
    old_dir = os.path.join(models_dir, "old")
    moved = []
    for f in glob.glob(os.path.join(models_dir, "*")):
        if not os.path.isfile(f):
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext in (".bin", ".onnx"):
            if load_manifest_safe(f).get("source_sha256", "") == keep_sha:
                continue
        elif ext == ".pt":
            if sha16(f) == keep_sha:
                continue
        else:
            continue
        os.makedirs(old_dir, exist_ok=True)
        for p in (f, os.path.splitext(f)[0] + ".json"):
            if os.path.exists(p):
                shutil.move(p, os.path.join(old_dir, os.path.basename(p)))
        moved.append(os.path.basename(f))
    return moved


# ============================================================================ 6/7. 校准 + 编译
YAML_TMPL = """model_parameters:
  onnx_model: '{onnx}'
  march: "bayes-e"
  layer_out_dump: False
  working_dir: 'bpu_model_output'
  output_model_file_prefix: '{prefix}'
input_parameters:
  input_name: ""
  input_type_rt: 'nv12'
  input_type_train: 'rgb'
  input_layout_train: 'NCHW'
  norm_type: 'data_scale'
  scale_value: 0.003921568627451
calibration_parameters:
  cal_data_dir: './calib_data'
  cal_data_type: '{cal_dtype}'
  calibration_type: '{cal_type}'
  optimization: set_Softmax_input_int8,set_Softmax_output_int8{int16}
compiler_parameters:
  jobs: {jobs}
  compile_mode: 'latency'
  debug: False
  optimize_level: '{opt}'
"""


def prepare_calib(files, ws, h, w, dtype="uint8"):
    """
    校准数据 = 与板端一致的前处理 (letterbox114 -> RGB -> CHW), 归一化 (1/255) 在模型里做。
    uint8 与 float32 数值相同 (0~255), uint8 文件小 4 倍, 远程编译时传得快。
    files 已经是 plan_calib() 选好的列表 (张数与类别覆盖在这里不再改)。
    """
    d = os.path.join(ws, "calib_data")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    n = 0
    for f in files:
        img = cv2.imread(f)
        if img is None:
            continue
        x = cv2.cvtColor(letterbox(img, w, h), cv2.COLOR_BGR2RGB).transpose(2, 0, 1)[None]
        x.astype(np.float32 if dtype == "float32" else np.uint8).tofile(
            os.path.join(d, f"calib_{n:04d}.rgbchw"))
        n += 1
    return n


HB_CMDS = ["hb_mapper checker --model-type onnx --march bayes-e --model model.onnx",
           "hb_mapper makertbin --model-type onnx --config config.yaml"]


def run_compiler_local(ws, mode, image):
    if mode == "local":
        for c in HB_CMDS:
            print("$", c)
            if subprocess.call(c, shell=True, cwd=ws) != 0:
                die(f"编译失败, 日志见 {ws}")
        return
    uid = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else None
    inner = " && ".join(HB_CMDS)
    if uid:
        inner += f" ; chown -R {uid} /data"
    c = ["docker", "run", "--rm", "--shm-size=8g", "-v", f"{ws}:/data", "-w", "/data", image,
         "bash", "-lc", inner]
    print("$", " ".join(c))
    if subprocess.call(c) != 0:
        die(f"docker 编译失败 (镜像 {image} 是否已 docker load?), 日志见 {ws}")


def parse_cosine(ws):
    """
    从 hb_mapper 日志取每个模型输出的余弦相似度。
    日志末尾的输出对比表形如:  cls_s8   0.999561   0.0112   ...  (第一列数值是 Cosine Similarity)
    """
    logs = glob.glob(os.path.join(ws, "**", "*.log"), recursive=True)
    sims = {}
    pat = re.compile(r"\b((?:cls|bbox)_s\d+)\b(.*)")
    for lg in logs:
        try:
            lines = open(lg, encoding="utf-8", errors="ignore").read().splitlines()
        except Exception:
            continue
        for line in lines:
            m = pat.search(line)
            if not m:
                continue
            nums = [float(x) for x in re.findall(r"(?<![\w.])-?\d+\.\d+", m.group(2))]
            if nums and 0.0 <= nums[0] <= 1.0001:
                sims[m.group(1)] = nums[0]
    return sorted(sims.items())


# ============================================================================ 远程编译 (笔记本)
def load_remote_cfg(a=None):
    cfg = {"remote": "", "remote_type": "windows", "wsl_distro": "", "remote_dir": "~/rdk_convert",
           "remote_init": "", "ssh_port": 22}
    if os.path.exists(CONVERT_CFG):
        try:
            with open(CONVERT_CFG, encoding="utf-8") as f:
                cfg.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
        except Exception as e:
            warn(f"{CONVERT_CFG} 解析失败: {e}")
    if os.environ.get("IS_COMPILE_HOST"):
        cfg["remote"] = os.environ["IS_COMPILE_HOST"]
    if a is not None:
        if getattr(a, "remote", ""):
            cfg["remote"] = a.remote
        if getattr(a, "remote_type", ""):
            cfg["remote_type"] = a.remote_type
        if getattr(a, "wsl_distro", ""):
            cfg["wsl_distro"] = a.wsl_distro
    return cfg


def save_remote_cfg(cfg):
    out = {"_说明": [
        "一键转换的远程编译机 (笔记本)。由 ./convert.sh --setup-remote 生成, 可手改。",
        "remote: 用户名@IP;  remote_type: windows (Windows OpenSSH + WSL) | linux (Linux/WSL 内的 sshd)",
        "wsl_distro: Windows 下指定 WSL 发行版名 (wsl -l -v 查看), 空 = 默认发行版",
        "remote_init: 编译前执行的命令, 例 conda 环境: source ~/miniconda3/bin/activate rdk",
        "remote_dir: 笔记本上的临时工作目录 (在 WSL 里)"]}
    if os.path.exists(CONVERT_CFG):          # 保留别人写在配置里的 _ 注释键 (如 _门禁说明)
        try:
            with open(CONVERT_CFG, encoding="utf-8") as f:
                out.update({k: v for k, v in json.load(f).items() if k.startswith("_")})
        except Exception:
            pass
    out.update(cfg)
    write_json(CONVERT_CFG, out)


def _ssh_base(cfg):
    c = ["ssh", "-p", str(cfg.get("ssh_port") or 22),
         "-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/.cvt_ssh_%r@%h_%p",
         "-o", "ControlPersist=600", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15"]
    if os.path.exists(SSH_KEY):
        c += ["-i", SSH_KEY]
    return c + [cfg["remote"]]


def remote_argv(cfg, bash_cmd):
    """
    组装在笔记本上执行 bash 命令的 ssh 参数。
    bash_cmd 里不能有双引号和 $ (要穿过 Windows cmd 或远端 shell 两层解析)。
    windows: ssh 用户@IP  wsl [-d 发行版] -e bash -lc "命令"
    linux:   ssh 用户@IP  bash -lc "命令"
    """
    assert '"' not in bash_cmd and "$" not in bash_cmd, bash_cmd
    init = cfg.get("remote_init") or ""
    full = f"{init} && {bash_cmd}" if init else bash_cmd
    pre = []
    if cfg.get("remote_type", "windows") == "windows":
        pre = ["wsl"] + (["-d", cfg["wsl_distro"]] if cfg.get("wsl_distro") else []) + ["-e"]
    return _ssh_base(cfg) + pre + ["bash", "-lc", f'"{full}"']


def remote_run(cfg, bash_cmd, stdin=None, capture=False, timeout=None):
    argv = remote_argv(cfg, bash_cmd)
    try:
        return subprocess.run(argv, input=stdin, stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.STDOUT if capture else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, b"timeout", b"")
    except FileNotFoundError:
        die("没有 ssh 命令: 容器里执行 apt-get install -y openssh-client (provision 脚本已包含)")


def remote_probe(cfg, timeout=25):
    r = remote_run(cfg, "echo CVT_OK && (command -v hb_mapper || echo NO_HB_MAPPER) && python3 -V",
                   capture=True, timeout=timeout)
    out = (r.stdout or b"").decode(errors="ignore")
    return r.returncode == 0 and "CVT_OK" in out, ("NO_HB_MAPPER" not in out), out.strip()


def run_compiler_remote(ws, cfg, prefix):
    job = f"{cfg.get('remote_dir') or '~/rdk_convert'}/{prefix}"
    print(f"  编译机: {cfg['remote']} ({cfg.get('remote_type')}) 工作目录 {job}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for n in ("model.onnx", "config.yaml", "calib_data"):
            tf.add(os.path.join(ws, n), arcname=n)
    data = buf.getvalue()
    print(f"  上传 {len(data) / 1e6:.1f} MB ...", flush=True)
    r = remote_run(cfg, f"rm -rf {job} && mkdir -p {job} && cat > {job}/in.tgz && cd {job} && tar xzf in.tgz && rm in.tgz",
                   stdin=data, timeout=600)
    if r.returncode != 0:
        return False, "上传失败 (网络/ssh)"
    print("  远程编译中 (约 3~15 分钟, 输出实时显示) ...", flush=True)
    cmd = f"cd {job} && " + " && ".join(c + " 2>&1 | tail -n 40" if "checker" in c else c for c in HB_CMDS)
    r = remote_run(cfg, cmd + " && echo CVT_BUILD_OK")
    if r.returncode != 0:
        warn("远程编译返回非 0, 尝试取回日志")
    out = remote_run(cfg, f"cd {job} && tar czf - bpu_model_output *.log 2>/dev/null", capture=True, timeout=600)
    try:
        with tarfile.open(fileobj=io.BytesIO(out.stdout or b""), mode="r:gz") as tf:
            try:
                tf.extractall(ws, filter="data")
            except TypeError:                       # Python < 3.12
                tf.extractall(ws)
    except Exception as e:
        return False, f"取回结果失败: {e}"
    return bool(glob.glob(os.path.join(ws, "bpu_model_output", "*.bin"))), ""


def cmd_setup_remote(a):
    cfg = load_remote_cfg(a)
    if not cfg["remote"]:
        die("用法: ./convert.sh --setup-remote 用户名@笔记本IP [--remote-type windows|linux] [--wsl-distro Ubuntu-22.04]")
    if a.remote_init:
        cfg["remote_init"] = a.remote_init
    save_remote_cfg(cfg)
    print(f"已保存 {CONVERT_CFG}: {cfg['remote']} ({cfg['remote_type']})")
    os.makedirs(SSH_DIR, exist_ok=True)
    if not os.path.exists(SSH_KEY) and shutil.which("ssh-keygen"):
        subprocess.call(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "rdkx5-convert", "-f", SSH_KEY, "-q"])
    if not os.path.exists(SSH_KEY + ".pub"):
        warn("没有 ssh-keygen (apt-get install -y openssh-client), 跳过免密配置: 每次转换输一次密码")
    elif cfg["remote_type"] == "linux":
        subprocess.call(["ssh-copy-id", "-i", SSH_KEY + ".pub", "-p", str(cfg["ssh_port"]), cfg["remote"]])
    else:
        print("\n板子公钥 (配置免密后每次转换不用输密码):\n  " + open(SSH_KEY + ".pub").read().strip())
        print("\nWindows 上 (管理员 PowerShell) 添加公钥, 二选一:")
        print("  · 管理员账户:  Add-Content -Encoding ascii C:\\ProgramData\\ssh\\administrators_authorized_keys '<上面整行公钥>'")
        print("                 icacls C:\\ProgramData\\ssh\\administrators_authorized_keys /inheritance:r /grant Administrators:F /grant SYSTEM:F")
        print("  · 普通账户:    Add-Content -Encoding ascii $env:USERPROFILE\\.ssh\\authorized_keys '<上面整行公钥>'")
        print("  (不配也能用, 每次转换输一次 Windows 登录密码)")
    print("\n测试连接 ...")
    good, has_hb, out = remote_probe(cfg, timeout=60)
    print("  " + out.replace("\n", "\n  "))
    if not good:
        die("连不上编译机。检查: 笔记本和板子同一网络 / Windows 已开启 OpenSSH Server / 防火墙放行 22 端口 / "
            "WSL 已安装 (Windows 模式)。详见 tools/GUIDE.md")
    if not has_hb:
        if a.install_remote:
            print("在笔记本 WSL 里安装 rdkx5-yolo-mapper ...")
            r = remote_run(cfg, "python3 -m pip install --user -U rdkx5-yolo-mapper")
            good, has_hb, _ = remote_probe(cfg)
        if not has_hb:
            die("编译机没有 hb_mapper。在笔记本 WSL (Ubuntu 22.04, 自带 Python 3.10) 里执行:\n"
                "   sudo apt install -y python3-pip && python3 -m pip install --user rdkx5-yolo-mapper\n"
                "或在板子上: ./convert.sh --setup-remote ... --install-remote")
    ok("编译机就绪。以后换 .pt 后直接 ./convert.sh")


# ============================================================================ main
def main():
    global TOTAL_STEPS
    ap = argparse.ArgumentParser(description="YOLOv8 .pt -> RDK X5 .bin 一键转换")
    ap.add_argument("pt", nargs="?", default="", help="训练得到的 .pt; 板上不填 = vision/models/ 里最新的 .pt")
    ap.add_argument("--imgsz", type=int, nargs=2, metavar=("H", "W"), default=None,
                    help="模型输入尺寸, 默认按相机比例 (640x480 -> 480 640)")
    ap.add_argument("--calib", default="", help="量化校准图片目录 (现场真实画面 20~100 张). "
                    "默认依次尝试 tools/calib_images, records/scans/raw (比赛识别时自动保存的原图)")
    ap.add_argument("--calib-max", type=int, default=None,
                    help="校准图片张数上限 (默认取 tools/convert_config.json 的 calib_max_images, "
                         "环境变量 IS_CALIB_MAX 可覆盖)")
    ap.add_argument("--cal-float32", action="store_true", help="校准数据用 float32 (默认 uint8, 数值相同)")
    ap.add_argument("--name", default="", help="输出模型名前缀, 默认取 .pt 文件名")
    ap.add_argument("--onnx-only", action="store_true", help="只导出并验证 ONNX")
    ap.add_argument("--force", action="store_true",
                    help="即使已转换过也重新转换; 并绕过部署门禁 (余弦/一致率不达标也强制部署)")
    ap.add_argument("--compiler", choices=["auto", "local", "docker", "remote", "none"], default="auto",
                    help="auto: x86 上本机 hb_mapper > docker; ARM 板上 = remote (已配置时)")
    ap.add_argument("--docker-image", default=os.environ.get("OE_DOCKER_IMAGE", DEFAULT_DOCKER))
    ap.add_argument("--remote", default="", help="远程编译机 用户名@IP (覆盖 tools/convert_config.json)")
    ap.add_argument("--remote-type", default="", choices=["", "windows", "linux"])
    ap.add_argument("--wsl-distro", default="")
    ap.add_argument("--remote-init", default="", help="远程编译前执行的命令 (如激活 conda 环境)")
    ap.add_argument("--setup-remote", nargs="?", const="__cfg__", default=None, metavar="用户名@IP",
                    help="一次性配置远程编译机 (笔记本)")
    ap.add_argument("--install-remote", action="store_true", help="--setup-remote 时自动在笔记本装 rdkx5-yolo-mapper")
    ap.add_argument("--int16", action="store_true", help="全 int16 量化 (更准, 更慢)")
    ap.add_argument("--cal-type", default="default", choices=["default", "max", "kl", "mix"])
    ap.add_argument("--opt", default="O3")
    ap.add_argument("--deploy", default=os.environ.get("BOARD", ""),
                    help="(电脑上用) 部署到板子: 用户@IP:板上工程根目录")
    ap.add_argument("--no-sync-classes", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="只做只读门禁判定 (不导出/不编译, 不写 vision/models 下任何文件) 并退出")
    ap.add_argument("--hard-cases", default="", metavar="目录",
                    help="难例图片目录, 优先用作校准图 (默认取 tools/convert_config.json 的 hard_cases_dir)")
    ap.add_argument("--regression", default="", metavar="目录",
                    help="回归图片目录 (每类一个子目录), 跑类别正确率 (默认取 regression_dir)")
    ap.add_argument("--no-regression", action="store_true", help="跳过回归正确率测试")
    ap.add_argument("--assume-cosine", type=float, default=None, metavar="V",
                    help="[仅 --dry-run] 用模拟的余弦相似度 V 代替待编译的值 (不产生实测数据)")
    ap.add_argument("--assume-consistency", type=float, default=None, metavar="V",
                    help="[仅 --dry-run] 用模拟的 BPU/ONNX 一致率 V 代替待编译的值 (不产生实测数据)")
    a = ap.parse_args()

    if a.setup_remote is not None:
        if a.setup_remote != "__cfg__":
            a.remote = a.setup_remote
        cmd_setup_remote(a)
        return

    # ------------------------------------------------------------ 找 .pt
    if a.pt:
        pt = os.path.abspath(a.pt)
    else:
        pts = sorted(glob.glob(os.path.join(MODELS, "*.pt")), key=os.path.getmtime, reverse=True)
        if not pts:
            die("vision/models/ 里没有 .pt。把训练好的 best.pt 放进去再运行 (或: ./convert.sh 路径/best.pt)")
        pt = pts[0]
    if not os.path.isfile(pt):
        die(f"找不到 {pt}")
    if ON_BOARD and os.path.dirname(pt) != MODELS:
        dst = os.path.join(MODELS, os.path.basename(pt))
        os.makedirs(MODELS, exist_ok=True)
        shutil.copy2(pt, dst)
        print(f"  已复制到 {dst}")
        pt = dst
    sha = sha16(pt)
    h, w = a.imgsz or default_imgsz()
    if h % 32 or w % 32:
        die("imgsz 必须是 32 的倍数")
    name = a.name or os.path.splitext(os.path.basename(pt))[0]
    prefix = f"{name}_bayese_{w}x{h}_nv12"
    onnx_dst = os.path.join(MODELS, f"{name}_{w}x{h}.onnx")
    bin_dst = os.path.join(MODELS, prefix + ".bin")
    ws = os.path.join(ROOT, "tools", "build", prefix)

    from vision.yolo_engine import load_model_manifest
    have_onnx = os.path.exists(onnx_dst) and load_model_manifest(onnx_dst).get("source_sha256") == sha
    have_bin = os.path.exists(bin_dst) and load_model_manifest(bin_dst).get("source_sha256") == sha
    print(f"源模型 {os.path.relpath(pt, ROOT)}  指纹 {sha}  输入 {h}x{w}"
          f"  ({'ARM 板上' if ON_BOARD else platform.machine()})")

    # ------------------------------------------------------------ A7 部署门禁: 纯只读判定 (--dry-run)
    gate = load_gate_cfg()
    if a.calib_max is not None:
        gate["calib_max_images"] = a.calib_max
    if a.hard_cases:
        gate["hard_cases_dir"] = a.hard_cases
    if a.regression:
        gate["regression_dir"] = a.regression

    if a.dry_run:
        hard = scan_hard_cases(_abs_dir(gate["hard_cases_dir"]))
        images, src = (list_images(a.calib), a.calib) if a.calib else ([], "")
        if not images:
            for d in default_image_dirs():
                images = list_images(d)
                if images:
                    src = d
                    break
        picked, per_class = plan_calib(gate, images, hard)
        print("== 部署门禁判定 (--dry-run, 只读: 不写/不移动 vision/models 下任何文件) ==")
        print(f"  校准图来源 {src or '(未找到)'}: 共 {len(images)} 张; "
              f"hard_cases {sum(len(v) for v in hard.values())} 张 / {len(hard)} 类")
        print(f"  选中 {len(picked)} 张 (上限 {gate['calib_max_images']}, 下限 {gate['calib_min_images']}); "
              f"每类覆盖: {_fmt_per_class(per_class)}")
        if calib_shortage(gate, len(picked)):
            warn(f"校准图不足下限 {gate['calib_min_images']} 张 (现在 {len(picked)} 张)")
        sims = parse_cosine(ws)
        if a.assume_cosine is not None:
            cosine, has_bin = a.assume_cosine, True
            print(f"  [dry-run] 余弦相似度 {cosine:.4f} (命令行模拟值, 无实测数据)")
        else:
            cosine = min(v for _, v in sims) if sims else None
            has_bin = bool(sims)
            if sims:
                print(f"  [dry-run] 余弦相似度 {cosine:.4f} (读自已有编译日志 {os.path.relpath(ws, ROOT)})")
        cons = a.assume_consistency
        if cons is not None:
            has_bin = True
            print(f"  [dry-run] BPU/ONNX 一致率 {cons:.3f} (命令行模拟值, 无实测数据)")
        print(f"  阈值 cosine_min={gate['cosine_min']}  consistency_min={gate['consistency_min']}  "
              f"force={bool(a.force)}")
        d = decide(gate, {"force": a.force, "has_bin": has_bin, "onnx_ok": True,
                          "cosine": cosine, "consistency": cons})
        print_decision(d, dry_run=True)
        return

    if have_bin and not a.force and not a.deploy:
        ok(f"已是最新: {os.path.basename(bin_dst)} 就是由这个 .pt 转换的 (重新转换加 --force)")
        return

    # ------------------------------------------------------------ 决定编译方式
    rcfg = load_remote_cfg(a)
    mode = a.compiler
    if mode == "auto":
        if not ON_BOARD and shutil.which("hb_mapper"):
            mode = "local"
        elif not ON_BOARD and shutil.which("docker"):
            mode = "docker"
        elif rcfg["remote"]:
            mode = "remote"
        else:
            mode = "none"
    if a.onnx_only:
        mode = "none"

    reuse = have_onnx and not a.force and os.path.exists(os.path.join(ws, "model.onnx"))
    try:
        from ultralytics import YOLO  # noqa
    except Exception as e:
        if not reuse:
            die(f"缺 ultralytics/torch ({e})。容器里: deploy/provision_intelligentstoring.sh 会自动安装; "
                "或 pip install ultralytics onnx")
    os.makedirs(ws, exist_ok=True)

    images = list_images(a.calib) if a.calib else []
    if not images:
        for d in default_image_dirs():
            images = list_images(d)
            if images:
                print(f"  校准/验证图片: {d} ({len(images)} 张)")
                break

    meta = {"names": [], "imgsz": [h, w], "source_pt": os.path.basename(pt), "source_sha256": sha,
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "layout": "6head cls,bbox NHWC", "input": "nv12 (bin) / rgb float (onnx)"}

    hard = scan_hard_cases(_abs_dir(gate["hard_cases_dir"]))
    if hard:
        print(f"  hard_cases: {len(hard)} 类共 {sum(len(v) for v in hard.values())} 张 (优先用作校准图)")
    calib_imgs, calib_per_class = plan_calib(gate, images, hard)
    if calib_imgs:
        print(f"  校准图: 选中 {len(calib_imgs)} 张 (上限 {gate['calib_max_images']}, "
              f"下限 {gate['calib_min_images']}); 每类覆盖: {_fmt_per_class(calib_per_class)}")
    # 类别同步也推迟到判定通过后 (避免"类别已同步但模型未部署")
    do_sync = not reuse and not a.no_sync_classes

    if reuse:
        print("  ONNX 已由这个 .pt 导出过, 跳过 1~5 步, 直接编译")
        meta.update({k: v for k, v in load_model_manifest(onnx_dst).items() if k in ("names", "created")})
        names = meta["names"]
    else:
        step(1, f"读取模型 {os.path.basename(pt)}")
        from ultralytics import YOLO
        y = YOLO(pt)
        if getattr(y, "task", "detect") != "detect":
            die(f"只支持检测模型, 当前 task={y.task}")
        names = [y.names[k] for k in sorted(y.names)]
        meta["names"] = names
        print(f"  类别 {len(names)}: {names}")
        if len(names) == 64:
            die("类别数恰好 64 时 cls 与 bbox(64 通道) 无法区分, 请加一个占位类重新训练")

        step(2, f"导出 6 头 ONNX  输入 {h}x{w}" + ("  (ARM CPU 上约 1~3 分钟)" if ON_BOARD else ""))
        onnx_path = os.path.join(ws, "model.onnx")
        export_onnx(pt, names, h, w, onnx_path)
        print("  ->", os.path.relpath(onnx_path, ROOT))

        step(3, "ONNX 解码 与 ultralytics 原生预测 对拍")
        if images:
            tot, hit = verify_onnx(pt, onnx_path, names, images)
            if tot == 0:
                warn("验证图片上没有检测到目标, 无法对拍 (请用含货物的现场图片)")
            else:
                print(f"  一致 {hit}/{tot} 框")
                if hit < 0.95 * tot:
                    die("ONNX 解码结果与原模型不一致, 停止 (请把 .pt 和本输出发给维护者)")
        else:
            warn("没有图片, 跳过对拍 (板上跑一次 vision_tool check 或比赛流程就会自动存图)")

        step(4, "同步 vision/classes.json / tools/data.yaml")
        print("  推迟到部署判定通过之后执行 (判定不通过时不动任何文件)")

        step(5, "准备候选模型 (判定通过后才写入 vision/models)")
        print("  候选 ONNX 在", os.path.relpath(onnx_path, ROOT),
              "; 判定通过前不动 vision/models 下任何文件")

    if mode == "none":
        # 只出 ONNX: 同样先判定再落盘 (不部署时不动 vision/models)
        d = decide(gate, {"force": a.force, "has_bin": False, "onnx_ok": True})
        if a.onnx_only and not d.deploy:
            print_decision(d, dry_run=False)
            sys.exit(3)
        if do_sync:
            apply_classes(names, no_sync=False)
        paths = {"models_dir": MODELS, "onnx_src": os.path.join(ws, "model.onnx"), "onnx_dst": onnx_dst,
                 "bin_src": None, "bin_dst": None, "keep_sha": sha}
        r = apply_decision(d, paths, meta)
        if r["moved"]:
            print(f"  旧模型移到 models/old/: {r['moved']}")
        for p in r["written"]:
            print("  ->", os.path.relpath(p, ROOT))
        if a.onnx_only:
            ok(f"已输出 {os.path.relpath(onnx_dst, ROOT)}")
        else:
            warn("没有可用的编译器, 停在 ONNX: 程序现在用 ONNX (CPU, 较慢) 推理, 结果正确。")
            if ON_BOARD:
                print("  配置笔记本编译机后再运行 ./convert.sh 即可补编译 .bin:\n"
                      "     ./convert.sh --setup-remote 用户名@笔记本IP")
            else:
                print("  安装编译器: pip install rdkx5-yolo-mapper (Ubuntu22/Python3.10) 或 OE docker")
        print("  重启比赛程序 (执行页「一键重启」) 后生效")
        return

    step(6, "生成量化校准数据")
    if calib_shortage(gate, len(calib_imgs)):
        die(f"校准需要至少 {gate['calib_min_images']} 张 (建议 {gate['calib_max_images']}) "
            "比赛相机拍的真实托盘图片: "
            "python3 tools/vision_tool.py capture --count 60  (换货物摆放/数量), 或用 --calib 指定目录。\n"
            "  ONNX 已可用, 补齐图片后再运行 ./convert.sh", code=3)
    dtype = "float32" if a.cal_float32 else "uint8"
    n = prepare_calib(calib_imgs, ws, h, w, dtype=dtype)
    print(f"  {n} 张 ({dtype}, 上限 {gate['calib_max_images']}) -> {os.path.relpath(ws, ROOT)}/calib_data")
    with open(os.path.join(ws, "config.yaml"), "w") as f:
        f.write(YAML_TMPL.format(onnx="model.onnx", prefix=prefix, cal_type=a.cal_type, cal_dtype=dtype,
                                 int16=",set_all_nodes_int16" if a.int16 else "",
                                 jobs=max(1, (os.cpu_count() or 2) // 2), opt=a.opt))

    step(7, f"编译 BPU 模型 (hb_mapper, march=bayes-e, {mode})")
    shutil.rmtree(os.path.join(ws, "bpu_model_output"), ignore_errors=True)
    if mode == "remote":
        if not rcfg["remote"]:
            die("没有配置远程编译机: ./convert.sh --setup-remote 用户名@笔记本IP")
        good, has_hb, out = remote_probe(rcfg)
        if not good:
            warn(f"连不上编译机 {rcfg['remote']}: {out[-200:]}")
            print("  ONNX 已可用 (CPU 推理)。笔记本开机联网后再运行 ./convert.sh 补编译")
            sys.exit(4)
        if not has_hb:
            die("编译机上没有 hb_mapper: ./convert.sh --setup-remote --install-remote")
        built, err = run_compiler_remote(ws, rcfg, prefix)
        if not built:
            die(f"远程编译没有生成 .bin {err}; 日志已取回到 {ws}")
    else:
        run_compiler_local(ws, mode, a.docker_image)
    bins = glob.glob(os.path.join(ws, "bpu_model_output", "*.bin"))
    if not bins:
        die(f"没有生成 .bin, 请看 {ws} 里的 hb_mapper 日志")

    step(8, "检查量化精度")
    sims = parse_cosine(ws)
    cosine = min(v for _, v in sims) if sims else None
    if sims:
        for k, v in sims:
            print(f"  {k}: cosine={v:.4f}"
                  + (f"   <-- 低于门禁 {gate['cosine_min']}" if v < gate["cosine_min"] else ""))
        if cosine < gate["cosine_min"]:
            warn(f"有输出余弦相似度 < {gate['cosine_min']}: 换更有代表性的校准图, "
                 "或 --cal-type max/kl, 或 --int16")
    else:
        warn("未能从日志解析余弦相似度, 请手动查看 hb_mapper_makertbin.log")
    meta["cosine"] = dict(sims)

    onnx_src = os.path.join(ws, "model.onnx")
    if not a.no_regression:
        reg = run_regression(onnx_src, meta.get("names") or names, _abs_dir(gate["regression_dir"]))
        if reg:
            meta["regression"] = reg

    step(9, "判定并启用 .bin")
    from vision.yolo_engine import is_bpu_available
    tmp = os.path.join(ws, "new.bin")          # 候选 .bin 先放工作区, 判定通过后才进 vision/models
    shutil.copy(bins[0], tmp)
    cons, bpu_ok, bpu_err = None, None, ""
    if ON_BOARD and is_bpu_available():
        res, err = verify_bin(tmp, onnx_src, meta.get("names") or names, images)
        if res is None:
            bpu_ok, bpu_err = False, err
            warn(f"{err} (.bin 候选未能上板加载)")
        else:
            tot, hit, ms = res
            cons = (float(hit) / tot) if tot else None
            bpu_ok = True
            print(f"  BPU 与 ONNX 一致 {hit}/{tot} 框"
                  + (f" (一致率 {cons:.3f})" if cons is not None else "") + f", BPU 每帧 {ms:.1f} ms")
            meta["bpu_check"] = {"match": f"{hit}/{tot}", "ms": round(ms, 1), "consistency": cons}
    elif ON_BOARD:
        warn("本机 BPU 运行库不可用, 无法上板验证 .bin (容器需挂载宿主运行库, 见 deploy/bpu_doctor.sh)")

    print(f"  门禁阈值 cosine_min={gate['cosine_min']}  consistency_min={gate['consistency_min']}  "
          f"force={bool(a.force)}")
    d = decide(gate, {"force": a.force, "has_bin": True, "onnx_ok": True,
                      "bpu_load_ok": bpu_ok, "bpu_error": bpu_err,
                      "cosine": cosine, "consistency": cons})
    if not d.deploy:
        print_decision(d, dry_run=False)
        sys.exit(3)

    if do_sync:
        apply_classes(names, no_sync=False)
    paths = {"models_dir": MODELS, "onnx_src": onnx_src, "onnx_dst": onnx_dst,
             "bin_src": bins[0], "bin_dst": bin_dst, "keep_sha": sha}
    r = apply_decision(d, paths, meta)
    print_decision(d, dry_run=False)
    if r["moved"]:
        print(f"  旧模型移到 models/old/: {r['moved']}")
    for p in r["written"]:
        print("  ->", os.path.relpath(p, ROOT))

    if a.deploy:
        print("\n部署到板子")
        if ":" not in a.deploy:
            die("--deploy 格式: 用户@IP:板上工程根目录")
        host, bd = a.deploy.split(":", 1)
        q = shlex.quote
        files = [bin_dst, os.path.splitext(bin_dst)[0] + ".json", onnx_dst, os.path.splitext(onnx_dst)[0] + ".json"]
        cmds = [["scp"] + files + [f"{host}:{q(bd + '/vision/models/')}"],
                ["scp", CLASSES_JSON, f"{host}:{q(bd + '/vision/classes.json')}"]]
        for c in cmds:
            print("$", " ".join(c))
            if subprocess.call(c) != 0:
                die("scp 失败, 请检查 IP/用户名/路径")
        print("  注意: 板子 vision/models/ 里若有别的 .pt, 会优先按最新 .pt 选模型; 建议把同一个 .pt 也拷过去")
    ok("转换完成。重启比赛程序 (执行页「一键重启」) 后使用 BPU")


if __name__ == "__main__":
    main()
