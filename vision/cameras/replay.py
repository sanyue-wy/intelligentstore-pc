"""
图片/视频回放相机 (注册名 "replay")。

用途: 把比赛现场录下的图片目录或视频当作相机, 在 PC 或板子上离线复现问题、
调参、做回归测试 —— 不需要机械臂和真相机。
    path 为目录: 按文件名排序循环播放 *.jpg/*.png/*.bmp
    path 为视频: 逐帧播放, 结束后从头开始 (loop=True)
"""
from __future__ import annotations

import glob
import os
import time
from typing import List, Optional, Tuple

import cv2
import numpy as np

from ..interfaces import CameraInfo, VisionCamera

_IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp")


class ReplayCamera(VisionCamera):
    def __init__(self, path: str, fps: float = 10.0, loop: bool = True,
                 width: int = 0, height: int = 0):
        self.path = path
        self.fps = max(0.5, float(fps or 10.0))
        self.loop = loop
        self._files: List[str] = []
        self._idx = 0
        self._cap = None
        self._opened = False
        self._next_t = 0.0
        self._size = (int(width or 0), int(height or 0))
        self.last_error = ""
        self.current_name = ""

    def open(self) -> bool:
        self.close()
        p = self.path
        if p and os.path.isdir(p):
            self._files = sorted(f for f in glob.glob(os.path.join(p, "*"))
                                 if f.lower().endswith(_IMG_EXT))
            if not self._files:
                self.last_error = f"目录里没有图片: {p}"
                return False
        elif p and os.path.isfile(p):
            if p.lower().endswith(_IMG_EXT):
                self._files = [p]
            else:
                self._cap = cv2.VideoCapture(p)
                if not self._cap.isOpened():
                    self.last_error = f"无法打开视频: {p}"
                    return False
        else:
            self.last_error = f"回放路径不存在: {p!r}"
            return False
        self._idx = 0
        self._opened = True
        return True

    def close(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        self._opened = False

    def is_opened(self) -> bool:
        return self._opened

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        if not self._opened:
            return False, None
        now = time.time()
        if self._next_t > now:
            time.sleep(self._next_t - now)
        self._next_t = max(now, self._next_t) + 1.0 / self.fps
        frame = None
        if self._cap is not None:
            ok, frame = self._cap.read()
            if not ok and self.loop:
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self._cap.read()
            self.current_name = f"{os.path.basename(self.path)}#{int(self._cap.get(cv2.CAP_PROP_POS_FRAMES))}"
        elif self._files:
            if self._idx >= len(self._files):
                if not self.loop:
                    return False, None
                self._idx = 0
            f = self._files[self._idx]
            self._idx += 1
            frame = cv2.imread(f)
            self.current_name = os.path.basename(f)
        if frame is None:
            return False, None
        if self._size[0] > 0 and self._size[1] > 0 and frame.shape[1::-1] != self._size:
            frame = cv2.resize(frame, self._size)
        return True, frame

    @property
    def frame_size(self) -> Tuple[int, int]:
        return self._size if self._size[0] else (640, 480)

    @property
    def info(self) -> CameraInfo:
        return CameraInfo(kind="replay", model=os.path.basename(self.path or ""),
                          width=self.frame_size[0], height=self.frame_size[1], fps=self.fps,
                          extra={"frames": len(self._files)})
