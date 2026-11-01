"""
STM32 智能分拣系统 - 视觉模块接口契约 (Vision Interfaces)
================================================================
本文件是 **视觉模块与上层系统之间的唯一契约**。

内部视觉模块需要被整体接替时，接替方只需保证本文件中的抽象基类被正确实现
(或者注册一个全新的后端)，上层 (消息中心 / 调试界面 / 业务闭环) 无需任何改动。

------------------------------------------------------------------------------
1. 四个抽象基类 (ABC)
------------------------------------------------------------------------------
    VisionCamera          图像输入源 (真实 USB 相机 / 虚拟仿真源 / 工业相机 SDK)
    VisionCalibrator      像素坐标 (u, v)  <->  机械臂物理坐标 (x_mm, y_mm) 的映射
    WorkpieceDetectorBase 工件识别与定位 (HSV 分割 / 深度学习 / 模板匹配 ...)
    VisionServiceBase     视觉后台服务, 对上层暴露的唯一门面 (Facade)

    上层 (debug 界面、业务路由器) **只** 依赖 VisionServiceBase;
    其余三个基类是 VisionServiceBase 的可替换组成部分。

------------------------------------------------------------------------------
2. 数据契约 VisionTarget
------------------------------------------------------------------------------
    re-export 自 MessegeCenter.topics.VisionTarget, **不重复定义**。
    必须复用同一份类: MessegeCenter/router.py 通过 isinstance(payload, VisionTarget)
    判断分拣任务的载荷, 若另建一份类会导致 isinstance 静默失效。

------------------------------------------------------------------------------
3. 消息总线约定 (接替方必须遵守, 否则看门狗会误报警)
------------------------------------------------------------------------------
    - 心跳:  每 1 秒向 TOPIC_MONITOR_HEARTBEAT 发布
             ModuleHeartbeat(module_name="vision", status="OK", fps=..., details=...)
             若缺失, moniter/health.py 的 3 秒看门狗会判定 vision 模块离线,
             并触发 code=1001 的 WARNING 告警。
    - 目标:  每检测到一批工件, 向 TOPIC_VISION_TARGET 发布 List[VisionTarget]
             或优先级最高的单个 VisionTarget, sender 建议填 "vision"。

------------------------------------------------------------------------------
4. 接入方式 (两种)
------------------------------------------------------------------------------
    (A) 替换组件实现 —— 保留原有 camera/detector/calibration 分层, 只换算法:
            class MyDetector(WorkpieceDetectorBase):
                ...
            register_vision_backend(VisionBackend(
                name="my_detector",
                display_name="我的检测算法",
                service_factory=..., camera_factory=...,
                calibrator_factory=..., detector_factory=MyDetector,
            ))

    (B) 整体替换实现 —— 连服务门面一起换掉 (例如改用外部视觉服务):
            class MyVisionService(VisionServiceBase):
                ...
            register_vision_backend(VisionBackend(
                name="external",
                display_name="外部视觉服务",
                service_factory=MyVisionService,
                ...
            ))

    之后上层只需 create_vision_service(bus, backend="my_detector") 即可拿到新实现。

------------------------------------------------------------------------------
5. 内置后端
------------------------------------------------------------------------------
    "yolo"       YOLOv8 (RDK X5 BPU .bin / ONNX / .pt) + HSV 颜色 + 精定位, 比赛默认;
                 模型缺失时自动退回 HSV, 不会崩。
    "hsv_color"  纯 HSV 颜色分割 (无需模型, 调试界面兜底)
    默认后端: vision_config.json 的 "backend", 可被环境变量 IS_VISION_BACKEND 覆盖。

------------------------------------------------------------------------------
6. 比赛流程使用的扩展接口 (VisionServiceBase 中带默认实现, 接替方建议覆盖)
------------------------------------------------------------------------------
    get_fresh_frames(n, timeout_s) -> List[BGR]
        返回调用之后才采到的 n 帧新图 (机械臂刚移开时不能用旧帧)。
    scan(n_frames, skip_frames) -> (List[VisionTarget], 参考帧 BGR)
        按需多帧融合识别, 比赛流程 **唯一** 调用的识别接口。永不抛异常。
        返回的每个 VisionTarget 必须填好 x_mm/y_mm、label (大屏中文名)、
        category (分拣类别键: 形状|颜色|信息, 相同才进同一盒)、bbox。
    create_vision_service(..., continuous_detect=False)
        后台线程只采图不推理, 推理全部走 scan() (避免与主流程争抢 BPU)。

------------------------------------------------------------------------------
7. 相机 VisionCamera (可插拔: USB/UVC、海康 MVS 工业相机、图片回放、仿真)
------------------------------------------------------------------------------
    必须实现: open / close / is_opened / read
    可选实现 (默认安全空实现, 返回 False 表示不支持):
        set_exposure(us) / set_gain(db) / set_white_balance(kelvin|None=自动)
        info -> CameraInfo  (型号/序列号/分辨率/是否硬件触发 ...)
    采集线程/掉线重连/新帧判定由 vision/cameras/grabber.py 统一完成,
    相机实现只管"读一帧", 不需要自己开线程。
    新相机注册: vision.cameras.register_camera("my_cam", factory)

------------------------------------------------------------------------------
8. 推理引擎 InferenceEngineBase
------------------------------------------------------------------------------
    YOLO 检测器内部可替换的推理引擎 (BPU / ONNX / TensorRT ...):
        is_ready: bool       infer(bgr) -> List[Detection]       describe() -> str
    Detection 字段: cls_id, name, conf, x1, y1, x2, y2 (原图像素坐标, 已 NMS)。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

# 数据契约的单一来源 —— 消息中心已定义, 此处仅 re-export。
# (见 MessegeCenter/topics.py)
from MessegeCenter.topics import (
    TOPIC_MONITOR_HEARTBEAT,
    TOPIC_VISION_TARGET,
    ModuleHeartbeat,
    VisionTarget,
)


# ==============================================================================
# 1. HSV 阈值描述 (调试界面 HSV 滑块直接依赖此结构)
# ==============================================================================

@dataclass
class ColorRange:
    """
    HSV 颜色阈值范围

    注意: OpenCV 的 H 取值范围是 0~179 (而非 0~360)。

    红色横跨色相环 0/180 边界, 因此提供第二组区间 lower2/upper2,
    不需要该功能时保持 None 即可。
    """
    name: str
    lower: np.ndarray
    upper: np.ndarray
    lower2: Optional[np.ndarray] = None
    upper2: Optional[np.ndarray] = None


@dataclass
class VisionCapabilities:
    """
    后端能力声明 —— 调试界面据此决定显示哪些控件。

    接替方若改用深度学习等无 HSV 阈值概念的实现, 应把
    supports_hsv_tuning / supports_mask_preview 置为 False,
    视觉调试界面会隐藏相关面板而不是报错。
    """
    supports_hsv_tuning: bool = True
    supports_mask_preview: bool = True
    requires_physical_camera: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


# ==============================================================================
# 2. 抽象基类
# ==============================================================================

@dataclass
class CameraInfo:
    """相机描述 (日志 / 调试界面 / 自检报告使用)。"""
    kind: str = "unknown"          # uvc | hikrobot | replay | synthetic | ...
    model: str = ""
    serial: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Detection:
    """推理引擎输出的一个检测框 (原图像素坐标)。"""
    cls_id: int
    name: str
    conf: float
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) * 0.5

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) * 0.5

    @property
    def w(self) -> float:
        return self.x2 - self.x1

    @property
    def h(self) -> float:
        return self.y2 - self.y1


class VisionCamera(ABC):
    """
    图像输入源接口。

    实现者需保证 read() 返回 BGR 三通道 uint8 图像 (OpenCV 约定)。
    read() 允许阻塞, 但单次不应超过 ~1 秒 (采集线程据此判断掉线)。
    """

    # -- 可选控制项: 默认不支持 (返回 False), 工业相机实现时覆盖 --------------
    def set_exposure(self, exposure_us: Optional[float]) -> bool:
        """设置曝光时间 (微秒); None = 自动曝光。"""
        return False

    def set_gain(self, gain_db: Optional[float]) -> bool:
        """设置增益 (dB); None = 自动。"""
        return False

    def set_white_balance(self, kelvin: Optional[float]) -> bool:
        """设置白平衡色温; None = 自动。比赛建议锁定, 防止颜色漂移。"""
        return False

    @property
    def info(self) -> CameraInfo:
        try:
            w, h = self.frame_size
        except Exception:
            w, h = 0, 0
        return CameraInfo(kind=type(self).__name__, width=w, height=h)

    def describe(self) -> str:
        i = self.info
        return f"{i.kind} {i.model} {i.width}x{i.height}".strip()

    @abstractmethod
    def open(self) -> bool:
        """打开图像源。成功返回 True, 失败返回 False (不要抛异常)。"""

    @abstractmethod
    def close(self):
        """释放底层资源, 须可重复调用。"""

    @abstractmethod
    def is_opened(self) -> bool:
        """当前图像源是否可用。"""

    @abstractmethod
    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        """读取一帧。返回 (是否成功, BGR 图像); 失败时第二项为 None。"""

    @property
    def frame_size(self) -> Tuple[int, int]:
        """(宽, 高) 像素尺寸, 用于界面缩放与标定预置。"""
        raise NotImplementedError


class VisionCalibrator(ABC):
    """
    像素坐标与机械臂物理坐标的映射器 (标定器)。
    """

    @abstractmethod
    def pixel_to_robot(self, u: float, v: float) -> Tuple[float, float]:
        """图像像素坐标 (u, v) -> 机械臂台面坐标 (x_mm, y_mm)。"""

    @abstractmethod
    def robot_to_pixel(self, x_mm: float, y_mm: float) -> Tuple[float, float]:
        """机械臂台面坐标 (x_mm, y_mm) -> 图像像素坐标 (u, v)。"""

    def set_calibration_points(
        self,
        src_pts: List[Tuple[float, float]],
        dst_pts: List[Tuple[float, float]]
    ):
        """设置新的标定点对 (像素 -> 毫米)。"""
        raise NotImplementedError

    def save_config(self):
        """持久化标定参数。"""
        raise NotImplementedError

    def load_config(self) -> bool:
        """从持久化存储载入标定参数, 成功返回 True。"""
        raise NotImplementedError


class WorkpieceDetectorBase(ABC):
    """
    工件检测与定位器。

    color_ranges / get_color_mask / update_color_range 三者被调试界面的
    HSV 滑块面板直接调用, 因此属于 UI 契约的一部分, 而非单纯的算法细节。
    若实现不支持 HSV 调参, 请在 VisionCapabilities 中声明
    supports_hsv_tuning=False, 并让这三个成员退化为安全的空实现。
    """

    #: 颜色名 -> ColorRange 的映射 (如 {"red": ColorRange(...), ...})
    color_ranges: Dict[str, ColorRange]

    @abstractmethod
    def get_color_mask(self, hsv: np.ndarray, color_name: str) -> np.ndarray:
        """获取指定颜色的二值掩膜 (uint8, 0/255)。"""

    @abstractmethod
    def update_color_range(self, color_name: str, lower: List[int], upper: List[int]):
        """动态更新某颜色的 HSV 阈值。"""

    @abstractmethod
    def detect(self, bgr_frame: np.ndarray) -> List[VisionTarget]:
        """
        在 BGR 图像中检测所有工件。

        返回的每个 VisionTarget 必须填满 x_mm / y_mm (即已完成标定换算),
        上层会直接把 x_mm/y_mm 下发给机械臂。
        """

    @abstractmethod
    def annotate(self, bgr_frame: np.ndarray, targets: List[VisionTarget]) -> np.ndarray:
        """绘制标注图像 (检测框/质心/坐标读数), 供界面显示。"""


class VisionServiceBase(ABC):
    """
    视觉后台服务门面 —— **上层系统唯一依赖的视觉接口**。

    生命周期:
        service = create_vision_service(bus)   # 或 VisionService(bus)
        service.set_camera_source(use_synthetic=True)
        service.start()
        ...
        service.stop()

    线程约定:
        start() 之后内部自行开线程采集与识别; 所有查询方法必须线程安全,
        可在工作线程中阻塞 (内部需自行加锁), 但不得阻塞调用方过久。
    """

    def __init__(self, bus: Optional[Any] = None):
        self.bus = bus
        self._lock = threading.Lock()

    # -- 状态查询 ------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        """采集与识别线程是否正在运行。"""
        raise NotImplementedError

    @property
    def fps(self) -> float:
        """当前识别帧率 (帧/秒), 未运行时为 0.0。"""
        raise NotImplementedError

    @property
    def capabilities(self) -> VisionCapabilities:
        """声明本实现支持哪些能力, 调试界面据此调整界面。"""
        return VisionCapabilities()

    # -- 生命周期 ------------------------------------------------------------
    @abstractmethod
    def set_camera_source(self, use_synthetic: bool = True, device_id: int = 0):
        """切换图像输入源。若正在运行, 内部负责重启采集。"""

    @abstractmethod
    def start(self):
        """启动采集与识别。失败时应自行记录日志, 不向外抛异常。"""

    @abstractmethod
    def stop(self):
        """停止采集与识别, 并释放图像源。须可重复调用。"""

    # -- 数据查询 ------------------------------------------------------------
    @abstractmethod
    def get_latest_frame_and_targets(self) -> Tuple[Optional[np.ndarray], List[VisionTarget]]:
        """
        取最新一帧 **标注后** 的图像与本帧识别到的目标列表。

        返回值必须是副本或不可变快照: 调用方会在自己的线程里使用图像,
        若直接返回内部缓冲区会与采集线程竞争。
        """

    def get_latest_raw_frame(self) -> Optional[np.ndarray]:
        """
        取最新一帧 **未标注** 的原始图像 (用于 HSV 掩膜预览等二次分析)。

        同样必须返回副本。若后端不做二次分析, 返回 None 即可。
        """
        return None

    def get_fresh_frames(self, n: int = 1, timeout_s: float = 2.0) -> List[np.ndarray]:
        """返回 n 帧调用之后才采集到的新原图 (副本)。默认实现: 轮询 get_latest_raw_frame。"""
        import time as _t
        frames: List[np.ndarray] = []
        deadline = _t.time() + timeout_s
        cur = self.get_latest_raw_frame()
        prev = cur[::16, ::16].copy() if cur is not None else None
        while len(frames) < n and _t.time() < deadline:
            f = self.get_latest_raw_frame()
            if f is not None:
                sig = f[::16, ::16]
                if prev is None or not np.array_equal(sig, prev):
                    frames.append(f)
                    prev = sig.copy()
            _t.sleep(0.005)
        return frames

    def scan(self, n_frames: int = 5, skip_frames: int = 1) -> Tuple[List[VisionTarget], Optional[np.ndarray]]:
        """
        按需识别 (比赛流程唯一调用的识别接口), 永不抛异常。
        默认实现: 取 1 帧新图交给 detector.detect; 内置 VisionService 覆盖为多帧融合。
        """
        frames = self.get_fresh_frames(1)
        if not frames or self.detector is None:
            return [], None
        try:
            return self.detector.detect(frames[-1]), frames[-1]
        except Exception:
            return [], frames[-1]

    @property
    def detector(self) -> Optional[WorkpieceDetectorBase]:
        """底层工件检测器实例 (供 HSV 调参面板访问 color_ranges 等契约)。"""
        return getattr(self, "_detector", None)

    @detector.setter
    def detector(self, value: Optional[WorkpieceDetectorBase]):
        self._detector = value


class InferenceEngineBase(ABC):
    """
    目标检测推理引擎 (YoloWorkpieceDetector 的可替换组成部分)。

    infer() 返回的 Detection 坐标必须已还原到 **输入原图** 像素坐标系
    (letterbox / 缩放由引擎内部处理), 且已做 NMS。
    """

    is_ready: bool = False
    last_error: str = ""
    class_names: List[str]

    @abstractmethod
    def infer(self, bgr: np.ndarray) -> List[Detection]:
        """BGR uint8 图 -> List[Detection]。未就绪时返回 []。不得抛异常给调用方。"""

    def describe(self) -> str:
        """一行状态说明 (后端 / 模型 / 各阶段耗时), 用于日志与界面。"""
        return type(self).__name__


# ==============================================================================
# 3. 后端注册表
# ==============================================================================

@dataclass
class VisionBackend:
    """
    一个可插拔的视觉实现。

    service_factory  必填: 无参或接受 bus 的可调用对象, 返回 VisionServiceBase
                     (约定签名: service_factory(bus) -> VisionServiceBase)
    其余三个 factory 选填, 主要供调试界面与测试构造独立组件使用。
    """
    name: str
    display_name: str
    service_factory: Callable[..., VisionServiceBase]
    camera_factory: Optional[Callable[..., VisionCamera]] = None
    calibrator_factory: Optional[Callable[..., VisionCalibrator]] = None
    detector_factory: Optional[Callable[..., WorkpieceDetectorBase]] = None


DEFAULT_BACKEND = "hsv_color"

_BACKENDS: Dict[str, VisionBackend] = {}
_BACKENDS_LOCK = threading.Lock()


def register_vision_backend(backend: VisionBackend, *, replace: bool = True):
    """注册 (或替换) 一个视觉后端。同名后端默认允许覆盖。"""
    if not backend.name:
        raise ValueError("后端名称不能为空")
    if not callable(backend.service_factory):
        raise TypeError(f"后端 {backend.name!r} 的 service_factory 必须可调用")
    with _BACKENDS_LOCK:
        if backend.name in _BACKENDS and not replace:
            raise ValueError(f"后端 {backend.name!r} 已存在; 传 replace=True 覆盖")
        _BACKENDS[backend.name] = backend


def list_vision_backends() -> List[str]:
    """列出所有已注册的后端名称。"""
    with _BACKENDS_LOCK:
        return list(_BACKENDS.keys())


def get_vision_backend(name: Optional[str] = None) -> VisionBackend:
    """按名称取后端; name 为 None 时返回默认后端。"""
    key = name or DEFAULT_BACKEND
    with _BACKENDS_LOCK:
        if key not in _BACKENDS:
            raise KeyError(
                f"未注册名为 {key!r} 的视觉后端; 已注册: {list(_BACKENDS.keys())}"
            )
        return _BACKENDS[key]


def create_vision_service(bus: Optional[Any] = None, backend: Optional[str] = None,
                          **kwargs) -> VisionServiceBase:
    """
    按后端名称构造视觉服务 —— **上层获取视觉能力的推荐入口**。

    # 替换整个视觉实现时, 上层唯一需要改的就是这里的参数:
    service = create_vision_service(bus, backend="my_detector")
    """
    import os
    key = backend or os.environ.get("IS_VISION_BACKEND") or None
    if key is None:
        try:
            from .config import load_vision_config
            key = load_vision_config().backend
        except Exception:
            key = None
    factory = get_vision_backend(key).service_factory
    return factory(bus, **kwargs) if kwargs else factory(bus)


__all__ = [
    # 数据契约
    "VisionTarget",
    "ColorRange",
    "VisionCapabilities",
    "CameraInfo",
    "Detection",
    # 抽象基类
    "VisionCamera",
    "VisionCalibrator",
    "WorkpieceDetectorBase",
    "VisionServiceBase",
    "InferenceEngineBase",
    # 注册表
    "VisionBackend",
    "DEFAULT_BACKEND",
    "register_vision_backend",
    "list_vision_backends",
    "get_vision_backend",
    "create_vision_service",
    # 消息中心常量 (供实现方发布心跳/目标时引用)
    "TOPIC_MONITOR_HEARTBEAT",
    "TOPIC_VISION_TARGET",
    "ModuleHeartbeat",
]