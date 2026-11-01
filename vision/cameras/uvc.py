"""
USB / UVC 摄像头 (OpenCV VideoCapture, Linux V4L2 优先)。

注册名: "uvc"。接口契约见 vision/interfaces.py 的 VisionCamera。
采集线程 / 掉线重连由 cameras/grabber.py 统一负责, 本类只负责"读一帧"。

跨平台说明 (RDK X5 / Linux 适配):
    Windows 上 DirectShow (CAP_DSHOW) 打开 USB 摄像头更快, 但 CAP_DSHOW 是
    Windows 专有后端, 在 Linux 下 cv2.VideoCapture(id, cv2.CAP_DSHOW) 必然
    返回 not opened。因此本模块按平台选择后端, 并保留多后端回退:
        Linux   : CAP_V4L2 -> CAP_GSTREAMER(若编译) -> CAP_ANY
        Windows : CAP_DSHOW -> CAP_MSMF -> CAP_ANY
        macOS   : CAP_AVFOUNDATION -> CAP_ANY
    每次尝试的后端名与失败原因都会记录在 ``last_error`` / ``backend_name`` 中,
    便于无硬件环境定位问题 (不要静默吞掉)。
"""

from __future__ import annotations
import os
import sys
import glob
import time
import math
import logging
import threading
from typing import Tuple, Optional, List

import cv2
import numpy as np

from ..interfaces import CameraInfo, VisionCamera

logger = logging.getLogger(__name__)


# ==============================================================================
# 平台与后端探测
# ==============================================================================

IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")
IS_DARWIN = sys.platform == "darwin"


def available_capture_backends() -> List[int]:
    """返回本机 OpenCV 实际编译进来的 VideoCapture 后端 (过滤掉不可用的)。"""
    names = [
        ("CAP_V4L2", getattr(cv2, "CAP_V4L2", None)),
        ("CAP_DSHOW", getattr(cv2, "CAP_DSHOW", None)),
        ("CAP_MSMF", getattr(cv2, "CAP_MSMF", None)),
        ("CAP_AVFOUNDATION", getattr(cv2, "CAP_AVFOUNDATION", None)),
        ("CAP_GSTREAMER", getattr(cv2, "CAP_GSTREAMER", None)),
        ("CAP_ANY", getattr(cv2, "CAP_ANY", 0)),
    ]
    backends: List[int] = []
    for _name, api in names:
        if api is not None and api not in backends:
            backends.append(api)
    return backends


def capture_backend_candidates() -> List[Tuple[int, str]]:
    """
    按当前平台给出 VideoCapture 后端的尝试顺序: [(apiPreference, 名称), ...]

    CAP_ANY 永远放在最后兜底 —— 交给 OpenCV 自选后端, 保证即使平台判断
    有误也仍有一次机会打开设备。
    """
    cands: List[Tuple[int, str]] = []

    def _add(name: str):
        api = getattr(cv2, name, None)
        if api is not None and all(api != a for a, _ in cands):
            cands.append((api, name))

    if IS_LINUX:
        _add("CAP_V4L2")
        _add("CAP_GSTREAMER")
    elif IS_WINDOWS:
        _add("CAP_DSHOW")
        _add("CAP_MSMF")
    elif IS_DARWIN:
        _add("CAP_AVFOUNDATION")

    _add("CAP_ANY")
    return cands


def list_video_devices() -> List[str]:
    """
    枚举本机可用的视频设备节点 (Linux 下扫描 /dev/video*)。

    Windows/macOS 没有设备节点概念, 返回空列表 (改用整数索引)。
    """
    if not IS_LINUX:
        return []
    try:
        return sorted(glob.glob("/dev/video*"))
    except OSError:
        return []


class BaseCamera(VisionCamera):
    """相机接口基类"""
    def open(self) -> bool:
        raise NotImplementedError
    def close(self):
        raise NotImplementedError
    def is_opened(self) -> bool:
        raise NotImplementedError
    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        raise NotImplementedError
    @property
    def frame_size(self) -> Tuple[int, int]:
        return (self.width, self.height)


