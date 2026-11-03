"""
YOLOv8 工件检测器 —— 识别(形状/类别) + 颜色判定 + 精确定位 + 多帧融合

流程 (单帧):
    YoloEngine.infer  ->  每个框:
        1. GrabCut 前景分割 (框内)  -> 掩膜质心作为抓取点 (比框中心准, 三角/锥体尤甚)
                                    -> minAreaRect 角度
        2. 颜色: 类别自带颜色则用之, 否则对前景像素做 HSV 投票 (红/橙/黄/绿/浅蓝/蓝/紫/黑/白/灰)
        3. 信息: 类别 info + (可选) 二维码解码
        4. 视差校正 + 畸变校正 + 单应 -> x_mm / y_mm
多帧融合 detect_multi(frames):
    对 N 帧分别推理, 按中心距离聚类; 类别按置信度加权投票, 位置取中位数,
    出现帧数 < min_hits 的丢弃 (过滤偶发误检), 最后在最新一帧上做精定位。
过滤 (vision_config.json -> detect): 托盘 ROI / 面积上下限 / 贴边截断 / 按类阈值。
    这是提高"识别率"最便宜的手段: 单帧 95% 的识别率, 5 帧投票后通常 >99%。

------------------------------------------------------------------------------
解耦要点
------------------------------------------------------------------------------
    * 只依赖 interfaces.InferenceEngineBase, **不** import 任何具体引擎:
      换 BPU / ONNX / TensorRT / 厂商 SDK 时, 传 ``engine=MyEngine()`` 即可;
      engine 为空时才按配置 (vision_config.json -> model) 构造 YoloEngine。
    * 引擎只产出 Detection (像素框); 颜色 / 信息 / 物理坐标 / 多帧融合全部在这里,
      因此换引擎不会影响识别语义。
    * 继承 HSV WorkpieceDetector, 以便 color_ranges / get_color_mask /
      update_color_range 等调试界面契约继续可用; 引擎不可用时自动退回 HSV, 程序不会崩。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from MessegeCenter.topics import VisionTarget
from .detector import WorkpieceDetector
from .interfaces import Detection, InferenceEngineBase, VisionCalibrator
from .yolo_engine import YoloEngine, load_class_names

logger = logging.getLogger(__name__)
CUR_DIR = os.path.dirname(os.path.abspath(__file__))

_DRAW = {"red": (0, 0, 255), "orange": (0, 140, 255), "yellow": (0, 230, 255),
         "green": (0, 200, 0), "light_blue": (255, 220, 120), "blue": (255, 90, 0),
         "purple": (200, 0, 160), "black": (40, 40, 40), "white": (240, 240, 240),
         "gray": (150, 150, 150)}


class YoloWorkpieceDetector(WorkpieceDetector):

    def __init__(self, calibrator: Optional[VisionCalibrator] = None,
                 engine: Optional[InferenceEngineBase] = None,
                 meta_path: Optional[str] = None,
                 refine: Optional[bool] = None,
                 config=None):
        super().__init__(calibrator)
        from .config import load_vision_config
        self.cfg = config or load_vision_config()
        det_cfg, mdl = self.cfg.detect, self.cfg.model
        self.meta_path = meta_path or os.path.join(CUR_DIR, "classes.json")
        self.meta = self._load_meta(self.meta_path)
        self.color_cn: Dict[str, str] = self.meta.get("color_names", {})
        self.hsv_cfg: dict = self.meta.get("hsv", {})
        self.engine: InferenceEngineBase = engine or YoloEngine(
            model_path=mdl.get("path") or None, backend=mdl.get("backend") or None,
            class_names=load_class_names(self.meta_path),
            conf_thresh=float(mdl.get("conf", 0.4)), iou_thresh=float(mdl.get("iou", 0.5)),
            max_det=int(mdl.get("max_det", 40)), class_conf=mdl.get("class_conf") or {})
        # classes.json 的条目用 id 对应模型类别序号, 但上层一律按类名查 (d.name / engine.class_names),
        # 所以这里以「模型类名」为键: 条目里的 name 优先, 否则用模型第 id 个类名。
        # (旧实现直接拿 id 当键, 字符串查整数键永远查不到 -> 元数据全部失效)
        _names = list(getattr(self.engine, "class_names", []) or [])
        self.class_meta: Dict[str, dict] = {}
        for c in self.meta.get("classes", []):
            i = c.get("id")
            name = c.get("name") or (_names[i] if isinstance(i, int) and 0 <= i < len(_names) else None)
            if name:
                self.class_meta[name] = c
        self.refine = bool(det_cfg.get("refine", True)) if refine is None else refine
        self.decode_qr_enabled = bool(det_cfg.get("decode_qr", True))
        # A4 信息维度: require_info=true 时 info 解不出必须是 "unknown" 并压置信度触发抖料
        self.require_info = bool(getattr(self.cfg, "require_info", False))
        self.require_info_conf = float(det_cfg.get("require_info_conf", 0.43) or 0.43)
        # A4 QR 时间预算: detect.qr_budget_ms 按「整次 scan」计 (detect()/detect_multi() 入口重置)
        self.qr_budget_ms = float(det_cfg.get("qr_budget_ms", 500) or 0)
        self._qr_budget_left = self.qr_budget_ms
        self._qr_used_ms = 0.0
        self._qr_exhausted = self.qr_budget_ms <= 0
        # A1 颜色来源: hsv(默认, HSV 投票为权威) | model(应急: 直接用类名颜色)
        self.color_source = str((getattr(self.cfg, "color", {}) or {}).get("source", "hsv")).lower()
        self.parallax_factor = float(det_cfg.get("parallax_factor", 0.6))
        self.min_area_px = float(det_cfg.get("min_area_px", 0) or 0)
        self.max_area_px = float(det_cfg.get("max_area_px", 0) or 0)
        self.edge_margin_px = float(det_cfg.get("edge_margin_px", 0) or 0)
        self.set_roi(det_cfg.get("roi") or [])
        self.rejected: Dict[str, int] = {}          # 最近一次过滤掉的原因统计 (调试用)
        self._qr = None
        missing = [n for n in getattr(self.engine, "class_names", []) if n not in self.class_meta]
        if missing:
            logger.warning("classes.json 缺少模型类别 %s 的元数据 (中文名/高度), 将按类别名显示", missing)
        if not self._engine_ready():
            logger.warning("YOLO 不可用 (%s), 退回 HSV 颜色检测", self._engine_error())

    def model_color_of(self, class_name: str) -> Optional[str]:
        """该类在 classes.json 里记录的"训练时颜色" (只读)。

        只用于与 HSV 投票做冲突检测/界面提示, **不参与 category** —— 否则换颜色就得重训。
        """
        return (self.class_meta.get(class_name, {}) or {}).get("model_color")

    def set_roi(self, polygon):
        """设置托盘 ROI 多边形 (像素)。空列表 = 不限制。"""
        self.roi = np.array(polygon, dtype=np.float32).reshape(-1, 2) if polygon else None

    def _keep(self, d: Detection, w: int, h: int) -> Optional[str]:
        """返回 None 表示保留, 否则返回丢弃原因。"""
        area = max(0.0, d.w) * max(0.0, d.h)
        if self.min_area_px and area < self.min_area_px:
            return "too_small"
        if self.max_area_px and area > self.max_area_px:
            return "too_large"
        m = self.edge_margin_px
        if m and (d.x1 < m or d.y1 < m or d.x2 > w - m or d.y2 > h - m):
            return "at_edge"
        if self.roi is not None and cv2.pointPolygonTest(self.roi, (float(d.cx), float(d.cy)), False) < 0:
            return "outside_roi"
        return None

    @staticmethod
    def _load_meta(path: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("读取 %s 失败: %s", path, e)
            return {}

    # ================================================================== 引擎契约
    def _engine_ready(self) -> bool:
        """
        引擎是否可用。

        契约要求 is_ready 是普通属性, 但第三方引擎可能只提供 is_ready/is_loaded
        之一, 甚至构造失败后根本没赋值 —— 这里统一兜底为 False (退回 HSV),
        绝不让一个不完整的引擎实现把整条识别链路带崩。
        """
        return bool(getattr(self.engine, "is_ready", False))

    def _engine_error(self) -> str:
        return str(getattr(self.engine, "last_error", "") or "引擎未就绪")

    # ================================================================== 对外
    @property
    def using_yolo(self) -> bool:
        return self._engine_ready()

    def _infer(self, frame: np.ndarray) -> List[Detection]:
        """
        调用引擎并兜底异常。

        契约要求 infer() 不抛异常, 但接入第三方引擎时未必守约 —— 这里把异常
        降级为 "本帧无目标", 保证一次推理失败不会让采集线程或整轮比赛挂掉。
        """
        try:
            dets = list(self.engine.infer(frame) or [])
        except Exception as e:
            logger.error("推理引擎异常, 本帧按无目标处理: %s", e, exc_info=True)
            return []
        h, w = frame.shape[:2]
        kept, rej = [], {}
        for d in dets:
            why = self._keep(d, w, h)
            if why is None:
                kept.append(d)
            else:
                rej[why] = rej.get(why, 0) + 1
        self.rejected = rej
        return kept

    def detect(self, bgr_frame: np.ndarray) -> List[VisionTarget]:
        if bgr_frame is None or bgr_frame.size == 0:
            return []
        if not self._engine_ready():
            return self._hsv_fallback(bgr_frame)
        self._reset_qr_budget()
        return [self._build_target(bgr_frame, d, i + 1, refine=self.refine)
                for i, d in enumerate(self._infer(bgr_frame))]

    def detect_fast(self, bgr_frame: np.ndarray) -> List[VisionTarget]:
        """只推理不精定位 (实时预览用)。"""
        if not self._engine_ready():
            return self._hsv_fallback(bgr_frame)
        return [self._build_target(bgr_frame, d, i + 1, refine=False)
                for i, d in enumerate(self._infer(bgr_frame))]

    def _match_radius(self, det: Detection, match_px: float) -> float:
        """多帧融合的匹配半径 (A5)。

        半径 = ``max(scan.match_px_min, scan.match_ratio × min(框宽, 框高))`` —— 按目标自身
        大小自适应: 小件用 match_px_min 兜底 (默认 12px), 大件按比例放宽, 不会被别的目标
        尺寸带偏。``match_ratio <= 0`` 时退回旧的固定 ``match_px`` (``service.scan()`` 仍在传,
        作为旧参数兜底)。
        """
        scn = getattr(self.cfg, "scan", None) or {}
        ratio = float(scn.get("match_ratio", 0.35) or 0)
        if ratio <= 0:
            return float(match_px)
        px_min = float(scn.get("match_px_min", 12) or 0)
        return max(px_min, ratio * min(float(det.w), float(det.h)))

    def detect_multi(self, frames: Sequence[np.ndarray], min_hits: Optional[int] = None,
                     match_px: float = 25.0, min_hit_ratio: float = 0.5
                     ) -> Tuple[List[VisionTarget], Optional[np.ndarray]]:
        """多帧融合。返回 (目标列表, 用于精定位/截图的那一帧)。

        匹配**与类别无关** (同一个框跨帧被认成不同类名也照合, 类别按加权投票取多数);
        「同类优先」只在**距离并列**时做 tie-breaker, 不作硬过滤。``min_hits`` 语义不变。
        """
        frames = [f for f in frames if f is not None and f.size]
        if not frames:
            return [], None
        if not self._engine_ready():
            return self._hsv_fallback(frames[-1]), frames[-1]
        self._reset_qr_budget()
        n = len(frames)
        min_hits = min_hits or max(1, int(np.ceil(n * min_hit_ratio)))

        tracks: List[dict] = []
        for fi, f in enumerate(frames):
            for d in self._infer(f):
                # A5: 半径按本目标大小自适应 (不再是固定 25px)
                radius = self._match_radius(d, match_px)
                cands = []
                for t in tracks:
                    if t["last"] == fi:
                        continue
                    dist = float(np.hypot(t["cx"][-1] - d.cx, t["cy"][-1] - d.cy))
                    if dist < radius:
                        cands.append((dist, t))
                best = None
                if cands:
                    dmin = min(c[0] for c in cands)
                    near = [c for c in cands if c[0] <= dmin + 1e-6]   # 距离并列
                    same = [c for c in near if d.cls_id in c[1]["votes"]]
                    best = (same or near)[0][1]                        # 并列时同类优先 (tie-breaker)
                if best is None:
                    best = {"cx": [], "cy": [], "dets": [], "votes": {}, "last": -1}
                    tracks.append(best)
                best["cx"].append(d.cx)
                best["cy"].append(d.cy)
                best["dets"].append((fi, d))
                best["votes"][d.cls_id] = best["votes"].get(d.cls_id, 0.0) + d.conf
                best["last"] = fi

        ref_idx = n - 1
        ref = frames[ref_idx]
        targets: List[VisionTarget] = []
        for t in tracks:
            hits = len(t["dets"])
            if hits < min_hits:
                continue
            cls_id = max(t["votes"], key=t["votes"].get)
            same = [d for _, d in t["dets"] if d.cls_id == cls_id]
            conf = float(np.mean([d.conf for d in same])) * min(1.0, hits / n + 0.2)
            # 用中位框做精定位 (抗单帧抖动)
            box = np.median(np.array([[d.x1, d.y1, d.x2, d.y2] for d in same]), axis=0)
            d = Detection(cls_id, same[0].name, conf, *box.tolist())
            tgt = self._build_target(ref, d, len(targets) + 1, refine=True)
            tgt.hits = hits
            targets.append(tgt)
        return targets, ref

    def _hsv_fallback(self, frame) -> List[VisionTarget]:
        """无模型时的 HSV 检测, 补齐 label/category 以便规划与大屏使用。"""
        ts = super().detect(frame)
        for t in ts:
            t.label = self.color_cn.get(t.color, t.color) + "货物"
            t.category = f"{t.shape}|{t.color}|"
            # A4: category_core (形状|颜色|) 每个目标都要有, 与 YOLO 路径一致
            t.diagnostics.update({"category_core": f"{t.shape}|{t.color}|"})
        return ts

    # ================================================================== 单目标
    def _parse_info(self, meta: dict) -> Tuple[str, bool, Optional[str]]:
        """解析 classes.json 的信息声明 (A4)。返回 (声明值, 是否尝试扫码, template_set)。

        info 可以是字符串 (旧用法, 直接当声明值), 也可以是
        ``{"qr": ..., "text": ..., "template_set": ..., "decode_qr": bool}``。
        ``decode_qr``: info.decode_qr 优先, 否则回退**旧的顶层** decode_qr 字段; 两者都没有
        就不尝试解码 —— 与 A1 及以前的行为完全一致 (require_info 默认 false 时逐步不变)。
        """
        raw = meta.get("info")
        text, template_set, flag = "", None, None
        if isinstance(raw, dict):
            text = str(raw.get("text") or raw.get("qr") or "").strip()
            template_set = raw.get("template_set") or None
            if "decode_qr" in raw:
                flag = bool(raw.get("decode_qr"))
        elif raw:
            text = str(raw).strip()
        if flag is None and "decode_qr" in meta:
            flag = bool(meta.get("decode_qr"))
        return text, bool(flag), template_set

    def _reset_qr_budget(self) -> None:
        """每次 scan 重置 QR 时间预算 (detect.qr_budget_ms 按整次 scan 计, 不是每件)。"""
        self._qr_budget_left = self.qr_budget_ms
        self._qr_used_ms = 0.0
        self._qr_exhausted = self.qr_budget_ms <= 0

    def _build_target(self, frame: np.ndarray, d: Detection, idx: int, refine: bool) -> VisionTarget:
        meta = self.class_meta.get(d.name, {})
        h_img, w_img = frame.shape[:2]
        x1, y1, x2, y2 = int(d.x1), int(d.y1), int(np.ceil(d.x2)), int(np.ceil(d.y2))
        u, v, angle = d.cx, d.cy, 0.0
        mask = None
        thin = bool(meta.get("thin_wall"))
        if refine:
            u, v, angle, mask = self._refine(frame, x1, y1, x2, y2, (d.cx, d.cy), thin=thin)

        # A1 颜色三态: 人为强制(color 非 null) > color.source=model(应急) > HSV 投票(默认权威)。
        # hsv_color 总是算出来, 用于与 model_color 做冲突检测 (diagnostics)。
        model_color = meta.get("model_color")
        hsv_color = self._vote_color(frame, x1, y1, x2, y2, mask, thin=thin)
        if meta.get("color"):
            color = meta["color"]
        elif self.color_source == "model" and model_color:
            color = model_color
        else:
            color = hsv_color
        # A4 信息维度: 声明值来自 classes.json 的 info; 「是否解码」只由声明决定 (没声明就不解),
        # 保证 require_info 默认 false 时与 A1 行为逐步一致。
        info_decl, want_qr, template_set = self._parse_info(meta)
        info = info_decl
        qr = {"text": "", "ms": 0.0, "decoder": "none", "attempted": False,
              "exhausted": bool(self._qr_exhausted)}
        if refine and self.decode_qr_enabled and want_qr:
            qr = self._decode_qr(frame, x1, y1, x2, y2)
            if qr["text"]:
                info = f"{info}:{qr['text']}" if info else qr["text"]
        if self.require_info and not info:
            info = "unknown"      # A4: 解不出必须是 unknown (不许空串, 更不许猜)

        # A1: classes.json 只有 shape_cn; 旧代码 `meta.get("shape", d.name)` 会把「带颜色的
        # 中文类名」当形状 (现场: 新类别 蓝色半球|red|), category 第一段随模型颜色变化。
        shape = meta.get("shape_cn") or meta.get("shape") or d.name
        height = float(meta.get("height_mm", 0) or 0)
        cal = self.calibrator
        # 抓取点高度 = 物体高度 × IS_PARALLAX_FACTOR (0.6 约等于上表面的高度带);
        # 未标定画面尺寸 (principal_px 缺省) 时按当前帧中心兜底, 只影响视差项。
        if cal.cam_height_mm > 0 and cal.principal_px is None and cal.camera_matrix is None:
            cal.principal_px = (w_img / 2.0, h_img / 2.0)
        x_mm, y_mm = cal.pixel_to_robot(u, v, height * self.parallax_factor)

        label = meta.get("name") or (self.color_cn.get(color, color) + meta.get("shape_cn", shape))
        if info and not meta.get("name"):
            label += f"({info})"
        # A4: require_info 但信息解不出 -> 压到 COMMIT_CONF(0.45) 以下, 交给 uncertain 抖料重扫
        conf = float(d.conf)
        info_missing = bool(self.require_info and info == "unknown")
        if info_missing:
            conf = min(conf, self.require_info_conf)
        tgt = VisionTarget(
            target_id=idx, color=color, shape=shape, confidence=round(float(conf), 3),
            u_pixel=round(float(u), 1), v_pixel=round(float(v), 1), x_mm=x_mm, y_mm=y_mm,
            class_name=d.name, label=label, info=info,
            category=f"{shape}|{color}|{info}", bbox=(x1, y1, x2, y2),
            angle_deg=round(float(angle), 1))
        # A1 旁路数据: 模型类名颜色 vs HSV 投票, 由 service 做冲突仲裁 (不影响对外字段)
        # A4 追加: category_core + QR 解码器/耗时/预算标记。svc-gate 的 A2 也会往同一 dict 写,
        #          所以统一用 update() 合并, 不整键覆盖。
        tgt.diagnostics.update({
            "model_color": model_color, "hsv_color": hsv_color,
            "color_source": self.color_source,
            "category_core": f"{shape}|{color}|",
            "qr_decoder": qr["decoder"], "qr_ms": round(float(qr["ms"]), 1),
            "qr_attempted": bool(qr["attempted"]),
            "qr_budget_exhausted": bool(qr["exhausted"] or self._qr_exhausted),
        })
        if template_set:
            tgt.diagnostics["template_set"] = template_set
        if info_missing:
            tgt.diagnostics["info_missing"] = True
        return tgt

    def _refine(self, frame, x1, y1, x2, y2, fallback, thin: bool = False):
        """
        GrabCut 分割框内前景, 返回 (u, v, angle, mask_in_box)。失败退回框中心。
        thin=True (薄壁/空心件, classes.json 里 thin_wall): 前景只是一圈壁, 中间露出托盘,
            抓取点取外轮廓填实后的质心; 颜色只用壁上的像素 (返回的 mask 是环)。
        """
        h, w = frame.shape[:2]
        pad = max(6, int(0.15 * max(x2 - x1, y2 - y1)))
        cx1, cy1, cx2, cy2 = max(0, x1 - pad), max(0, y1 - pad), min(w, x2 + pad), min(h, y2 + pad)
        crop = frame[cy1:cy2, cx1:cx2]
        rw, rh = x2 - x1, y2 - y1
        if crop.size == 0 or rw < 8 or rh < 8:
            return fallback[0], fallback[1], 0.0, None
        try:
            # GrabCut 耗时与面积成正比: 大框先缩到 ~128px 再分割, 掩膜放大回原尺寸
            k_s = min(1.0, 128.0 / max(rw, rh))
            small = crop if k_s >= 1.0 else cv2.resize(crop, None, fx=k_s, fy=k_s, interpolation=cv2.INTER_AREA)
            rect = (int((x1 - cx1) * k_s), int((y1 - cy1) * k_s),
                    max(2, int(rw * k_s)), max(2, int(rh * k_s)))
            gc = np.zeros(small.shape[:2], np.uint8)
            bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
            cv2.grabCut(small, gc, rect, bgd, fgd, 2, cv2.GC_INIT_WITH_RECT)
            m = np.where((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
            if k_s < 1.0:
                m = cv2.resize(m, (crop.shape[1], crop.shape[0]), interpolation=cv2.INTER_NEAREST)
            m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            n, lab, stats, cents = cv2.connectedComponentsWithStats(m)
            if n <= 1:
                raise ValueError("无前景")
            k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            ratio = stats[k, cv2.CC_STAT_AREA] / float(rw * rh)
            if not (0.05 if thin else 0.2) <= ratio <= 0.98:
                raise ValueError(f"前景占比异常 {ratio:.2f}")
            m = (lab == k).astype(np.uint8) * 255
            cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            outer = max(cnts, key=cv2.contourArea) if cnts else None
            angle = cv2.minAreaRect(outer)[2] if outer is not None else 0.0
            u, v = cents[k][0] + cx1, cents[k][1] + cy1
            if thin and outer is not None:
                mo = cv2.moments(outer)          # 外轮廓填实后的质心 = 空心件几何中心
                if mo["m00"] > 0:
                    u, v = mo["m10"] / mo["m00"] + cx1, mo["m01"] / mo["m00"] + cy1
            box_mask = m[y1 - cy1:y2 - cy1, x1 - cx1:x2 - cx1]
            return float(u), float(v), float(angle), box_mask
        except Exception as e:
            logger.debug("精定位退回框中心: %s", e)
            return fallback[0], fallback[1], 0.0, None

    def _vote_color(self, frame, x1, y1, x2, y2, mask, thin: bool = False) -> str:
        roi = frame[y1:y2, x1:x2]
        if roi.size == 0:
            return "unknown"
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        hh, ww = hsv.shape[:2]
        if mask is not None and mask.shape == hsv.shape[:2] and mask.any():
            px = hsv[mask > 0]
        elif thin:  # 空心件无掩膜: 取框内 8%~30% 的环带 (壁), 避开中间露出的托盘和框外背景
            ring = np.zeros((hh, ww), np.uint8)
            cv2.rectangle(ring, (int(ww * .08), int(hh * .08)), (int(ww * .92), int(hh * .92)), 255, -1)
            cv2.rectangle(ring, (int(ww * .30), int(hh * .30)), (int(ww * .70), int(hh * .70)), 0, -1)
            px = hsv[ring > 0]
        else:  # 无掩膜时取框中心 50% 区域, 尽量避开背景
            px = hsv[hh // 4: hh * 3 // 4 + 1, ww // 4: ww * 3 // 4 + 1].reshape(-1, 3)
        if px.size == 0:
            return "unknown"
        cfg = self.hsv_cfg
        H, S, V = px[:, 0].astype(int), px[:, 1].astype(int), px[:, 2].astype(int)
        black = V <= cfg.get("black_v_max", 60)
        achrom = (~black) & (S <= cfg.get("white_s_max", 45))
        white = achrom & (V >= cfg.get("white_v_min", 150))
        gray = achrom & ~white
        chrom = ~(black | achrom)
        counts = {"black": int(black.sum()), "white": int(white.sum()), "gray": int(gray.sum())}
        hc = H[chrom]
        for name, lo, hi in cfg.get("hue_bins", []):
            counts[name] = counts.get(name, 0) + int(((hc >= lo) & (hc <= hi)).sum())
        color = max(counts, key=counts.get)
        if counts[color] == 0:
            return "unknown"      # 彩色像素全落在 hue_bins 空隙: 宁可 unknown 也不误判成 black
        if color == "blue" and chrom.any():
            s_med = float(np.median(S[chrom][(hc >= 85) & (hc <= 129)])) if ((hc >= 85) & (hc <= 129)).any() else 255
            if s_med < cfg.get("light_blue_s_max", 150):
                color = "light_blue"
        return color

    # A4: 多尺度 × 多预处理 (命中即停)
    _QR_SCALES = (1, 2, 3)

    def _qr_detector(self) -> Tuple[str, object]:
        """惰性构造 QR 解码器 (A4)。

        主路径 ``cv2.QRCodeDetectorAruco``, 不存在才回退 ``cv2.QRCodeDetector``。
        **禁止**用 ``cv2.wechat_qrcode_WeChatQRCode()`` (属 opencv-contrib, 板上只有
        opencv-python 4.11, 调它只会抛异常) —— 也不做静默降级: 实际用哪个解码器由调用方
        写进 ``diagnostics["qr_decoder"]`` ("aruco"/"qrcode"/"none")。
        """
        if self._qr is None:
            det, kind = None, "none"
            cls = getattr(cv2, "QRCodeDetectorAruco", None)
            if cls is not None:
                try:
                    det, kind = cls(), "aruco"
                except Exception as e:            # 构造失败才回退, 不吞掉事实
                    logger.warning("QRCodeDetectorAruco 构造失败 (%s), 回退 QRCodeDetector", e)
            if det is None:
                try:
                    det, kind = cv2.QRCodeDetector(), "qrcode"
                except Exception as e:
                    logger.warning("QRCodeDetector 也不可用 (%s): 本次运行不做二维码解码", e)
                    det, kind = None, "none"
            self._qr = (kind, det)
            logger.info("QR 解码器: %s", kind)
        return self._qr

    def _qr_images(self, crop):
        """裁剪图 × {1×,2×,3×} × {原图, OTSU, 自适应阈值, CLAHE}, 命中即停 (A4)。"""
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        clahe = None
        for s in self._QR_SCALES:
            img = gray if s == 1 else cv2.resize(gray, None, fx=s, fy=s,
                                                 interpolation=cv2.INTER_CUBIC)
            yield img                                  # 1) 原图 (最便宜, 先试)
            try:
                _t, otsu = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                yield otsu                             # 2) OTSU 二值
                yield cv2.adaptiveThreshold(img, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                            cv2.THRESH_BINARY, 31, 5)     # 3) 自适应阈值
                if clahe is None:
                    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                yield clahe.apply(img)                 # 4) CLAHE
            except Exception as e:
                logger.debug("QR 预处理失败 (scale=%s): %s", s, e)

    def _decode_qr(self, frame, x1, y1, x2, y2) -> Dict[str, object]:
        """框内裁剪 → 多尺度 × 多预处理, 命中即停。返回解码结果字典 (A4)。

        ``detect.qr_budget_ms`` 是**整次 scan** 的预算 (detect()/detect_multi() 入口由
        ``_reset_qr_budget()`` 重置), 预算耗尽后本次 scan 内不再尝试解码。
        返回 ``{text, ms, decoder, attempted, exhausted}``; 调用方把 decoder/ms/attempted/
        exhausted 如实写进 ``diagnostics``。
        """
        out: Dict[str, object] = {"text": "", "ms": 0.0, "decoder": "none",
                                  "attempted": False, "exhausted": bool(self._qr_exhausted)}
        if self._qr_budget_left <= 0:            # 预算已耗尽 (含 qr_budget_ms=0)
            self._qr_exhausted = True
            out["exhausted"] = True
            return out
        h, w = frame.shape[:2]
        pad = 10
        crop = frame[max(0, y1 - pad):min(h, y2 + pad), max(0, x1 - pad):min(w, x2 + pad)]
        if crop.size == 0 or min(crop.shape[:2]) < 16:
            return out
        kind, det = self._qr_detector()
        out["decoder"] = kind
        if det is None:                          # 没有可用解码器: 本次 scan 不再白试
            self._qr_exhausted = True
            out["exhausted"] = True
            return out
        out["attempted"] = True
        t0 = time.perf_counter()
        budget = self.qr_budget_ms if self.qr_budget_ms > 0 else self._qr_budget_left
        for img in self._qr_images(crop):
            used_ms = (time.perf_counter() - t0) * 1000.0
            if used_ms >= budget or used_ms >= self._qr_budget_left:
                out["exhausted"] = True
                break
            try:
                data = det.detectAndDecode(img)[0]
            except Exception as e:
                logger.debug("QR 解码异常 (%s): %s", kind, e)
                data = ""
            if data:
                out["text"] = str(data).strip()
                break
        out["ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        self._qr_budget_left = max(0.0, self._qr_budget_left - float(out["ms"]))
        if out["exhausted"] or self._qr_budget_left <= 0:
            self._qr_exhausted = True
            out["exhausted"] = True
        return out

    # ================================================================== 绘制
    def annotate(self, bgr_frame: np.ndarray, targets: List[VisionTarget]) -> np.ndarray:
        if not self._engine_ready():
            return super().annotate(bgr_frame, targets)
        vis = bgr_frame.copy()
        if self.roi is not None:
            cv2.polylines(vis, [self.roi.astype(np.int32)], True, (0, 200, 255), 1)
        for t in targets:
            # A1: 颜色冲突目标用洋红醒目色 + 文字标「冲突」, 调试台一眼能看到仲裁对象
            _cf = bool((getattr(t, "diagnostics", None) or {}).get("color_conflict"))
            c = (255, 0, 255) if _cf else _DRAW.get(t.color, (255, 255, 255))
            if t.bbox:
                x1, y1, x2, y2 = [int(v) for v in t.bbox]
                cv2.rectangle(vis, (x1, y1), (x2, y2), c, 2)
            else:
                x1, y1 = int(t.u_pixel) - 20, int(t.v_pixel) - 20
            u, v = int(t.u_pixel), int(t.v_pixel)
            cv2.drawMarker(vis, (u, v), (255, 255, 255), cv2.MARKER_CROSS, 14, 2)
            txt = f"#{t.target_id} {t.class_name}/{t.color} {t.confidence:.2f} ({t.x_mm:.1f},{t.y_mm:.1f})"
            if _cf:
                _d = t.diagnostics or {}
                txt += f" 冲突 HSV={_d.get('hsv_color')} 类名={_d.get('model_color')}"
            cv2.putText(vis, txt, (x1, max(12, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 0), 3)
            cv2.putText(vis, txt, (x1, max(12, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.42, c, 1)
        cv2.putText(vis, self.engine.describe(), (6, vis.shape[0] - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 120), 1)
        return vis
