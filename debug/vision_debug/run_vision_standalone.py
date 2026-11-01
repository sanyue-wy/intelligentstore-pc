"""
视觉调试入口 (python main.py --vision)

所有需要实时画面的调参 (HSV 颜色、标定、托盘、镜头畸变、相机曝光/白平衡、ROI、点哪去哪)
都在浏览器调试台里完成, 板子无需显示器:
    python3 tools/vision_tool.py web [--arm]   ->  笔记本浏览器打开 http://<板子IP>:8090
本入口只是转调它 (旧的 Tk 视觉调试窗口已删除)。
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_vision_standalone(backend: str = None):
    cmd = [sys.executable, os.path.join(ROOT, "tools", "vision_tool.py"), "web"]
    if backend:
        cmd += ["--backend", backend]
    cmd += [a for a in sys.argv[1:] if a in ("--arm", "--force")]
    print("[视觉] 浏览器调试台: http://<板子IP>:8090  (Ctrl+C 退出)")
    raise SystemExit(subprocess.call(cmd, cwd=ROOT))


if __name__ == "__main__":
    run_vision_standalone()
