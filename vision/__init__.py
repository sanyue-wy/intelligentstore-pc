"""
STM32 智能分拣系统 - 视觉模块

对外契约: interfaces.py (上层只通过 create_vision_service() 获取视觉服务)。
使用说明: GUIDE.md
"""

import os as _os

# RDK X5 容器里的 OpenCL 上下文不可用: cv2 先报 "can't create cl_mem handle", 之后
# undistortPoints / initUndistortRectifyMap 会**无限挂起** (实测 vision 测试卡在畸变校正那一条,
# 禁用后 0.45s 通过)。畸变页「一键校正」走的正是这条路径, 所以必须在 import cv2 前关掉。
# 关掉只影响 OpenCV 的 GPU 加速, YOLO 走 BPU (hbm_runtime), 不受影响。
_os.environ.setdefault("OPENCV_OPENCL_RUNTIME", "disabled")

from .interfaces import (
    CameraInfo,
    ColorRange,
    Detection,
    InferenceEngineBase,
    VisionCapabilities,
    VisionCamera,
    VisionCalibrator,
    WorkpieceDetectorBase,
    VisionServiceBase,
    VisionBackend,
    VisionTarget,
    DEFAULT_BACKEND,
    register_vision_backend,
    list_vision_backends,
    get_vision_backend,
    create_vision_service,
)
from .config import VisionConfig, load_vision_config
from .calibration import CoordinateCalibrator
from .cameras import (FrameGrabber, ReplayCamera, create_camera, list_cameras,
                      register_camera)
from .camera import BaseCamera, PhysicalCamera, SyntheticCamera
from .detector import (
    WorkpieceDetector,
    VISION_SCALE_ENV,
    DEFAULT_VISION_SCALE,
    sanitize_vision_scale,
    resolve_vision_scale,
)
from .service import VisionService
from .yolo_engine import YoloEngine, is_bpu_available
from .yolo_detector import YoloWorkpieceDetector

# 兜底: 若 cv2 在本包之前就被 import 过, 上面的环境变量已不生效, 再用运行时开关关一次。
try:
    import cv2 as _cv2

    _cv2.ocl.setUseOpenCL(False)
except Exception:      # cv2 未安装或没有 ocl 模块
    pass

__all__ = [
    # ---- 接口契约 ----
    "VisionCamera", "VisionCalibrator", "WorkpieceDetectorBase", "VisionServiceBase",
    "InferenceEngineBase", "VisionBackend", "VisionCapabilities", "VisionTarget",
    "ColorRange", "CameraInfo", "Detection", "DEFAULT_BACKEND",
    "register_vision_backend", "list_vision_backends", "get_vision_backend",
    "create_vision_service",
    # ---- 配置 ----
    "VisionConfig", "load_vision_config",
    # ---- 相机 ----
    "FrameGrabber", "register_camera", "list_cameras", "create_camera",
    "BaseCamera", "PhysicalCamera", "SyntheticCamera", "ReplayCamera",
    # ---- 实现 ----
    "CoordinateCalibrator", "WorkpieceDetector", "VisionService",
    "YoloEngine", "YoloWorkpieceDetector", "is_bpu_available",
    # ---- HSV 降采样开关 ----
    "VISION_SCALE_ENV", "DEFAULT_VISION_SCALE", "sanitize_vision_scale", "resolve_vision_scale",
]
