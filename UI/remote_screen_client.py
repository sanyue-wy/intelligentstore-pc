# UI/remote_screen_client.py
# -*- coding: utf-8 -*-
"""
分拣成功页 → RDK X5 大屏服务的推送客户端

设计原则:
  1. 零第三方依赖（只用标准库 urllib），容器里直接可用；
  2. **绝不阻塞主流程**：后台线程推送，服务不可达时静默降级，
     不会因为大屏服务没开而让比赛流程卡住或抛异常；
  3. 失败自动重连，不打印刷屏日志。

用法:
    from UI.remote_screen_client import ScreenClient
    screen = ScreenClient()                 # 默认 127.0.0.1:8756
    screen.show(seq=1, name="五棱柱", total=1,
                image_path="/tmp/goods/1.png")   # 图片可选
    screen.close()
"""

from __future__ import annotations

import base64
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = os.environ.get(
    "X5_SCREEN_ENDPOINT", "http://127.0.0.1:8756/api/screen")


class ScreenClient:
    """后台把分拣信息推到 X5 大屏服务。线程安全，永不抛到调用方。"""

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, timeout: float = 2.0,
                 quiet: bool = True):
        self.endpoint = endpoint
        self.timeout = timeout
        self.quiet = quiet
        self._q: "queue.Queue[dict]" = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._fail_streak = 0
        self._last_ok = 0.0
        self._busy = False
        self.last_payload: dict = {}
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # -- 对外 API -----------------------------------------------------------
    def show(self, seq, name: str, total: int, image_path: str = "",
             stage: str = "分拣中", color: str = "") -> None:
        """推送一条分拣成功信息（非阻塞，只保留最新一条）。"""
        payload = {
            "cmd": "show",
            "seq": seq,
            "name": str(name),
            "total": int(total),
            "stage": str(stage),
            "color": str(color),
            "image": self._load_image(image_path),
        }
        try:
            self._q.put_nowait(payload)
        except queue.Full:
            # 丢掉最旧的一条，保证显示的是最新状态
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(payload)
            except queue.Full:
                pass

    def show_image_bytes(self, seq, name: str, total: int, data: bytes,
                         stage: str = "分拣中") -> None:
        payload = {
            "cmd": "show", "seq": seq, "name": str(name), "total": int(total),
            "stage": str(stage),
            "image": base64.b64encode(data).decode() if data else "",
        }
        try:
            self._q.put_nowait(payload)
        except queue.Full:
            pass

    def flush(self, timeout: float = 1.0) -> bool:
        """等到已排队的内容推送完成 (最多 timeout 秒)。返回最后一次推送是否成功。
        赛规: 显示完本件信息后才能分拣下一件, 比赛流程在 update_status 后调用。"""
        deadline = time.time() + timeout
        while (not self._q.empty() or self._busy) and time.time() < deadline:
            time.sleep(0.02)
        return self._fail_streak == 0 and self._last_ok > 0

    def available(self) -> bool:
        return self._last_ok > 0 and (time.time() - self._last_ok) < 10

    def close(self, timeout: float = 1.5) -> None:
        """结束前把队列里最后一条发完（最多等 timeout 秒）。"""
        deadline = time.time() + timeout
        while not self._q.empty() and time.time() < deadline:
            time.sleep(0.05)
        self._stop.set()
        self._worker.join(timeout=timeout)

    # -- 内部 ---------------------------------------------------------------
    @staticmethod
    def _load_image(path: str) -> str:
        if not path:
            return ""
        try:
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode()
        except OSError:
            return ""

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self._q.get(timeout=0.4)
            except queue.Empty:
                continue
            self._busy = True
            try:
                self._post(payload)
            finally:
                self._busy = False

    def _post(self, payload: dict) -> None:
        self.last_payload = {k: v for k, v in payload.items() if k != "image"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=body,
            headers={"Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read(64)
            self._last_ok = time.time()
            self._fail_streak = 0
        except Exception:
            self._fail_streak += 1
            # 只在第一次失败和每 30 次失败时提示，避免刷屏
            if not self.quiet and self._fail_streak in (1, 30):
                print(f"[大屏] 推送失败（第 {self._fail_streak} 次），"
                      f"目标 {self.endpoint}；比赛流程不受影响")


# ---------------------------------------------------------------------------
# 便捷函数：不创建对象的一次性推送（适合临时脚本）
# ---------------------------------------------------------------------------

def push_once(seq, name: str, total: int, image_path: str = "",
              endpoint: str = DEFAULT_ENDPOINT,
              stage: str = "分拣中") -> bool:
    payload = {
        "cmd": "show", "seq": seq, "name": str(name), "total": int(total),
        "stage": stage,
        "image": ScreenClient._load_image(image_path),
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(
            endpoint, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            resp.read(64)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    import sys
    seq = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    name = sys.argv[2] if len(sys.argv) > 2 else "五棱柱"
    ok = push_once(seq, name, seq)
    print("推送成功" if ok else "推送失败（大屏服务未运行？）")
