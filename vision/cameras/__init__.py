"""
相机抽象层: 注册表 + 工厂。

内置:
    "uvc"        USB/UVC 摄像头 (OpenCV V4L2)          cameras/uvc.py
    "hikrobot"   海康 MVS 工业相机 (GigE / USB3)        cameras/hikrobot.py
    "replay"     图片目录 / 视频回放 (离线调试)          cameras/replay.py
    "synthetic"  仿真色块画面 (无硬件联调)              cameras/synthetic.py

换相机 = 改 vision_config.json 的 camera.type (或环境变量 IS_CAM_TYPE), 代码不动。
接入新品牌 (大恒 / Basler / MindVision ...):
    class MyCam(VisionCamera): ...                     # 实现 open/close/is_opened/read
    register_camera("mycam", lambda cfg: MyCam(**cfg))  # cfg 是 camera 配置字典
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List

from ..interfaces import VisionCamera
from .grabber import FrameGrabber
from .replay import ReplayCamera
from .synthetic import SyntheticCamera
from .uvc import PhysicalCamera, list_video_devices

logger = logging.getLogger(__name__)

CameraFactory = Callable[[Dict[str, Any]], VisionCamera]
_REGISTRY: Dict[str, CameraFactory] = {}


def register_camera(name: str, factory: CameraFactory):
    _REGISTRY[name.lower()] = factory


def list_cameras() -> List[str]:
    return sorted(_REGISTRY)


def create_camera(cfg: Dict[str, Any]) -> VisionCamera:
    """按配置字典创建相机 (不打开)。cfg["type"] 选择实现, 其余键传给实现。"""
    kind = str(cfg.get("type", "uvc")).lower()
    if kind not in _REGISTRY:
        raise KeyError(f"未知相机类型 {kind!r}, 可选: {list_cameras()}")
    cam = _REGISTRY[kind](cfg)
    return cam


def apply_controls(cam: VisionCamera, cfg: Dict[str, Any]) -> Dict[str, bool]:
    """打开后统一下发曝光/增益/白平衡 (未配置的项保持相机默认)。返回各项是否生效。"""
    res = {}
    for key, fn in (("exposure_us", cam.set_exposure), ("gain_db", cam.set_gain),
                    ("wb_kelvin", cam.set_white_balance)):
        if cfg.get(key) is not None:
            try:
                res[key] = bool(fn(cfg[key]))
            except Exception:
                res[key] = False
            if not res[key]:
                logger.warning("相机不支持或设置失败: %s=%s", key, cfg[key])
    return res


def _uvc(cfg):
    return PhysicalCamera(device_id=cfg.get("device", 0), width=int(cfg.get("width", 640)),
                          height=int(cfg.get("height", 480)), fps=cfg.get("fps") or None,
                          fourcc=cfg.get("fourcc") or None, auto_reopen=False)


def _hik(cfg):
    from .hikrobot import HikrobotCamera
    return HikrobotCamera(**{k: v for k, v in cfg.items() if k != "type"})


def _replay(cfg):
    return ReplayCamera(path=cfg.get("path", ""), fps=float(cfg.get("fps") or 10),
                        loop=bool(cfg.get("loop", True)))   # 回放保持原图尺寸, 不拉伸


def _synthetic(cfg):
    return SyntheticCamera(width=int(cfg.get("width", 640)), height=int(cfg.get("height", 480)))


register_camera("uvc", _uvc)
register_camera("hikrobot", _hik)
register_camera("replay", _replay)
register_camera("synthetic", _synthetic)

__all__ = ["FrameGrabber", "PhysicalCamera", "SyntheticCamera", "ReplayCamera",
           "register_camera", "list_cameras", "create_camera", "apply_controls",
           "list_video_devices"]
