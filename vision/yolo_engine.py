"""
YOLOv8 统一推理引擎 (RDK X5 BPU 优先, ONNX Runtime / Ultralytics 兜底)
==========================================================================

后端优先级 (自动选择, 也可用 IS_YOLO_BACKEND=bpu|onnx|ultralytics 强制):
    1. bpu          *.bin  RDK X5 板端 BPU。运行库自动选择:
                           hbm_runtime (RDK OS >= 3.5, 地瓜官方 Model Zoo rdk_x5 分支使用)
                           -> hobot_dnn.pyeasy_dnn (旧系统)
    2. onnx         *.onnx onnxruntime CPU, PC 开发 / 对拍验证
    3. ultralytics  *.pt   仅调试用, 板端很慢

模型选择: 配置 model.path > IS_YOLO_MODEL > vision/models/ 自动选择 (见 resolve_model_path):
          以 models/ 里最新的 .pt 为准, 用它转换出的 .bin > .onnx; 还没转换时临时用 .pt 并提示
类别名:   模型旁的 <模型名>.json (tools/convert_model.py 生成) > ONNX 元数据 > classes.json

支持两种模型输出结构 (自动按输出张量形状识别, 与输出顺序无关):
    A. "BPU 6 头" (推荐, tools/export_bpu_onnx.py 导出):
         每个 stride(8/16/32) 两个 NHWC 张量:
             bbox: (1, H, W, 64)   DFL 原始分布 (reg_max=16)
             cls : (1, H, W, nc)   未经 sigmoid 的 logits
         DFL / sigmoid 都放到 CPU 上, 且只对过了阈值的少量格子做 ->
         BPU 不用跑 transpose/softmax, 后处理也只算几十个点, 是 X5 上最快的结构。
    B. 标准 Ultralytics 单输出: (1, 4+nc, N) 或 (1, N, 4+nc), 类别分数已 sigmoid。

修正旧版 rdk_x5_bpu_infer.py 的致命问题:
    * 旧版把 BGR uint8 图直接喂给 input_type_rt=nv12 的模型 -> 输入格式错, 结果全是噪声;
      本版按模型实际输入 (nv12 / bgr / rgb, NCHW / NHWC) 构造张量。
    * 旧版 letterbox 填 0 (训练时是 114), 有轻微分布偏移; 本版填 114。
    * 旧版逐框 Python 循环 + 按类 NMS; 本版全向量化 + 类无关 NMS
      (同一个工件被判成两个类别时只保留置信度高的那个, 避免重复抓取)。
    * 去掉 COCO 80 类硬编码, 类别名从 classes.json / 模型元数据读取。
"""

from __future__ import annotations

import ast
import glob
import json
import logging
import os
import time
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

from .interfaces import Detection, InferenceEngineBase

logger = logging.getLogger(__name__)

CUR_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(CUR_DIR, "models")

MODEL_ENV = "IS_YOLO_MODEL"
BACKEND_ENV = "IS_YOLO_BACKEND"
CONF_ENV = "IS_YOLO_CONF"
IOU_ENV = "IS_YOLO_IOU"


def _env_thresh(env: str, given, default: float, lo: float, hi: float, owner=None) -> float:
    """阈值: 环境变量 > 参数 > 默认; 越界/非数字退回默认并写 notice (自检里可见), 不再静默全空。"""
    raw = os.environ.get(env)
    src, val = (env, raw) if raw not in (None, "") else ("参数", given if given is not None else default)
    try:
        v = float(val)
    except (TypeError, ValueError):
        v = float("nan")
    if not (lo <= v <= hi):
        msg = f"{src} 阈值 {val!r} 不在 [{lo}, {hi}], 改用 {default}"
        logger.warning(msg)
        if owner is not None:
            owner.notice = (owner.notice + "; " if owner.notice else "") + msg
        return default
    return v

STRIDES = (8, 16, 32)
REG_MAX = 16


# ------------------------------------------------------------------------------
# 运行库探测 (全部惰性, 缺库不抛异常)
# ------------------------------------------------------------------------------

def _import_hobot_dnn():
    for mod in ("hobot_dnn", "hobot_dnn_rdkx5"):
        try:
            m = __import__(mod, fromlist=["pyeasy_dnn"])
            return getattr(m, "pyeasy_dnn")
        except Exception:
            continue
    try:  # 极老版本
        from hobot_dnn import pbdnn  # type: ignore
        return pbdnn
    except Exception:
        return None


