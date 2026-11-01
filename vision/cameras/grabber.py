"""
采集线程 FrameGrabber —— 与具体相机无关的"取图引擎"。

职责:
    * 独立线程持续 read(), 只保留最新一帧 (不堆积延迟)
    * 每帧带单调递增序号 seq 与时间戳 -> 可靠判定"新帧" (不靠像素比较)
    * 连续读失败 / 超过 stale_s 没有新帧 -> 关闭并重开相机 (指数退避, 上限 5s)
    * 统计: 实测帧率、重连次数、最近错误, 供心跳与自检使用

任何相机实现 (USB / 工业相机 / 回放 / 仿真) 都通过它接入, 相机类本身不需要线程。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional, Tuple

import numpy as np

from ..interfaces import VisionCamera

logger = logging.getLogger(__name__)


class FrameGrabber:
    def __init__(self, camera_factory: Callable[[], VisionCamera],
                 stale_s: float = 2.0, max_fail: int = 15):
        self._factory = camera_factory
        self.camera: Optional[VisionCamera] = None
        self.stale_s = float(stale_s)
        self.max_fail = int(max_fail)
        self._cv = threading.Condition()
        self._frame: Optional[np.ndarray] = None
        self._seq = 0
        self._ts = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.reconnects = 0
        self.last_error = ""
        self._fps = 0.0
        self._on_frame: List[Callable[[np.ndarray, int], None]] = []
        self.on_open: Optional[Callable[[VisionCamera], None]] = None   # 打开后回调 (下发曝光等)

    # ------------------------------------------------------------------ 生命周期
    def start(self, wait_first_s: float = 5.0) -> bool:
        """启动采集; 在 wait_first_s 内拿到首帧返回 True (失败也会在后台持续重试)。"""
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="VisionGrabber", daemon=True)
        self._thread.start()
        return self.wait_frame(0, wait_first_s) is not None

    def stop(self):
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        t = self._thread
        if t:
            t.join(timeout=3.0)
        if t is not None and t.is_alive():
            # 采集线程还卡在 read() 里 (SDK 阻塞): 不能在这里释放句柄, 否则线程醒来用已关闭的
            # 句柄 (海康 SDK 会崩)。由采集线程退出循环时自己关闭。
            logger.warning("相机采集线程 3s 内未退出, 句柄交给线程退出时关闭")
            self._thread = None
            return
        self._thread = None
        self._close_cam()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def fps(self) -> float:
        return self._fps if (time.time() - self._ts) < self.stale_s else 0.0

    @property
    def connected(self) -> bool:
        return self._ts > 0 and (time.time() - self._ts) < self.stale_s

    # ------------------------------------------------------------------ 取帧
    def latest(self) -> Tuple[Optional[np.ndarray], int, float]:
        """(最新帧引用[只读, 不要原地修改], seq, 时间戳)"""
        with self._cv:
            return self._frame, self._seq, self._ts

    def wait_frame(self, after_seq: int, timeout_s: float) -> Optional[Tuple[np.ndarray, int]]:
        """等待 seq > after_seq 的帧。返回 (帧副本, seq) 或 None(超时)。"""
        deadline = time.time() + timeout_s
        with self._cv:
            while self._seq <= after_seq and not self._stop.is_set():
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._cv.wait(left)
            if self._frame is None or self._seq <= after_seq:
                return None
            return self._frame.copy(), self._seq

    def fresh_frames(self, n: int, timeout_s: float, skip: int = 0) -> List[np.ndarray]:
        """取调用之后采集的 n 帧 (先丢弃 skip 帧, 例如曝光/机械臂刚移出视野时)。"""
        _, seq, _ = self.latest()
        out: List[np.ndarray] = []
        deadline = time.time() + timeout_s
        skipped = 0
        while len(out) < n:
            r = self.wait_frame(seq, max(0.0, deadline - time.time()))
            if r is None:
                break
            frame, seq = r
            if skipped < skip:
                skipped += 1
                continue
            out.append(frame)
        return out

    def add_listener(self, fn: Callable[[np.ndarray, int], None]):
        """每来一帧在采集线程回调 fn(frame, seq)。回调须很快, 异常被吞。"""
        self._on_frame.append(fn)

    # ------------------------------------------------------------------ 内部
    def _close_cam(self):
        cam, self.camera = self.camera, None
        if cam is not None:
            try:
                cam.close()
            except Exception:
                pass

    def _open_cam(self) -> bool:
        self._close_cam()
        try:
            cam = self._factory()
            if cam.open():
                self.camera = cam
                if self.on_open:
                    try:
                        self.on_open(cam)
                    except Exception:
                        logger.exception("相机 on_open 回调异常")
                logger.info("相机就绪: %s", cam.describe())
                self.last_error = ""
                return True
            self.last_error = getattr(cam, "last_error", "") or "open() 返回 False"
            try:
                cam.close()
            except Exception:
                pass
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
        logger.warning("相机打开失败: %s", self.last_error)
        return False

    def _loop(self):
        backoff = 0.5
        fails = 0
        t_opened = time.time()
        t_win, n_win = time.time(), 0
        while not self._stop.is_set():
            if self.camera is None:
                if not self._open_cam():
                    self._stop.wait(backoff)
                    backoff = min(5.0, backoff * 2)
                    continue
                backoff = 0.5
                fails = 0
                t_opened = time.time()
            try:
                ok, frame = self.camera.read()
            except Exception as e:
                ok, frame = False, None
                self.last_error = f"read 异常: {e}"
            now = time.time()
            if ok and frame is not None and frame.size > 0:
                fails = 0
                with self._cv:
                    self._frame = frame
                    self._seq += 1
                    self._ts = now
                    seq = self._seq
                    self._cv.notify_all()
                n_win += 1
                if now - t_win >= 1.0:
                    self._fps = n_win / (now - t_win)
                    t_win, n_win = now, 0
                for fn in list(self._on_frame):
                    try:
                        fn(frame, seq)
                    except Exception:
                        logger.exception("帧回调异常")
                continue
            fails += 1
            stale = (now - max(self._ts, t_opened)) > self.stale_s
            if fails >= self.max_fail or stale:
                self.reconnects += 1
                logger.warning("相机无图 (连续失败 %d 次 / %.1fs 无新帧), 第 %d 次重连",
                               fails, now - max(self._ts, t_opened), self.reconnects)
                self._close_cam()
                fails = 0
            else:
                time.sleep(0.01)
        if self._thread is None or self._thread is not threading.current_thread():
            self._close_cam()     # stop() 等待超时时由本线程收尾
