"""
STM32 智能分拣系统 - 2D 平台运动与坐标可视化组件
基于 Tkinter Canvas，提供实时机械臂位置、轨迹、目标点及工位标识绘制。
"""

from __future__ import annotations
import tkinter as tk
from tkinter import ttk
from typing import Optional, Tuple, List, Callable

try:
    from debug.common.responsive import clamp
except Exception:                                    # 允许单文件直接运行
    def clamp(value, low, high):
        return low if value < low else (high if value > high else value)


class PlatformCanvasView(ttk.Frame):
    """
    2D 运动平台俯视图画布
    坐标系:
    - 原点 (0, 0) 为机械臂基准待机点
    - X 轴向右递增 (通常 0 ~ 120 mm)
    - Y 轴向上递增 (通常 -50 ~ +50 mm)
    """

    def __init__(
        self,
        parent,
        width: int = 400,
        height: int = 300,
        on_coordinate_picked: Optional[Callable[[float, float], None]] = None,
        compact: bool = False,
        **kwargs
    ):
        super().__init__(parent, **kwargs)
        self.on_coordinate_picked = on_coordinate_picked

        # 紧凑模式 (800x480 小屏): 缩小边距与刻度字号, 避免刻度文字彼此重叠/被裁
        self.compact = bool(compact)
        self._font_small = ("Segoe UI", 6 if compact else 8)
        self._font_bold = ("Segoe UI", 7 if compact else 8, "bold")
        self._font_cur = ("Segoe UI", 7 if compact else 9, "bold")
        self._grid_step_min_px = 34 if compact else 46

        # 视口范围 (单位: mm)
        self.min_x: float = -10.0
        self.max_x: float = 110.0
        self.min_y: float = -40.0
        self.max_y: float = 40.0

        # 当前位置与目标位置 (mm)
        self.cur_x: float = 0.0
        self.cur_y: float = 0.0
        self.target_x: Optional[float] = None
        self.target_y: Optional[float] = None

        # 轨迹点列表 [(x, y), ...]
        self._trail: List[Tuple[float, float]] = []
        self._max_trail_len: int = 150

        # UI Canvas
        self.canvas = tk.Canvas(
            self,
            width=1 if compact else width,
            height=1 if compact else height,
            bg="#1e1e24",  # 深色工程背景
            highlightthickness=1,
            highlightbackground="#3a3a48"
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<Button-1>", self._on_canvas_click)

        # 初始绘制
        self.redraw()

    def _pick_step(self, span_px: float, span_mm: float) -> float:
        """
        依据可用像素挑选刻度步长 (mm), 保证相邻刻度标签不重叠。

        屏幕越小, 自动跳到更大的步长 (20 -> 40 -> 60 ...), 这是 800x480 下
        坐标图仍然清晰可读、不糊成一团的关键。
        """
        span_mm = max(1.0, float(span_mm))
        for step in (20.0, 40.0, 60.0, 100.0):
            if span_mm / step * self._grid_step_min_px <= max(40.0, span_px):
                return step
        return 100.0

    def update_position(self, x_mm: float, y_mm: float):
        """更新当前机械臂坐标并记录轨迹"""
        if abs(x_mm - self.cur_x) > 0.05 or abs(y_mm - self.cur_y) > 0.05:
            self._trail.append((x_mm, y_mm))
            if len(self._trail) > self._max_trail_len:
                self._trail.pop(0)

        self.cur_x = x_mm
        self.cur_y = y_mm
        self.redraw()

    def set_target(self, x_mm: Optional[float], y_mm: Optional[float]):
        """设置目标坐标标识点"""
        self.target_x = x_mm
        self.target_y = y_mm
        self.redraw()

    def clear_trail(self):
        """清除历史运动轨迹"""
        self._trail.clear()
        self.redraw()

    def _margin(self) -> int:
        """画布边距: 随画布尺寸自适应, 小屏收紧 (给刻度文字留位)"""
        w = max(100, self.canvas.winfo_width())
        h = max(100, self.canvas.winfo_height())
        # 取短边的 ~11%, 并限制在紧凑/常规两种合理区间内
        raw = int(min(w, h) * 0.11)
        if self.compact:
            return int(clamp(raw, 16, 26))
        return int(clamp(raw, 28, 40))

    def _to_canvas_coords(self, x_mm: float, y_mm: float) -> Tuple[float, float]:
        """将物理 mm 坐标转换为 Canvas 像素坐标 (Y轴向上)"""
        w = max(100, self.canvas.winfo_width())
        h = max(100, self.canvas.winfo_height())
        margin = self._margin()

        usable_w = w - 2 * margin
        usable_h = h - 2 * margin

        span_x = self.max_x - self.min_x
        span_y = self.max_y - self.min_y

        px = margin + ((x_mm - self.min_x) / span_x) * usable_w
        py = (h - margin) - ((y_mm - self.min_y) / span_y) * usable_h
        return px, py

    def _to_mm_coords(self, px: float, py: float) -> Tuple[float, float]:
        """将 Canvas 像素坐标转换为物理 mm 坐标"""
        w = max(100, self.canvas.winfo_width())
        h = max(100, self.canvas.winfo_height())
        margin = self._margin()

        usable_w = w - 2 * margin
        usable_h = h - 2 * margin

        span_x = self.max_x - self.min_x
        span_y = self.max_y - self.min_y

        x_mm = self.min_x + ((px - margin) / usable_w) * span_x
        y_mm = self.min_y + (((h - margin) - py) / usable_h) * span_y
        return round(x_mm, 2), round(y_mm, 2)

    def _on_canvas_click(self, event):
        """点击画布选点"""
        x_mm, y_mm = self._to_mm_coords(event.x, event.y)
        # 边界检查
        if self.min_x <= x_mm <= self.max_x and self.min_y <= y_mm <= self.max_y:
            self.set_target(x_mm, y_mm)
            if self.on_coordinate_picked:
                self.on_coordinate_picked(x_mm, y_mm)

    def redraw(self):
        """重新绘制所有元素"""
        self.canvas.delete("all")
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w < 10 or h < 10:
            return

        margin = self._margin()
        fs_small = self._font_small
        fs_bold = self._font_bold

        # 1. 绘制网格与刻度
        # X 轴辅助线: 步长随可视宽度自适应, 保证刻度标签不互相重叠
        grid_step_x = self._pick_step(w - 2 * margin, self.max_x - self.min_x)
        cur_x = int(self.min_x // grid_step_x) * grid_step_x
        while cur_x <= self.max_x:
            px, py_bottom = self._to_canvas_coords(cur_x, self.min_y)
            _, py_top = self._to_canvas_coords(cur_x, self.max_y)
            color = "#444455" if cur_x == 0 else "#282833"
            self.canvas.create_line(px, py_bottom, px, py_top, fill=color, dash=(2, 4))
            self.canvas.create_text(px, h - margin + (7 if self.compact else 12),
                                    text=f"{int(cur_x)}", fill="#888899", font=fs_small)
            cur_x += grid_step_x

        # Y 轴辅助线: 步长随可视高度自适应
        grid_step_y = self._pick_step(h - 2 * margin, self.max_y - self.min_y)
        cur_y = int(self.min_y // grid_step_y) * grid_step_y
        while cur_y <= self.max_y:
            px_left, py = self._to_canvas_coords(self.min_x, cur_y)
            px_right, _ = self._to_canvas_coords(self.max_x, cur_y)
            color = "#444455" if cur_y == 0 else "#282833"
            self.canvas.create_line(px_left, py, px_right, py, fill=color, dash=(2, 4))
            self.canvas.create_text(margin - (10 if self.compact else 16), py,
                                    text=f"{int(cur_y)}", fill="#888899", font=fs_small)
            cur_y += grid_step_y

        # 轴标签
        self.canvas.create_text(w - (11 if self.compact else 15), h - margin + (7 if self.compact else 12),
                                text="X(mm)", fill="#aaaacc", font=fs_bold)
        self.canvas.create_text(margin - (8 if self.compact else 12), (9 if self.compact else 15),
                                text="Y(mm)", fill="#aaaacc", font=fs_bold)

        # 2. 标记典型预设工位区域
        # 原点待机区 (0, 0)
        ox, oy = self._to_canvas_coords(0, 0)
        self.canvas.create_oval(ox - 5, oy - 5, ox + 5, oy + 5, outline="#55aa55", fill="#224422")
        self.canvas.create_text(ox, oy - (9 if self.compact else 12), text="原点(0,0)",
                                fill="#66cc66", font=fs_small)

        # 预设取件区 (X=30~50, Y=10~30)
        p1x, p1y = self._to_canvas_coords(30, 30)
        p2x, p2y = self._to_canvas_coords(50, 10)
        self.canvas.create_rectangle(p1x, p1y, p2x, p2y, outline="#336699", fill="#182c42", dash=(3, 3))
        self.canvas.create_text((p1x + p2x) / 2, (p1y + p2y) / 2, text="取料区",
                                fill="#4488cc", font=fs_small)

        # 放置释放区 (X=80, Y=0)
        dx, dy = self._to_canvas_coords(80, 0)
        self.canvas.create_rectangle(dx - 12, dy - 12, dx + 12, dy + 12, outline="#996633", fill="#442a18")
        self.canvas.create_text(dx, dy + (12 if self.compact else 16), text="放置区(80,0)",
                                fill="#dd9944", font=fs_small)

        # 3. 绘制历史运动轨迹虚线
        if len(self._trail) > 1:
            pts = []
            for tx, ty in self._trail:
                px, py = self._to_canvas_coords(tx, ty)
                pts.extend([px, py])
            self.canvas.create_line(*pts, fill="#0088cc", width=1.5, dash=(2, 2))

        # 4. 绘制目标坐标标记 (红色十字瞄准镜)
        if self.target_x is not None and self.target_y is not None:
            tx, ty = self._to_canvas_coords(self.target_x, self.target_y)
            # 十字
            self.canvas.create_line(tx - 10, ty, tx + 10, ty, fill="#ff4444", width=2)
            self.canvas.create_line(tx, ty - 10, tx, ty + 10, fill="#ff4444", width=2)
            self.canvas.create_oval(tx - 6, ty - 6, tx + 6, ty + 6, outline="#ff4444", width=1.5)
            self.canvas.create_text(
                tx + 14, ty - 8,
                text=f"目标({self.target_x:.1f},{self.target_y:.1f})",
                fill="#ff7777",
                font=fs_bold,
                anchor="w"
            )

        # 5. 绘制当前机械臂位置 (亮绿色高对比度实体光标与光晕)
        cx, cy = self._to_canvas_coords(self.cur_x, self.cur_y)
        # 外发光
        self.canvas.create_oval(cx - 10, cy - 10, cx + 10, cy + 10, outline="#00ff88", fill="#003322", width=1.5)
        # 中心点
        self.canvas.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill="#00ff88", outline="#ffffff")
        # 实时坐标读数
        self.canvas.create_text(
            cx + 12, cy + 10,
            text=f"当前({self.cur_x:.1f},{self.cur_y:.1f})",
            fill="#00ffaa",
            font=self._font_cur,
            anchor="w"
        )
