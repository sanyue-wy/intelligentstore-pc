"""
仿真相机 (注册名 "synthetic"): 无硬件时生成带移动色块的台面画面, 用于联调。
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

import cv2
import numpy as np

from ..interfaces import CameraInfo
from .uvc import BaseCamera


class SyntheticCamera(BaseCamera):
    """
    虚拟仿真摄像头:
    生成带有工件台面、标定刻度、动态生成红/绿/蓝/黄工件的真实视觉画面，
    用于在脱机无相机时调试视觉检测算法、坐标换算与自动化任务闭环。
    """
    def __init__(self, width: int = 640, height: int = 480):
        self.width = width
        self.height = height
        self._opened = False
        self._start_time = time.time()

        # 模拟台面工件列表: [x, y, vx, vy, color_name, bgr_color, size]
        self._blocks = [
            {"x": 320.0, "y": 240.0, "vx": 15.0, "vy": 10.0, "color": "red", "bgr": (30, 30, 220), "size": 36},
            {"x": 220.0, "y": 180.0, "vx": -10.0, "vy": 12.0, "color": "green", "bgr": (30, 200, 30), "size": 34},
            {"x": 420.0, "y": 300.0, "vx": 8.0, "vy": -14.0, "color": "blue", "bgr": (220, 100, 20), "size": 35},
            {"x": 250.0, "y": 320.0, "vx": -12.0, "vy": -8.0, "color": "yellow", "bgr": (30, 220, 220), "size": 32},
        ]

    def open(self) -> bool:
        self._opened = True
        self._start_time = time.time()
        return True

    def close(self):
        self._opened = False

    def is_opened(self) -> bool:
        return self._opened

    @property
    def info(self) -> CameraInfo:
        return CameraInfo(kind="synthetic", model="mock", width=self.width, height=self.height, fps=30.0)

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        if not self._opened:
            return False, None
        # 模拟 30FPS 的采集节拍 (真实相机 read 本身就会阻塞到下一帧)
        now = time.time()
        wait = getattr(self, "_next_t", 0.0) - now
        if wait > 0:
            time.sleep(wait)
        self._next_t = max(now, getattr(self, "_next_t", 0.0)) + 1.0 / 30

        # 1. 绘制工业工位背景
        frame = np.full((self.height, self.width, 3), 40, dtype=np.uint8)

        # 绘制网格与工作区
        cv2.rectangle(frame, (120, 80), (520, 400), (60, 60, 60), -1)
        cv2.rectangle(frame, (120, 80), (520, 400), (100, 100, 100), 2)

        # 网格虚线
        for gx in range(160, 520, 40):
            cv2.line(frame, (gx, 80), (gx, 400), (50, 50, 50), 1)
        for gy in range(120, 400, 40):
            cv2.line(frame, (120, gy), (520, gy), (50, 50, 50), 1)

        # 标定区域参考线
        cv2.rectangle(frame, (160, 120), (480, 360), (0, 140, 200), 1)
        cv2.putText(frame, "Sorting Zone (Calibrated)", (165, 110),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 180, 255), 1)

        # 2. 模拟缓慢移动的工件 (模拟传送带/料盘工件)
        now = time.time()
        dt = 0.03

        for b in self._blocks:
            b["x"] += b["vx"] * dt
            b["y"] += b["vy"] * dt

            # 边界碰撞反弹
            if b["x"] < 150 or b["x"] > 490:
                b["vx"] = -b["vx"]
            if b["y"] < 110 or b["y"] > 370:
                b["vy"] = -b["vy"]

            bx = int(b["x"])
            by = int(b["y"])
            s = b["size"]

            # 绘制实体方块工件
            p1 = (bx - s // 2, by - s // 2)
            p2 = (bx + s // 2, by + s // 2)
            cv2.rectangle(frame, p1, p2, b["bgr"], -1)
            cv2.rectangle(frame, p1, p2, (255, 255, 255), 1)

            # 中心标记十字
            cv2.drawMarker(frame, (bx, by), (0, 0, 0), cv2.MARKER_CROSS, 8, 1)

        # 叠加时间与模拟标签
        t_str = time.strftime("%H:%M:%S")
        cv2.putText(frame, f"[MOCK CAMERA] {t_str}", (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 120), 2)

        return True, frame