class PhysicalCamera(BaseCamera):
    """
    真实物理 USB 摄像头 (跨平台)。

    device_id 可以是:
        int     —— 设备索引, 如 0 -> /dev/video0 (Linux) / 第 0 个摄像头 (Windows)
        str     —— 设备路径 (Linux), 如 "/dev/video0"

    backend 为 None 时按平台自动选择 (Linux -> CAP_V4L2, Windows -> CAP_DSHOW),
    也可显式传入 cv2.CAP_* 强制指定, 便于排障。

    长期稳定运行相关:
        - open() 会依次尝试多个后端, 全部失败才返回 False, 并把每次失败原因
          汇总到 last_error, 绝不会抛异常给调用方。
        - read() 连续失败达到 max_read_failures 次会自动重开设备 (一次),
          避免 USB 摄像头偶发掉线导致采集线程永久空转。
    """

    def __init__(self, device_id=0, width: int = 640, height: int = 480,
                 backend: Optional[int] = None, fps: Optional[int] = None,
                 fourcc: Optional[str] = None, max_read_failures: int = 30,
                 auto_reopen: bool = True, warmup_reads: int = 3):
        self.device_id = device_id
        self.width = width
        self.height = height
        self.backend_override = backend
        self.fps = fps
        self.fourcc = fourcc
        self.max_read_failures = max(1, int(max_read_failures))
        self.auto_reopen = auto_reopen
        self.warmup_reads = max(0, int(warmup_reads))

        self._cap: Optional[cv2.VideoCapture] = None
        self._lock = threading.Lock()

        # ---- 诊断信息 (供日志 / 调试界面 / 实测证据使用) ----
        self.backend_name: str = ""
        self.backend_value: int = 0
        self.last_error: str = ""
        self.open_attempts: List[str] = []
        self._consecutive_read_failures: int = 0
        self._reopen_count: int = 0

    # -- 内部工具 ------------------------------------------------------------
    def _target(self):
        """把 device_id 归一化成 VideoCapture 可接受的形式。"""
        dev = self.device_id
        if isinstance(dev, str) and dev.isdigit():
            return int(dev)
        return dev

    def _apply_properties(self, cap: cv2.VideoCapture):
        """设置期望的分辨率 / 帧率 / 像素格式; 全部非致命。"""
        if self.fourcc:
            try:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.fourcc))
            except Exception:
                pass
        if self.fps:
            try:
                cap.set(cv2.CAP_PROP_FPS, int(self.fps))
            except Exception:
                pass
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self.width))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.height))
        except Exception:
            pass
        # V4L2 默认缓存多帧会造成画面延迟累积, 尽量压到 1 帧
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

    def _probe_frame(self, cap: cv2.VideoCapture) -> bool:
        """打开后立刻读若干帧: 丢弃首帧 (曝光未稳定), 确认真的能出图。"""
        ok_any = False
        for _ in range(max(1, self.warmup_reads)):
            try:
                ok, frame = cap.read()
            except Exception:
                ok, frame = False, None
            if ok and frame is not None and getattr(frame, "size", 0) > 0:
                ok_any = True
        return ok_any

    # -- 生命周期 ------------------------------------------------------------
    def open(self) -> bool:
        with self._lock:
            self._release_locked()
            self.open_attempts = []
            self.last_error = ""
            self.backend_name = ""
            self.backend_value = 0
            self._consecutive_read_failures = 0

            target = self._target()
            candidates = (
                [(self.backend_override, f"CAP#{self.backend_override}")]
                if self.backend_override is not None
                else capture_backend_candidates()
            )

            for api, name in candidates:
                try:
                    cap = cv2.VideoCapture(target, api) if api is not None \
                        else cv2.VideoCapture(target)
                except Exception as e:  # 后端不被支持时 OpenCV 可能直接抛异常
                    self.open_attempts.append(f"{name}: 异常 {type(e).__name__}: {e}")
                    continue

                try:
                    opened = bool(cap.isOpened())
                except Exception:
                    opened = False

                if not opened:
                    self.open_attempts.append(f"{name}: 未打开 (isOpened=False)")
                    try:
                        cap.release()
                    except Exception:
                        pass
                    continue

                self._apply_properties(cap)

                if not self._probe_frame(cap):
                    self.open_attempts.append(f"{name}: 已打开但读不到有效帧")
                    try:
                        cap.release()
                    except Exception:
                        pass
                    continue

                self._cap = cap
                self.backend_name = name
                self.backend_value = api if api is not None else 0

                # 用设备真实生效的分辨率覆盖配置值
                try:
                    real_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                    real_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                    if real_w > 0 and real_h > 0:
                        self.width, self.height = real_w, real_h
                except Exception:
                    pass

                logger.info("摄像头已打开: device=%r backend=%s 分辨率=%dx%d",
                            self.device_id, name, self.width, self.height)
                return True

            if self.device_id is None:
                self.last_error = "未指定摄像头设备"
            elif not self.open_attempts:
                self.last_error = "当前 OpenCV 构建没有可用的 VideoCapture 后端"
            else:
                self.last_error = "; ".join(self.open_attempts)

            logger.warning("摄像头打开失败 device=%r -> %s", self.device_id, self.last_error)
            return False

    def _release_locked(self):
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

    def close(self):
        with self._lock:
            self._release_locked()
            self.backend_name = ""
            self.backend_value = 0

    def is_opened(self) -> bool:
        cap = self._cap
        if cap is None:
            return False
        try:
            return bool(cap.isOpened())
        except Exception:
            return False

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        cap = self._cap
        if cap is None:
            ok, frame = False, None
        else:
            try:
                ok, frame = cap.read()
            except Exception as e:
                ok, frame = False, None
                logger.debug("读取帧异常: %s", e)

        if ok and frame is not None and getattr(frame, "size", 0) > 0:
            self._consecutive_read_failures = 0
            return True, frame

        self._consecutive_read_failures += 1

        # USB 摄像头偶发掉帧/掉线/初始未打开: 达到阈值后自动重试打开, 保持自愈能力
        if self.auto_reopen and self._consecutive_read_failures >= self.max_read_failures:
            self._consecutive_read_failures = 0
            self._reopen_count += 1
            logger.warning("连续读取失败 %d 次, 尝试重新打开摄像头 (第 %d 次重连)",
                           self.max_read_failures, self._reopen_count)
            self.open()

        return False, None

    # -- 诊断 ----------------------------------------------------------------
    @property
    def fps_measured(self) -> float:
        """实时测量读帧帧率 (阻塞约 1 秒), 用于自检与证据采集。"""
        if not self.is_opened():
            return 0.0
        start = time.time()
        count = 0
        while time.time() - start < 1.0:
            ok, _ = self.read()
            if ok:
                count += 1
        dt = time.time() - start
        return count / dt if dt > 0 else 0.0

    # -- 可选控制 (V4L2; 不同摄像头支持程度不同, 失败返回 False) ------------
    def set_exposure(self, exposure_us) -> bool:
        cap = self._cap
        if cap is None:
            return False
        try:
            if exposure_us is None:
                return bool(cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3))       # V4L2: 3 = 自动
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)                         # V4L2: 1 = 手动
            # V4L2 exposure_absolute 单位是 100us
            return bool(cap.set(cv2.CAP_PROP_EXPOSURE, float(exposure_us) / 100.0))
        except Exception:
            return False

    def set_gain(self, gain_db) -> bool:
        cap = self._cap
        if cap is None or gain_db is None:
            return False
        try:
            return bool(cap.set(cv2.CAP_PROP_GAIN, float(gain_db)))
        except Exception:
            return False

    def set_white_balance(self, kelvin) -> bool:
        cap = self._cap
        if cap is None:
            return False
        try:
            if kelvin is None:
                return bool(cap.set(cv2.CAP_PROP_AUTO_WB, 1))
            cap.set(cv2.CAP_PROP_AUTO_WB, 0)
            return bool(cap.set(cv2.CAP_PROP_WB_TEMPERATURE, float(kelvin)))
        except Exception:
            return False

    @property
    def info(self) -> CameraInfo:
        return CameraInfo(kind="uvc", model=str(self.device_id), width=self.width,
                          height=self.height, fps=float(self.fps or 0),
                          extra={"backend": self.backend_name})

    def describe(self) -> str:
        """一行可读的相机状态描述 (日志/界面/证据用)。"""
        if self.is_opened():
            return (f"PhysicalCamera(device={self.device_id!r}, backend={self.backend_name}, "
                    f"size={self.width}x{self.height})")
        return (f"PhysicalCamera(device={self.device_id!r}, 未打开, "
                f"错误: {self.last_error or '未知'})")


