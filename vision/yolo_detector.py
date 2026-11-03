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
        return [self._build_target(bgr_frame, d, i + 1, refine=self.refine)
                for i, d in enumerate(self._infer(bgr_frame))]

    def detect_fast(self, bgr_frame: np.ndarray) -> List[VisionTarget]:
        """只推理不精定位 (实时预览用)。"""
        if not self._engine_ready():
            return self._hsv_fallback(bgr_frame)
        return [self._build_target(bgr_frame, d, i + 1, refine=False)
                for i, d in enumerate(self._infer(bgr_frame))]

    def detect_multi(self, frames: Sequence[np.ndarray], min_hits: Optional[int] = None,
                     match_px: float = 25.0, min_hit_ratio: float = 0.5
                     ) -> Tuple[List[VisionTarget], Optional[np.ndarray]]:
        """多帧融合。返回 (目标列表, 用于精定位/截图的那一帧)。"""
        frames = [f for f in frames if f is not None and f.size]
        if not frames:
            return [], None
        if not self._engine_ready():
            return self._hsv_fallback(frames[-1]), frames[-1]
        n = len(frames)
        min_hits = min_hits or max(1, int(np.ceil(n * min_hit_ratio)))

        tracks: List[dict] = []
        for fi, f in enumerate(frames):
            for d in self._infer(f):
                best, best_d = None, match_px
                for t in tracks:
                    if t["last"] == fi:
                        continue
                    dist = np.hypot(t["cx"][-1] - d.cx, t["cy"][-1] - d.cy)
                    if dist < best_d:
                        best, best_d = t, dist
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
        return ts

    # ================================================================== 单目标
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
        info = meta.get("info", "") or ""
        if refine and self.decode_qr_enabled and meta.get("decode_qr"):
            qr = self._decode_qr(frame, x1, y1, x2, y2)
            if qr:
                info = f"{info}:{qr}" if info else qr

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
        return VisionTarget(
            target_id=idx, color=color, shape=shape, confidence=round(float(d.conf), 3),
            u_pixel=round(float(u), 1), v_pixel=round(float(v), 1), x_mm=x_mm, y_mm=y_mm,
            class_name=d.name, label=label, info=info,
            category=f"{shape}|{color}|{info}", bbox=(x1, y1, x2, y2),
            angle_deg=round(float(angle), 1),
            # A1 旁路数据: 模型类名颜色 vs HSV 投票, 由 service 做冲突仲裁 (不影响对外字段)
            diagnostics={"model_color": model_color, "hsv_color": hsv_color,
                         "color_source": self.color_source})

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

    def _decode_qr(self, frame, x1, y1, x2, y2) -> str:
        h, w = frame.shape[:2]
        pad = 10
        crop = frame[max(0, y1 - pad):min(h, y2 + pad), max(0, x1 - pad):min(w, x2 + pad)]
        if crop.size == 0:
            return ""
        if self._qr is None:
            try:
                self._qr = ("wechat", cv2.wechat_qrcode_WeChatQRCode())
            except Exception:
                self._qr = ("cv", cv2.QRCodeDetector())
        kind, det = self._qr
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        for img in (gray, cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)):
            try:
                if kind == "wechat":
                    res, _ = det.detectAndDecode(img)
                    if res:
                        return str(res[0]).strip()
                else:
                    data, _, _ = det.detectAndDecode(img)
                    if data:
                        return str(data).strip()
            except Exception:
                pass
        return ""

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
