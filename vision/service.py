"""
视觉服务 VisionService —— 上层系统唯一的视觉门面 (契约见 vision/interfaces.py)。

线程结构:
    VisionGrabber  (cameras/grabber.py)  持续取图, 只保留最新帧, 掉线自动重连
    VisionWorker   (本文件)              预览识别 (continuous_detect=True 时) + 1Hz 心跳
    scan()         调用方线程            按需多帧融合识别 (比赛流程唯一识别入口)

推理互斥: BPU 模型句柄非线程安全, 预览识别与 scan() 共用一把锁;
         scan 进行时预览识别直接跳过本帧, 不排队、不拖慢 scan。
稳定性:   scan()/get_*() 永不抛异常; 相机掉线时 scan 返回 ([], None) 并在日志说明原因,
         后台继续重连, 相机恢复后下一次 scan 自动可用。
复盘:     record.enabled 时每次 scan 保存 原图/标注图/目标 json 到 record.dir (环形, 保留 keep 组),
         赛后可直接用 replay 相机回放这些原图复现问题, 也可作为补充训练数据。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from MessegeCenter.bus import MessageBus, get_message_bus
from MessegeCenter.topics import (
    TOPIC_MONITOR_HEARTBEAT, TOPIC_VISION_TARGET, ModuleHeartbeat, VisionTarget,
)

from .calibration import CoordinateCalibrator
from .cameras import FrameGrabber, PhysicalCamera, apply_controls, create_camera
from .config import VisionConfig, load_vision_config
from .detector import WorkpieceDetector
from .interfaces import (
    VisionBackend, VisionCapabilities, VisionServiceBase, register_vision_backend,
)

logger = logging.getLogger(__name__)


class VisionService(VisionServiceBase):

    TRAY_EVENT_HOLD_S = 30.0   # 托盘位移事件在自检里保留的秒数

    def __init__(self, bus: Optional[MessageBus] = None, detector_factory=None,
                 continuous_detect: bool = True, config: Optional[VisionConfig] = None):
        bus = bus or get_message_bus()
        super().__init__(bus=bus)
        self.bus = bus
        self.cfg = config or load_vision_config()
        self.calibrator = CoordinateCalibrator()
        self.detector = (detector_factory or WorkpieceDetector)(self.calibrator)
        self.continuous_detect = continuous_detect

        self._cam_cfg: Dict[str, Any] = dict(self.cfg.camera)
        self._grabber: Optional[FrameGrabber] = None
        self._running = False
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._detect_lock = threading.Lock()

        self._latest_annotated: Optional[np.ndarray] = None
        self._latest_targets: List[VisionTarget] = []
        self._fps = 0.0
        self.last_scan: Dict[str, Any] = {}
        self.warnings: List[str] = list(self.cfg.warnings)   # 启动时的配置告警, 不再追加运行期事件
        self._tray_event: tuple = (0.0, "")

        self._rec_q: "queue.Queue" = queue.Queue(maxsize=8)
        self._rec_thread: Optional[threading.Thread] = None
        self._rec_n = 0

    # ================================================================== 状态
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def fps(self) -> float:
        if self.continuous_detect:
            return self._fps
        return self._grabber.fps if self._grabber else 0.0

    @property
    def camera(self):
        """当前打开的相机实例 (可能为 None: 未启动或正在重连)。"""
        return self._grabber.camera if self._grabber else None

    @property
    def camera_connected(self) -> bool:
        return bool(self._grabber and self._grabber.connected)

    @property
    def capabilities(self) -> VisionCapabilities:
        return VisionCapabilities(
            supports_hsv_tuning=True, supports_mask_preview=True,
            requires_physical_camera=False,
            extra={"camera_type": self._cam_cfg.get("type"),
                   "engine": getattr(getattr(self.detector, "engine", None), "backend", "hsv")})

    # ================================================================== 生命周期
    def set_camera_source(self, use_synthetic: bool = True, device_id=0):
        """
        use_synthetic=True  -> 仿真相机
        use_synthetic=False -> vision_config.json 里配置的相机 (uvc / hikrobot / replay)
                               device_id 非 0 时覆盖 UVC 设备号 (兼容旧调用)
        """
        was = self._running
        if was:
            self.stop()
        cfg = dict(self.cfg.camera)
        if use_synthetic:
            cfg["type"] = "synthetic"
        elif cfg.get("type") == "uvc" and device_id not in (0, "0", None):
            cfg["device"] = device_id
        self._cam_cfg = cfg
        if was:
            self.start()

    def set_camera_config(self, **overrides):
        """调试工具用: 直接改相机配置 (type/path/exposure_us ...), 运行中会自动重启采集。"""
        was = self._running
        if was:
            self.stop()
        self._cam_cfg.update(overrides)
        if was:
            self.start()

    def start(self):
        if self._running:
            return
        cfg = dict(self._cam_cfg)
        self._grabber = FrameGrabber(lambda: create_camera(cfg), stale_s=float(cfg.get("stale_s", 2.0)))
        self._grabber.on_open = lambda cam: apply_controls(cam, cfg)
        self._stop.clear()
        self._running = True
        got = self._grabber.start(wait_first_s=5.0)
        if not got:
            logger.error("视觉: 5 秒内未取到图像 (%s), 后台继续重试", self._grabber.last_error or "无图")
        self._worker = threading.Thread(target=self._worker_loop, name="VisionWorker", daemon=True)
        self._worker.start()
        if self.cfg.record.get("enabled") and self._rec_thread is None:
            self._rec_thread = threading.Thread(target=self._record_loop, name="VisionRecorder", daemon=True)
            self._rec_thread.start()

    def stop(self):
        self._running = False
        self._stop.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=2.0)
        self._worker = None
        if self._grabber:
            self._grabber.stop()

    # ================================================================== 查询
    def get_latest_frame_and_targets(self) -> Tuple[Optional[np.ndarray], List[VisionTarget]]:
        with self._lock:
            ann, ts = self._latest_annotated, list(self._latest_targets)
        if ann is None and self._grabber:            # 预览识别关闭时给原图
            f, _, _ = self._grabber.latest()
            ann = f
        return (ann.copy() if ann is not None else None), ts

    def get_latest_raw_frame(self) -> Optional[np.ndarray]:
        if not self._grabber:
            return None
        f, _, _ = self._grabber.latest()
        return f.copy() if f is not None else None

    def get_fresh_frames(self, n: int = 1, timeout_s: float = 2.0, skip: int = 0) -> List[np.ndarray]:
        if not self._grabber:
            return []
        return self._grabber.fresh_frames(n, timeout_s, skip=skip)

    def detect_now(self, frame: np.ndarray) -> List[VisionTarget]:
        try:
            with self._detect_lock:
                return self.detector.detect(frame)
        except Exception:
            logger.exception("detect_now 异常")
            return []

    # ================================================================== 比赛识别
    def scan(self, n_frames: Optional[int] = None, skip_frames: Optional[int] = None
             ) -> Tuple[List[VisionTarget], Optional[np.ndarray]]:
        """
        取 n 帧新图做多帧融合识别。永不抛异常; 相机无图时返回 ([], None)。
        结果同时发布到 TOPIC_VISION_TARGET (List[VisionTarget]) 并更新预览缓存。
        """
        sc = self.cfg.scan
        n = int(n_frames or sc.get("frames", 5))
        skip = int(sc.get("skip_frames", 1) if skip_frames is None else skip_frames)
        t0 = time.perf_counter()
        frames = self.get_fresh_frames(n, timeout_s=float(sc.get("timeout_s", 3.0)), skip=skip)
        t1 = time.perf_counter()
        if not frames:
            msg = self._grabber.last_error if self._grabber else "视觉服务未启动"
            logger.error("scan: 没有取到新图像 (%s)", msg or "相机无图")
            self.last_scan = {"ok": False, "error": msg or "no_frame", "time": time.time()}
            return [], None
        try:
            with self._detect_lock:
                self._update_tray(frames)
                det = self.detector
                if hasattr(det, "detect_multi"):
                    targets, ref = det.detect_multi(frames, match_px=float(sc.get("match_px", 25)),
                                                    min_hit_ratio=float(sc.get("min_hit_ratio", 0.5)))
                else:
                    ref = frames[-1]
                    targets = det.detect(ref)
                ann = det.annotate(ref, targets) if ref is not None else None
        except Exception:
            logger.exception("scan: 识别异常, 本次按无目标处理")
            return [], frames[-1]
        t2 = time.perf_counter()
        targets.sort(key=lambda t: -t.confidence)
        for i, t in enumerate(targets):
            t.target_id = i + 1
        with self._lock:
            self._latest_targets = list(targets)
            self._latest_annotated = ann
        self.last_scan = {"ok": True, "frames": len(frames), "targets": len(targets),
                          "grab_ms": round((t1 - t0) * 1e3, 1), "detect_ms": round((t2 - t1) * 1e3, 1),
                          "rejected": dict(getattr(self.detector, "rejected", {}) or {}),
                          "time": time.time()}
        logger.info("scan: %d 帧 -> %d 件 (取图 %.0f ms, 识别 %.0f ms) %s",
                    len(frames), len(targets), (t1 - t0) * 1e3, (t2 - t1) * 1e3,
                    self.last_scan["rejected"] or "")
        try:
            self.bus.publish(TOPIC_VISION_TARGET, list(targets), sender="vision")
        except Exception:
            pass
        self._enqueue_record(ref, ann, targets)
        return targets, ref

    # ================================================================== 自检
    def self_check(self, log: bool = True) -> Dict[str, Any]:
        """一次性检查 相机/模型/类别/标定/ROI, 返回报告 (log=True 时写日志)。"""
        rep: Dict[str, Any] = {"ok": True, "problems": [], "hints": [], "config": self.cfg.source}
        g = self._grabber
        frame = g.latest()[0] if g else None
        cam = g.camera if g else None
        rep["camera"] = {"type": self._cam_cfg.get("type"), "connected": bool(g and g.connected),
                         "fps": round(g.fps, 1) if g else 0.0,
                         "desc": cam.describe() if cam else (g.last_error if g else ""),
                         "reconnects": g.reconnects if g else 0}
        if frame is None:
            rep["problems"].append("相机无图像")
        eng = getattr(self.detector, "engine", None)
        if eng is not None:
            rep["engine"] = {"ready": bool(getattr(eng, "is_ready", False)), "desc": eng.describe(),
                             "classes": list(getattr(eng, "class_names", []))}
            if getattr(eng, "notice", ""):
                rep["problems"].append(eng.notice)
            if not rep["engine"]["ready"]:
                rep["problems"].append(f"YOLO 未就绪, 已退回 HSV: {getattr(eng, 'last_error', '')}")
            meta = getattr(self.detector, "class_meta", {})
            miss = [c for c in rep["engine"]["classes"] if c not in meta]
            if miss:
                rep["problems"].append(f"classes.json 缺少类别 {miss}")
        else:
            rep["engine"] = {"ready": False, "desc": "HSV 颜色检测 (未启用 YOLO 后端)"}
        cal = self.calibrator
        cfg_found = os.path.exists(cal.config_path)
        rep["calibration"] = {"calibrated": cal.is_calibrated, "config_found": cfg_found,
                              "points": int(len(cal.src_pixels)),
                              "reproj_error_mm": round(cal.reproj_error_mm, 2),
                              "undistort": cal.camera_matrix is not None,
                              "cam_height_mm": cal.cam_height_mm}
        # 托盘模式下 "未标定" 的真实原因是没有参考点/origin_mm, 由下面 tray 段给出可操作提示, 这里不重复报
        if not cfg_found:
            rep["problems"].append(f"没有标定文件 {cal.config_path}, 正在用默认占位标定, 坐标不可信")
        elif not cal.is_calibrated and not cal.tray_mode:
            rep["problems"].append("标定文件里没有有效标定点, 坐标不可信 (重新标定)")
        elif cal.reproj_error_mm > 2.0:
            rep["problems"].append(f"标定重投影误差 {cal.reproj_error_mm:.1f}mm 偏大, 建议重标")
        if frame is not None:
            w = cal.check_frame_size(frame.shape[1], frame.shape[0])
            if w:
                rep["problems"].append(w)
        if cal.tray_mode:
            if frame is not None and not cal.tray_tracker.locked_live:
                # 与 scan/预览串行: 不持检测锁会和正在用坐标的 scan 交错
                if self._detect_lock.acquire(timeout=3.0):
                    try:
                        self._update_tray([frame])
                    finally:
                        self._detect_lock.release()
            ts = dict(cal.tray_status)
            rep["tray"] = ts
            if not ts.get("ok"):
                rep["problems"].append(f"托盘未定位: {ts.get('msg') or '无画面'}")
            elif not ts.get("live"):
                rep["problems"].append("托盘角点来自上次保存, 本次还没在实时画面里锁定")
            if ts.get("warn"):
                rep["problems"].append(ts["warn"])
            persp = ts.get("perspective") or {}
            if persp.get("warn"):
                rep["problems"].append(persp["warn"])
            if persp.get("msg") and persp.get("method") != "pose":
                rep["problems"].append(
                    f"透视位姿没启用, 已退回"
                    f"{'径向视差' if persp.get('method') == 'radial' else '平面单应'}校正 "
                    f"(相机斜看时高度视差会有偏差): {persp['msg']}")
            if not ts.get("ref_points") and cal.tray_cfg.get("origin_mm") is None:
                rep["problems"].append(
                    "托盘模式没有参考点 (tray.ref_points) 也没有 origin_mm: 机械臂坐标只差一个平移, 不可信。"
                    "下一步: 打开调试台 http://<板子IP>:8090 → 托盘页 → 加参考点 "
                    "(尺子量 2~9 个固定点的托盘坐标 + 机械臂坐标; 接了下位机时用 ./is.sh web --arm, "
                    "点画面会自动读机械臂当前坐标), 或直接填 origin_mm")
            if cal.camera_matrix is None or cal.dist_coeffs is None:
                rep["hints"].append(
                    "未做畸变校正 (calib_config.json 没有 camera_matrix / dist_coeffs): 广角镜头边缘会偏, "
                    "四角与参考点可能对不上。建议: 调试台 → 畸变页 →「一键校正」"
                    "(打印 tools/calib_board_A4.pdf 平铺在托盘上拍一张; 没有打印机可用托盘页的直边估计)")
        rep["roi"] = bool(getattr(self.detector, "roi", None) is not None)
        rep["problems"] += self.warnings
        ev_t, ev = self._tray_event
        if ev and time.time() - ev_t < self.TRAY_EVENT_HOLD_S:
            rep["problems"].append(ev)       # 最近的托盘位移只提示一段时间, 不让自检永久 ok=False
        if ev:
            rep.setdefault("tray", {})["last_event"] = ev
        rep["ok"] = not rep["problems"]
        lvl = logging.INFO if rep["ok"] else logging.WARNING
        if log:
            logger.log(lvl, "视觉自检: 相机=%s | 引擎=%s | 标定=%s 点/%.2fmm | 问题=%s",
                       rep["camera"]["desc"], rep["engine"]["desc"], rep["calibration"]["points"],
                       cal.reproj_error_mm, rep["problems"] or "无")
        return rep

    # ================================================================== 后台
    def _worker_loop(self):
        seq = 0
        n_win, t_win = 0, time.time()
        last_hb = 0.0
        checked = False
        while not self._stop.is_set():
            g = self._grabber
            r = g.wait_frame(seq, 0.5) if g else None
            now = time.time()
            if r is not None:
                frame, seq = r
                if not checked:
                    checked = True
                    try:
                        self.self_check()
                    except Exception:
                        logger.exception("自检异常")
                if self.continuous_detect and self._detect_lock.acquire(blocking=False):
                    try:
                        self._update_tray([frame], force=False)
                        det = self.detector
                        targets = det.detect_fast(frame) if hasattr(det, "detect_fast") else det.detect(frame)
                        ann = det.annotate(frame, targets)
                    except Exception:
                        logger.exception("预览识别异常")
                        targets, ann = [], frame
                    finally:
                        self._detect_lock.release()
                    with self._lock:
                        self._latest_targets, self._latest_annotated = targets, ann
                    n_win += 1
                    if targets:
                        try:
                            self.bus.publish(TOPIC_VISION_TARGET, targets[0], sender="vision")
                        except Exception:
                            pass
            if now - t_win >= 1.0:
                self._fps = n_win / (now - t_win)
                n_win, t_win = 0, now
            if now - last_hb >= 1.0:
                last_hb = now
                self._heartbeat()

    def _update_tray(self, frames, force: bool = True):
        """托盘模式: 用新帧刷新托盘定位; 未手动设 ROI 时把托盘内框设为 ROI。调用方持有 _detect_lock。"""
        cal = self.calibrator
        if not getattr(cal, "tray_mode", False) or not frames:
            return
        try:
            todo = frames if not cal.tray_tracker.locked_live else frames[-1:]
            before = None if cal.tray_H is None else cal.tray_H.copy()
            for f in todo:
                cal.update_tray(f, force=force)
            ev = cal.tray_status.pop("event", "")
            if ev:
                # 只保留最新一条 (文字带位移数值, 追加到 warnings 会去重失效、自检永远不过)
                self._tray_event = (time.time(), ev)
                logger.warning("视觉: %s", ev)
            changed = cal.tray_H is not None and (before is None or not np.allclose(before, cal.tray_H))
            if changed and cal.tray_cfg.get("auto_roi", True) and not self.cfg.detect.get("roi") \
                    and hasattr(self.detector, "set_roi"):
                self.detector.set_roi(cal.tray_roi_px())
        except Exception:
            logger.exception("托盘定位异常")

    def _heartbeat(self):
        g = self._grabber
        ok = bool(g and g.connected)
        try:
            with self._lock:
                n = len(self._latest_targets)
            self.bus.publish(
                TOPIC_MONITOR_HEARTBEAT,
                ModuleHeartbeat(module_name="vision", status="OK" if ok else "ERROR", fps=self.fps,
                                details={"target_count": n, "camera_fps": round(g.fps, 1) if g else 0.0,
                                         "camera": self._cam_cfg.get("type"),
                                         "reconnects": g.reconnects if g else 0,
                                         "last_error": (g.last_error if g else "") or ""}),
                sender="vision")
        except Exception:
            pass

    # ------------------------------------------------------------------ 记录
    @property
    def record_dir(self) -> str:
        d = self.cfg.record.get("dir", "records/scans")
        return d if os.path.isabs(d) else os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), d)

    def _enqueue_record(self, raw, ann, targets):
        if not self.cfg.record.get("enabled") or raw is None:
            return
        try:
            self._rec_q.put_nowait((time.time(), raw, ann, [asdict(t) for t in targets]))
        except queue.Full:
            pass

    def _record_loop(self):
        d = self.record_dir
        keep = int(self.cfg.record.get("keep", 300))
        try:
            os.makedirs(os.path.join(d, "raw"), exist_ok=True)
            os.makedirs(os.path.join(d, "ann"), exist_ok=True)
        except Exception as e:
            logger.warning("记录目录不可用 %s: %s", d, e)
            return
        while True:
            ts, raw, ann, tj = self._rec_q.get()
            name = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts)) + f"_{int(ts * 1000) % 1000:03d}"
            try:
                cv2.imwrite(os.path.join(d, "raw", name + ".jpg"), raw, [cv2.IMWRITE_JPEG_QUALITY, 95])
                if ann is not None:
                    cv2.imwrite(os.path.join(d, "ann", name + ".jpg"), ann, [cv2.IMWRITE_JPEG_QUALITY, 80])
                with open(os.path.join(d, "ann", name + ".json"), "w", encoding="utf-8") as f:
                    json.dump(tj, f, ensure_ascii=False, default=str)
                self._rec_n += 1
                if self._rec_n % 20 == 0:
                    raws = sorted(os.listdir(os.path.join(d, "raw")))
                    for old in raws[:-keep] if len(raws) > keep else []:
                        base = os.path.splitext(old)[0]
                        for p in (os.path.join(d, "raw", old), os.path.join(d, "ann", base + ".jpg"),
                                  os.path.join(d, "ann", base + ".json")):
                            try:
                                os.remove(p)
                            except OSError:
                                pass
            except Exception as e:
                logger.debug("记录失败: %s", e)


# ==============================================================================
# 后端注册
# ==============================================================================

def _yolo_detector(calibrator=None):
    from .yolo_detector import YoloWorkpieceDetector
    return YoloWorkpieceDetector(calibrator)


def _yolo_service(bus=None, **kw):
    return VisionService(bus, detector_factory=_yolo_detector, **kw)


def _hsv_service(bus=None, **kw):
    return VisionService(bus, **kw)


register_vision_backend(VisionBackend(
    name="hsv_color", display_name="HSV 颜色分割 (无模型兜底)",
    service_factory=_hsv_service, camera_factory=lambda **kw: PhysicalCamera(**kw),
    calibrator_factory=CoordinateCalibrator, detector_factory=WorkpieceDetector))

register_vision_backend(VisionBackend(
    name="yolo", display_name="YOLOv8 (RDK X5 BPU / ONNX) + 颜色 + 精定位",
    service_factory=_yolo_service, camera_factory=lambda **kw: PhysicalCamera(**kw),
    calibrator_factory=CoordinateCalibrator, detector_factory=_yolo_detector))
