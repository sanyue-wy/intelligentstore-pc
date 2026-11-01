"""兼容层: 旧代码 `from vision.camera import PhysicalCamera, SyntheticCamera` 仍可用。
新代码请使用 vision.cameras (注册表 + 工厂 + FrameGrabber)。"""
from .cameras.uvc import (BaseCamera, PhysicalCamera, available_capture_backends,  # noqa: F401
                          capture_backend_candidates, list_video_devices)
from .cameras.synthetic import SyntheticCamera  # noqa: F401
from .cameras.replay import ReplayCamera  # noqa: F401