def _import_hbm_runtime():
    try:
        import hbm_runtime  # type: ignore
        return hbm_runtime
    except Exception:
        return None


def is_bpu_available() -> bool:
    return _import_hbm_runtime() is not None or _import_hobot_dnn() is not None


# ------------------------------------------------------------------------------
# 前处理工具
# ------------------------------------------------------------------------------
# 注: Detection 已上移到 interfaces.py —— 它是引擎层的数据契约, 不属于某个
# 具体实现。此处仅 re-export, 保证 `from vision.yolo_engine import Detection`
# 仍然可用 (yolo_detector.py / 自定义引擎都依赖它)。


def letterbox(bgr: np.ndarray, in_w: int, in_h: int, pad_value: int = 114
              ) -> Tuple[np.ndarray, float, int, int]:
    """等比缩放 + 居中填充。返回 (图, scale, pad_x, pad_y)。尺寸一致时零拷贝直通。"""
    ih, iw = bgr.shape[:2]
    if iw == in_w and ih == in_h:
        return bgr, 1.0, 0, 0
    scale = min(in_w / iw, in_h / ih)
    nw, nh = int(round(iw * scale)), int(round(ih * scale))
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    resized = cv2.resize(bgr, (nw, nh), interpolation=interp)
    pad_x, pad_y = (in_w - nw) // 2, (in_h - nh) // 2
    if nw == in_w and nh == in_h:
        return resized, scale, 0, 0
    canvas = np.full((in_h, in_w, 3), pad_value, dtype=np.uint8)
    canvas[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
    return canvas, scale, pad_x, pad_y


def bgr_to_nv12(bgr: np.ndarray) -> np.ndarray:
    """BGR -> NV12 (Y 平面 + UV 交织), BPU nv12 输入专用。宽高须为偶数。"""
    h, w = bgr.shape[:2]
    yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420).reshape(-1)
    area = h * w
    y = yuv[:area]
    u = yuv[area:area + area // 4]
    v = yuv[area + area // 4:]
    uv = np.empty(area // 2, dtype=np.uint8)
    uv[0::2] = u
    uv[1::2] = v
    return np.concatenate([y, uv])


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _nms_xyxy(boxes: np.ndarray, scores: np.ndarray, iou_thr: float, max_det: int) -> List[int]:
    """纯 numpy NMS (类无关)。boxes: (N,4) xyxy。"""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: List[int] = []
    while order.size and len(keep) < max_det:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[rest] - inter + 1e-9)
        order = rest[iou <= iou_thr]
    return keep


# ------------------------------------------------------------------------------
# 模型 / 类别名解析
# ------------------------------------------------------------------------------

_SHA_CACHE: dict = {}
#: 最近一次自动选模型的说明 (self_check / 执行页显示), 例: "best.pt 尚未转换, 临时用 .pt CPU 推理"
MODEL_NOTICE = ""


def file_sha16(path: str) -> str:
    """源 .pt 指纹 (sha256 前 16 位, 与 convert_model.py 写入 json 的 source_sha256 一致)。"""
    import hashlib
    try:
        st = os.stat(path)
    except OSError:
        return ""
    key = (path, st.st_mtime, st.st_size)
    if key not in _SHA_CACHE:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _SHA_CACHE[key] = h.hexdigest()[:16]
    return _SHA_CACHE[key]


def _ultralytics_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("ultralytics") is not None


def resolve_model_path(explicit: Optional[str] = None) -> Optional[str]:
    """
    显式参数 > 环境变量 IS_YOLO_MODEL > vision/models/ 自动选择:

      * models/ 里有 .pt (训练得到的源模型) 时, 以最新的 .pt 为准:
          由它转换出的 .bin (有 BPU) > .onnx  (按 json 里的 source_sha256 对应)
          还没转换 -> 有 ultralytics 时临时直接用 .pt (CPU, 慢), 否则用旧模型, 并给出提示
      * 没有 .pt 时: 最新的 .bin (有 BPU) > .onnx
    """
    global MODEL_NOTICE
    MODEL_NOTICE = ""
    for p in (explicit, os.environ.get(MODEL_ENV)):
        if p:
            p = p if os.path.isabs(p) else os.path.join(CUR_DIR, p)
            if os.path.exists(p):
                return p
            logger.error("指定的模型不存在: %s", p)
            return None
    bpu = is_bpu_available()
    derived = []
    for ext in ((".bin",) if bpu else ()) + (".onnx",):
        for f in glob.glob(os.path.join(MODEL_DIR, "*" + ext)):
            derived.append((f, ext, load_model_manifest(f).get("source_sha256", "")))
    rank = {".bin": 0, ".onnx": 1}
    pts = sorted(glob.glob(os.path.join(MODEL_DIR, "*.pt")), key=os.path.getmtime, reverse=True)
    if pts:
        pt = pts[0]
        sha = file_sha16(pt)
        mine = [d for d in derived if d[2] == sha]
        if mine:
            mine.sort(key=lambda d: (rank[d[1]], -os.path.getmtime(d[0])))
            if mine[0][1] == ".onnx" and bpu:
                MODEL_NOTICE = f"{os.path.basename(pt)} 已导出 ONNX 但还没编译成 .bin, 当前 CPU 推理 (运行 ./convert.sh 补编译)"
            return mine[0][0]
        if _ultralytics_available():
            MODEL_NOTICE = f"{os.path.basename(pt)} 尚未转换, 临时直接用 .pt (CPU, 很慢)。请运行 ./convert.sh"
            logger.warning("YOLO: %s", MODEL_NOTICE)
            return pt
        if derived:
            MODEL_NOTICE = f"{os.path.basename(pt)} 尚未转换, 仍在用旧模型 (类别可能不对)。请运行 ./convert.sh"
            logger.warning("YOLO: %s", MODEL_NOTICE)
    if derived:
        derived.sort(key=lambda d: (rank[d[1]], -os.path.getmtime(d[0])))
        return derived[0][0]
    return pts[0] if pts else None


def load_model_manifest(model_path: Optional[str]) -> dict:
    """读取模型旁的 <name>.json (convert_model.py 写入: names / imgsz / 源 .pt 等)。"""
    if not model_path:
        return {}
    p = os.path.splitext(model_path)[0] + ".json"
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_class_names(meta_path: Optional[str] = None) -> List[str]:
    """从 classes.json 读类别名 (顺序 = 训练时的类别 id)。"""
    path = meta_path or os.path.join(CUR_DIR, "classes.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [c["id"] if isinstance(c, dict) else str(c) for c in data.get("classes", [])]
    except Exception:
        return []


# ------------------------------------------------------------------------------
# 引擎
# ------------------------------------------------------------------------------

class YoloEngine(InferenceEngineBase):
    """线程不安全: 同一实例请勿多线程并发 infer (VisionService 里已加锁)。"""

    def __init__(self, model_path: Optional[str] = None,
                 class_names: Optional[Sequence[str]] = None,
                 conf_thresh: Optional[float] = None,
                 iou_thresh: Optional[float] = None,
                 agnostic_nms: bool = True,
                 max_det: int = 40,
                 backend: Optional[str] = None,
                 class_conf: Optional[dict] = None):
        self.model_path = resolve_model_path(model_path)
        self.notice = MODEL_NOTICE if not model_path else ""
        self.manifest = load_model_manifest(self.model_path)
        names_from_model = list(self.manifest.get("names") or [])
        self.class_names: List[str] = names_from_model or list(class_names or [])
        if names_from_model and class_names and list(class_names) != names_from_model:
            logger.warning("模型类别 %s 与 classes.json %s 不一致, 以模型为准 "
                           "(请运行 tools/convert_model.py 自动同步 classes.json)",
                           names_from_model, list(class_names))
        self.class_conf = {k: float(v) for k, v in (class_conf or {}).items()}
        self.conf_thresh = _env_thresh(CONF_ENV, conf_thresh, 0.40, 0.05, 0.95, self)
        self.iou_thresh = _env_thresh(IOU_ENV, iou_thresh, 0.50, 0.10, 0.95, self)
        self.agnostic_nms = agnostic_nms
        self.max_det = max_det
        self.backend = ""
        self.is_ready = False
        self.last_error = ""
        self.input_w = 640
        self.input_h = 640
        self.input_kind = "rgb"       # nv12 | bgr | rgb
        self.input_layout = "NCHW"    # NCHW | NHWC
        self.last_ms = {"pre": 0.0, "infer": 0.0, "post": 0.0}

        self._model = None
        self._bpu_api = ""            # hbm | pyeasy
        self._hbm_name = ""
        self._hbm_in = ""
        self._hbm_outs: List[str] = []
        self._ort_input = ""
        self._out_scales: List[Optional[np.ndarray]] = []
        self._grids = {}

        if not self.model_path:
            self.last_error = "未找到模型 (vision/models/*.bin|*.onnx|*.pt 或 IS_YOLO_MODEL)"
            logger.warning("YOLO: %s", self.last_error)
            return

        want = (backend or os.environ.get(BACKEND_ENV, "")).lower()
        ext = os.path.splitext(self.model_path)[1].lower()
        try:
            if ext == ".bin" and want in ("", "bpu"):
                self._load_bpu()
            elif ext == ".onnx" and want in ("", "onnx"):
                self._load_onnx()
            elif ext == ".pt" and want in ("", "ultralytics"):
                self._load_ultralytics()
            else:
                raise RuntimeError(f"后端 {want or '?'} 与模型 {ext} 不匹配")
            self.is_ready = True
            logger.info("YOLO 就绪: backend=%s model=%s 输入=%dx%d(%s/%s) 类别=%d conf=%.2f",
                        self.backend, os.path.basename(self.model_path), self.input_w,
                        self.input_h, self.input_kind, self.input_layout,
                        len(self.class_names), self.conf_thresh)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.error("YOLO 模型加载失败: %s", self.last_error)

    # ---------------------------------------------------------------- 加载
    def _load_bpu(self):
        hbm = _import_hbm_runtime()
        if hbm is not None:
            m = hbm.HB_HBMRuntime(self.model_path)
            self._hbm_name = m.model_names[0]
            self._hbm_in = m.input_names[self._hbm_name][0]
            self._hbm_outs = list(m.output_names[self._hbm_name])
            shape = list(m.input_shapes[self._hbm_name][self._hbm_in])
            if len(shape) == 4 and shape[1] == 3:
                self.input_h, self.input_w, self.input_layout = int(shape[2]), int(shape[3]), "NCHW"
            elif len(shape) == 4:
                self.input_h, self.input_w, self.input_layout = int(shape[1]), int(shape[2]), "NHWC"
            self.input_kind = "nv12"          # 官方 Model Zoo 约定: packed NV12 uint8
            try:
                m.set_scheduling_params(priority=0, bpu_cores=[0])
            except Exception:
                pass
            self._model, self._bpu_api, self.backend = m, "hbm", "bpu"
            return
        dnn = _import_hobot_dnn()
        if dnn is None:
            raise RuntimeError("BPU 运行库不可用 (hbm_runtime / hobot_dnn 均未找到)。容器需用 ubuntu:22.04 "
                               "并挂载宿主运行库: deploy/provision_intelligentstoring.sh --recreate; "
                               "排查: 宿主执行 deploy/bpu_doctor.sh")
        models = dnn.load(self.model_path)
        if not models:
            raise RuntimeError("dnn.load 返回空")
        self._model = models[0]
        props = self._model.inputs[0].properties
        shape = tuple(int(s) for s in props.shape)
        layout = str(getattr(props, "layout", "NCHW")).upper()
        if "NHWC" in layout:
            self.input_layout = "NHWC"
            self.input_h, self.input_w = shape[1], shape[2]
        else:
            self.input_layout = "NCHW"
            self.input_h, self.input_w = shape[2], shape[3]
        tensor_type = str(getattr(props, "tensor_type", "")).upper()
        if "NV12" in tensor_type:
            self.input_kind = "nv12"
        elif "RGB" in tensor_type:
            self.input_kind = "rgb"
        else:
            self.input_kind = "bgr"
        # 去掉反量化节点后输出为 int32, 需要乘 scale_data
        self._out_scales = []
        for out in self._model.outputs:
            sc = None
            try:
                sd = np.array(out.properties.scale_data, dtype=np.float32)
                if sd.size > 0:
                    sc = sd
            except Exception:
                pass
            self._out_scales.append(sc)
        self._bpu_api, self.backend = "pyeasy", "bpu"

    def _load_onnx(self):
        import onnxruntime as ort  # noqa
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.intra_op_num_threads = max(1, (os.cpu_count() or 4))
        self._model = ort.InferenceSession(self.model_path, so, providers=["CPUExecutionProvider"])
        inp = self._model.get_inputs()[0]
        self._ort_input = inp.name
        _, _, h, w = inp.shape
        self.input_h, self.input_w = int(h), int(w)
        self.input_kind, self.input_layout = "rgb", "NCHW"
        if not self.class_names:
            try:
                meta = self._model.get_modelmeta().custom_metadata_map
                names = ast.literal_eval(meta.get("names", "{}"))
                self.class_names = [names[k] for k in sorted(names)]
            except Exception:
                pass
        self.backend = "onnx"

    def _load_ultralytics(self):
        from ultralytics import YOLO  # noqa
        self._model = YOLO(self.model_path)
        if not self.class_names:
            names = self._model.names
            self.class_names = [names[k] for k in sorted(names)]
        self.input_w = self.input_h = int(os.environ.get("IS_YOLO_IMGSZ", 640))
        self.backend = "ultralytics"

    # ---------------------------------------------------------------- 推理
    def infer(self, bgr: np.ndarray) -> List[Detection]:
        if not self.is_ready or bgr is None or bgr.size == 0:
            return []
        if self.backend == "ultralytics":
            return self._infer_ultralytics(bgr)

        t0 = time.perf_counter()
        img, scale, px, py = letterbox(bgr, self.input_w, self.input_h)
        if self.backend == "bpu":
            if self.input_kind == "nv12":
                tensor = bgr_to_nv12(img)
            else:
                x = img if self.input_kind == "bgr" else cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                tensor = x[None] if self.input_layout == "NHWC" else x.transpose(2, 0, 1)[None]
                tensor = np.ascontiguousarray(tensor)
        else:  # onnx
            x = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).transpose(2, 0, 1)[None]
            tensor = np.ascontiguousarray(x, dtype=np.float32) * (1.0 / 255.0)
        t1 = time.perf_counter()

        if self.backend == "bpu" and self._bpu_api == "hbm":
            res = self._model.run({self._hbm_name: {self._hbm_in: tensor.reshape(-1)}})[self._hbm_name]
            arrays = [np.asarray(res[n], dtype=np.float32) for n in self._hbm_outs]
        elif self.backend == "bpu":
            outs = self._model.forward(tensor)
            arrays = []
            for i, o in enumerate(outs):
                a = np.asarray(o.buffer)
                sc = self._out_scales[i] if i < len(self._out_scales) else None
                if sc is not None and a.dtype.kind in "iu":
                    a = a.astype(np.float32)
                    if sc.size == 1:
                        a *= float(sc[0])
                    elif a.shape[-1] == sc.size:       # NHWC 按通道反量化
                        a *= sc.reshape([1] * (a.ndim - 1) + [-1])
                    else:
                        logger.warning("输出 %d 的 scale_data 长度 %d 与通道数不符", i, sc.size)
                arrays.append(a.astype(np.float32, copy=False))
        else:
            arrays = [np.asarray(a, dtype=np.float32)
                      for a in self._model.run(None, {self._ort_input: tensor})]
        t2 = time.perf_counter()

        dets = self._decode(arrays, scale, px, py, bgr.shape[1], bgr.shape[0])
        t3 = time.perf_counter()
        self.last_ms = {"pre": (t1 - t0) * 1e3, "infer": (t2 - t1) * 1e3, "post": (t3 - t2) * 1e3}
        return dets

    def _infer_ultralytics(self, bgr: np.ndarray) -> List[Detection]:
        t0 = time.perf_counter()
        r = self._model.predict(bgr, imgsz=self.input_w, conf=self.conf_thresh,
                                iou=self.iou_thresh, agnostic_nms=self.agnostic_nms,
                                max_det=self.max_det, verbose=False)[0]
        dets = []
        for b in r.boxes:
            cid = int(b.cls)
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0].tolist()]
            dets.append(Detection(cid, self._name(cid), float(b.conf), x1, y1, x2, y2))
        self.last_ms = {"pre": 0.0, "infer": (time.perf_counter() - t0) * 1e3, "post": 0.0}
        return dets

    # ---------------------------------------------------------------- 后处理
    def _name(self, cid: int) -> str:
        return self.class_names[cid] if 0 <= cid < len(self.class_names) else f"cls_{cid}"

    def _grid(self, h: int, w: int, stride: int) -> np.ndarray:
        key = (h, w, stride)
        g = self._grids.get(key)
        if g is None:
            ys, xs = np.meshgrid(np.arange(h), np.arange(w), indexing="ij")
            g = (np.stack([xs, ys], -1).reshape(-1, 2).astype(np.float32) + 0.5) * stride
            self._grids[key] = g
        return g

    def _decode(self, arrays: List[np.ndarray], scale: float, px: int, py: int,
                ow: int, oh: int) -> List[Detection]:
        squeezed = [a.reshape(a.shape[1:]) if a.ndim == 4 and a.shape[0] == 1 else a for a in arrays]
        if len(arrays) >= 6 and all(a.ndim == 3 for a in squeezed):
            boxes, scores, cls = self._decode_split(squeezed)
        else:
            boxes, scores, cls = self._decode_single(arrays[0])
        if boxes.shape[0] == 0:
            return []
        if self.class_conf:                      # 按类别的单独阈值
            thr = np.array([self.class_conf.get(self._name(int(c)), self.conf_thresh) for c in cls],
                           dtype=np.float32)
            ok = scores >= thr
            boxes, scores, cls = boxes[ok], scores[ok], cls[ok]
            if boxes.shape[0] == 0:
                return []

        if self.agnostic_nms:
            keep = _nms_xyxy(boxes, scores, self.iou_thresh, self.max_det)
        else:  # 按类偏移实现分类 NMS
            off = boxes + (cls[:, None] * 4096.0)
            keep = _nms_xyxy(off, scores, self.iou_thresh, self.max_det)

        b = boxes[keep]
        b[:, [0, 2]] = np.clip((b[:, [0, 2]] - px) / scale, 0, ow - 1)
        b[:, [1, 3]] = np.clip((b[:, [1, 3]] - py) / scale, 0, oh - 1)
        out = []
        for (x1, y1, x2, y2), s, c in zip(b, scores[keep], cls[keep]):
            c = int(c)
            out.append(Detection(c, self._name(c), float(s), float(x1), float(y1), float(x2), float(y2)))
        return out

    def _decode_split(self, outs: List[np.ndarray]):
        """6 头: 按 H 区分 stride, 按通道数区分 bbox(64)/cls(nc)。"""
        by_hw = {}
        for a in outs:
            h, w, _c = a.shape
            by_hw.setdefault((h, w), []).append(a)
        c_min = min([self.conf_thresh] + list(self.class_conf.values()))
        logit_thr = float(np.log(c_min / (1.0 - c_min)))
        all_b, all_s, all_c = [], [], []
        for (h, w), pair in by_hw.items():
            if len(pair) != 2:
                continue
            bbox = next((p for p in pair if p.shape[2] == REG_MAX * 4), None)
            clsm = next((p for p in pair if p is not bbox), None)
            if bbox is None or clsm is None:
                continue
            stride = int(round(self.input_h / h))
            clsm = clsm.reshape(-1, clsm.shape[2])
            best = clsm.max(1)
            idx = np.nonzero(best >= logit_thr)[0]      # 先在 logit 上筛, 不对全图做 sigmoid
            if idx.size == 0:
                continue
            cid = clsm[idx].argmax(1)
            score = _sigmoid(best[idx])
            reg = bbox.reshape(-1, REG_MAX * 4)[idx].reshape(-1, 4, REG_MAX)
            reg = np.exp(reg - reg.max(-1, keepdims=True))
            reg /= reg.sum(-1, keepdims=True)
            ltrb = (reg * np.arange(REG_MAX, dtype=np.float32)).sum(-1) * stride
            ctr = self._grid(h, w, stride)[idx]
            xyxy = np.concatenate([ctr - ltrb[:, :2], ctr + ltrb[:, 2:]], 1)
            all_b.append(xyxy)
            all_s.append(score)
            all_c.append(cid)
        if not all_b:
            return np.zeros((0, 4), np.float32), np.zeros(0, np.float32), np.zeros(0, int)
        return np.concatenate(all_b), np.concatenate(all_s), np.concatenate(all_c)

    def _decode_single(self, a: np.ndarray):
        out = np.squeeze(a)
        if out.ndim != 2:
            raise RuntimeError(f"无法识别的输出形状 {a.shape}")
        if out.shape[0] < out.shape[1]:
            out = out.T                                  # -> (N, 4+nc)
        scores_m = out[:, 4:]
        best = scores_m.max(1)
        idx = np.nonzero(best >= min([self.conf_thresh] + list(self.class_conf.values())))[0]
        if idx.size == 0:
            return np.zeros((0, 4), np.float32), np.zeros(0, np.float32), np.zeros(0, int)
        cxcywh = out[idx, :4]
        xyxy = np.concatenate([cxcywh[:, :2] - cxcywh[:, 2:] / 2,
                               cxcywh[:, :2] + cxcywh[:, 2:] / 2], 1)
        return xyxy, best[idx], scores_m[idx].argmax(1)

    def describe(self) -> str:
        if not self.is_ready:
            return f"YOLO 未就绪: {self.last_error}"
        m = self.last_ms
        api = f"/{self._bpu_api}" if self._bpu_api else ""
        return (f"YOLO[{self.backend}{api}] {os.path.basename(self.model_path)} "
                f"{self.input_w}x{self.input_h} pre={m['pre']:.1f} infer={m['infer']:.1f} "
                f"post={m['post']:.1f} ms")
