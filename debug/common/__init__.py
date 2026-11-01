"""
调试界面公共组件 (跨模块共用, 与任何单一业务模块无关)
"""

from .canvas_view import PlatformCanvasView
from .theme import COLORS, apply_dark_theme

__all__ = ["PlatformCanvasView", "COLORS", "apply_dark_theme"]
