"""
STM32 智能分拣系统 - 视觉模块

对外契约: interfaces.py (上层只通过 create_vision_service() 获取视觉服务)。
使用说明: GUIDE.md
"""

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
