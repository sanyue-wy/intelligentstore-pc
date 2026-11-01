"""
海康机器人 (Hikrobot) MVS 工业相机 (注册名 "hikrobot")。

依赖: 板子上安装 MVS SDK (aarch64 版), 其 Python 封装 MvCameraControl_class.py 默认在
    /opt/MVS/Samples/aarch64/Python/MvImport     (也可用环境变量 MVS_PYTHON_DIR 指定)
相机 IP/USB 选择: serial 为空时取第一台; 否则按序列号匹配。

!! 本文件按 MVS SDK 3.x/4.x Python 接口编写, 未在真实相机上验证 !!
!! 拿到相机后先跑: python3 -m vision.tools_cli live --camera hikrobot              !!

设计要点 (比赛稳定/精度):
    * 连续采集 (TriggerMode=Off), 取最新帧; 读超时 1000ms -> 返回 False, 由 grabber 重连
    * 曝光/增益/白平衡全部手动锁定, 颜色判别不随环境光漂移
    * 支持 Mono8 / Bayer* / RGB8 / BGR8 / YUV422 像素格式, 统一转 BGR
    * 可选 ROI 与像素合并 (binning) 降低带宽, 由 SDK 节点名设置 (extra_nodes)
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np

from ..interfaces import CameraInfo, VisionCamera

logger = logging.getLogger(__name__)

_MVS_DIRS = [os.environ.get("MVS_PYTHON_DIR", ""),
             "/opt/MVS/Samples/aarch64/Python/MvImport",
             "/opt/MVS/Samples/64/Python/MvImport"]


def _import_mvs():
    for d in _MVS_DIRS:
        if d and os.path.isdir(d) and d not in sys.path:
            sys.path.append(d)
    try:
        import MvCameraControl_class as mvs  # type: ignore
        return mvs
    except Exception as e:
        logger.debug("MVS SDK 不可用: %s", e)
        return None


def is_hikrobot_available() -> bool:
    return _import_mvs() is not None


class HikrobotCamera(VisionCamera):
    def __init__(self, serial: str = "", exposure_us: Optional[float] = None,
                 gain_db: Optional[float] = None, wb_kelvin: Optional[float] = None,
                 timeout_ms: int = 1000, extra_nodes: Optional[Dict[str, Any]] = None,
                 swap_rb: bool = False, **_):
        self.serial = serial or ""
        self.exposure_us = exposure_us
        self.gain_db = gain_db
        self.wb_kelvin = wb_kelvin
        self.timeout_ms = int(timeout_ms)
        self.extra_nodes = extra_nodes or {}
        self.swap_rb = bool(swap_rb)          # 现场若红蓝颠倒, 配置 camera.swap_rb=true
        self._mvs = None
        self._cam = None
        self._buf = None
        self._buf_size = 0
        self._opened = False
        self.width = 0
        self.height = 0
        self.model = ""
        self.last_error = ""

    # ------------------------------------------------------------------ 打开
    def open(self) -> bool:
        self.close()
        mvs = self._mvs = _import_mvs()
        if mvs is None:
            self.last_error = "未找到 MVS SDK (MvCameraControl_class), 请安装海康 MVS 并设置 MVS_PYTHON_DIR"
            return False
        try:
            dev_list = mvs.MV_CC_DEVICE_INFO_LIST()
            ret = mvs.MvCamera.MV_CC_EnumDevices(mvs.MV_GIGE_DEVICE | mvs.MV_USB_DEVICE, dev_list)
            if ret != 0 or dev_list.nDeviceNum == 0:
                self.last_error = f"未枚举到工业相机 (ret=0x{ret & 0xffffffff:x})"
                return False
            chosen = None
            for i in range(dev_list.nDeviceNum):
                info = ctypes.cast(dev_list.pDeviceInfo[i], ctypes.POINTER(mvs.MV_CC_DEVICE_INFO)).contents
                sn, model = self._sn_model(info)
                if not self.serial or sn == self.serial:
                    chosen, self.model = info, f"{model} SN:{sn}"
                    break
            if chosen is None:
                self.last_error = f"没有序列号为 {self.serial} 的相机"
                return False
            cam = mvs.MvCamera()
            self._check(cam.MV_CC_CreateHandle(chosen), "CreateHandle")
            self._check(cam.MV_CC_OpenDevice(mvs.MV_ACCESS_Exclusive, 0), "OpenDevice")
            self._cam = cam
            if chosen.nTLayerType == mvs.MV_GIGE_DEVICE:
                pkt = cam.MV_CC_GetOptimalPacketSize()
                if int(pkt) > 0:
                    cam.MV_CC_SetIntValue("GevSCPSPacketSize", int(pkt))
            cam.MV_CC_SetEnumValue("TriggerMode", 0)                 # 连续采集
            for k, v in self.extra_nodes.items():                    # 例 {"BinningHorizontal": 2}
                self._set_node(k, v)
            self.set_exposure(self.exposure_us)
            self.set_gain(self.gain_db)
            self.set_white_balance(self.wb_kelvin)
            w, h = mvs.MVCC_INTVALUE(), mvs.MVCC_INTVALUE()
            cam.MV_CC_GetIntValue("Width", w)
            cam.MV_CC_GetIntValue("Height", h)
            self.width, self.height = int(w.nCurValue), int(h.nCurValue)
            payload = mvs.MVCC_INTVALUE()
            cam.MV_CC_GetIntValue("PayloadSize", payload)
            self._buf_size = int(payload.nCurValue) or self.width * self.height * 3
            self._buf = (ctypes.c_ubyte * self._buf_size)()
            cam.MV_CC_SetImageNodeNum(2)                              # 小缓冲, 降低延迟
            self._check(cam.MV_CC_StartGrabbing(), "StartGrabbing")
            self._opened = True
            logger.info("工业相机已打开: %s %dx%d", self.model, self.width, self.height)
            return True
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.error("工业相机打开失败: %s", self.last_error)
            self.close()
            return False

    @staticmethod
    def _sn_model(info) -> Tuple[str, str]:
        def _s(arr):
            return bytes(bytearray(arr)).split(b"\0", 1)[0].decode(errors="ignore")
        try:
            if info.nTLayerType == 1:   # GigE
                g = info.SpecialInfo.stGigEInfo
                return _s(g.chSerialNumber), _s(g.chModelName)
            u = info.SpecialInfo.stUsb3VInfo
            return _s(u.chSerialNumber), _s(u.chModelName)
        except Exception:
            return "", "unknown"

    def _check(self, ret, what):
        if ret != 0:
            raise RuntimeError(f"{what} 失败 ret=0x{ret & 0xffffffff:x}")

    def _set_node(self, name, value) -> bool:
        cam = self._cam
        if cam is None:
            return False
        try:
            if isinstance(value, bool):
                return cam.MV_CC_SetBoolValue(name, value) == 0
            if isinstance(value, int):
                return cam.MV_CC_SetIntValue(name, value) == 0 or cam.MV_CC_SetEnumValue(name, value) == 0
            if isinstance(value, float):
                return cam.MV_CC_SetFloatValue(name, value) == 0
            return cam.MV_CC_SetEnumValueByString(name, str(value)) == 0
        except Exception:
            return False

    # ------------------------------------------------------------------ 控制
    def set_exposure(self, exposure_us) -> bool:
        if self._cam is None:
            self.exposure_us = exposure_us
            return False
        if exposure_us is None:
            return self._cam.MV_CC_SetEnumValue("ExposureAuto", 2) == 0
        self._cam.MV_CC_SetEnumValue("ExposureAuto", 0)
        return self._cam.MV_CC_SetFloatValue("ExposureTime", float(exposure_us)) == 0

    def set_gain(self, gain_db) -> bool:
        if self._cam is None:
            self.gain_db = gain_db
            return False
        if gain_db is None:
            return self._cam.MV_CC_SetEnumValue("GainAuto", 2) == 0
        self._cam.MV_CC_SetEnumValue("GainAuto", 0)
        return self._cam.MV_CC_SetFloatValue("Gain", float(gain_db)) == 0

    def set_white_balance(self, kelvin) -> bool:
        """MVS 用 BalanceRatio 而不是色温: None=连续自动, 其它值=先自动一次再锁定。"""
        if self._cam is None:
            self.wb_kelvin = kelvin
            return False
        if kelvin is None:
            return self._cam.MV_CC_SetEnumValue("BalanceWhiteAuto", 1) == 0      # Continuous
        return self._cam.MV_CC_SetEnumValue("BalanceWhiteAuto", 2) == 0          # Once 后保持

    # ------------------------------------------------------------------ 读帧
    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        if not self._opened or self._cam is None:
            return False, None
        mvs = self._mvs
        fi = mvs.MV_FRAME_OUT_INFO_EX()
        ctypes.memset(ctypes.byref(fi), 0, ctypes.sizeof(fi))
        ret = self._cam.MV_CC_GetOneFrameTimeout(self._buf, self._buf_size, fi, self.timeout_ms)
        if ret != 0:
            return False, None
        try:
            return True, self._to_bgr(fi)
        except Exception as e:
            logger.warning("工业相机帧转换失败: %s", e)
            return False, None

    def _to_bgr(self, fi) -> np.ndarray:
        mvs = self._mvs
        w, h, n = int(fi.nWidth), int(fi.nHeight), int(fi.nFrameLen)
        raw = np.frombuffer(self._buf, dtype=np.uint8, count=n)
        pt = int(fi.enPixelType)
        g = lambda name: getattr(mvs, name, None)  # noqa: E731
        if pt == g("PixelType_Gvsp_Mono8"):
            return cv2.cvtColor(raw.reshape(h, w), cv2.COLOR_GRAY2BGR)
        if pt == g("PixelType_Gvsp_RGB8_Packed"):
            return cv2.cvtColor(raw.reshape(h, w, 3), cv2.COLOR_RGB2BGR)
        if pt == g("PixelType_Gvsp_BGR8_Packed"):
            return raw.reshape(h, w, 3).copy()
        # OpenCV 的 Bayer 命名比 GenICam 错开一位: GenICam BayerRG8 用 COLOR_BayerRG2RGB 得到的才是 BGR。
        bayer = {"PixelType_Gvsp_BayerRG8": cv2.COLOR_BayerRG2RGB,
                 "PixelType_Gvsp_BayerGR8": cv2.COLOR_BayerGR2RGB,
                 "PixelType_Gvsp_BayerGB8": cv2.COLOR_BayerGB2RGB,
                 "PixelType_Gvsp_BayerBG8": cv2.COLOR_BayerBG2RGB}
        for name, code in bayer.items():
            if pt == g(name):
                img = cv2.cvtColor(raw.reshape(h, w), code)
                return img[:, :, ::-1].copy() if self.swap_rb else img
        if pt == g("PixelType_Gvsp_YUV422_Packed"):
            return cv2.cvtColor(raw.reshape(h, w, 2), cv2.COLOR_YUV2BGR_UYVY)
        if pt == g("PixelType_Gvsp_YUV422_YUYV_Packed"):
            return cv2.cvtColor(raw.reshape(h, w, 2), cv2.COLOR_YUV2BGR_YUYV)
        raise RuntimeError(f"不支持的像素格式 0x{pt:x}, 请在 MVS 客户端把 PixelFormat 设为 BayerRG8 或 RGB8")

    # ------------------------------------------------------------------ 关闭
    def close(self):
        cam, self._cam = self._cam, None
        self._opened = False
        if cam is not None:
            for fn in ("MV_CC_StopGrabbing", "MV_CC_CloseDevice", "MV_CC_DestroyHandle"):
                try:
                    getattr(cam, fn)()
                except Exception:
                    pass

    def is_opened(self) -> bool:
        return self._opened

    @property
    def frame_size(self) -> Tuple[int, int]:
        return (self.width, self.height)

    @property
    def info(self) -> CameraInfo:
        return CameraInfo(kind="hikrobot", model=self.model, serial=self.serial,
                          width=self.width, height=self.height)
