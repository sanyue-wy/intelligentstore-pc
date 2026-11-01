"""
STM32 智能分拣系统 - 自适应屏幕布局工具 (Responsive Layout Helpers)

背景 / 要解决的问题
--------------------------------------------------------------------------
物理显示屏只有 800x480 (xrandr: HDMI-1 800x480), 而调试界面最初是按
1280x820 设计并写死 geometry + minsize 的。更关键的是: X11 上**没有运行
窗口管理器**(xprop: _NET_CLIENT_LIST 不存在, 无 openbox 等), 没有 WM 时
Tk 窗口不会被自动摆放到屏幕内, 超出 800x480 的部分直接被裁掉, 鼠标也移不过去。

因此本模块把「窗口尺寸」和「分栏结构」都改成运行时推导:
    * 尺寸: 由 winfo_screenwidth() / winfo_screenheight() 动态计算;
    * 结构: 屏幕足够大时用宽屏多栏, 屏幕小(<= COMPACT_SCREEN_*)时进入紧凑
      模式, 收紧字号/内边距并改用紧凑分栏, 保证 800x480 下核心功能区全部可见;
    * 拉大窗口: 所有布局一律 fill/expand, 紧凑模式下也能正常铺满, 不会退化成
      「只适配 800x480 的固定布局」;
    * 兜底: 万一内容仍超出可视区, ScrolledFrame 提供滚动, 不会有控件被永久裁掉。

只提供布局工具, 不修改字体族配置 (仍用 "Segoe UI" / "Consolas"), 仅按屏幕
大小调整字号。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# ---------------------------------------------------------------------------
# 紧凑模式判定阈值 (屏幕像素, 不是窗口像素)
#
# 阈值来自各界面实测的最小需求宽度/高度:
#   * 通讯调试器三栏并排需要 ~1395x682, 减去边距后窗口至少 ~1411x754;
#   * 综合工作台 5 个完整 Tab 标签 + 右栏控制卡需要 ~1112px 宽。
# 因此 1280x800 仍属于「大屏」, 而 1024x768 / 800x480 必须走紧凑布局
# (1024x768 实测会把「放置区 (80,0)」预设按钮裁掉, 不是理论推算)。
# ---------------------------------------------------------------------------
COMPACT_SCREEN_WIDTH = 1200
COMPACT_SCREEN_HEIGHT = 760

#: 界面统一字体族 (与 debug/common/theme.py 保持一致, 不在此处改动字体配置)
UI_FONT = "Segoe UI"
MONO_FONT = "Consolas"


# ---------------------------------------------------------------------------
# 屏幕尺寸 / 窗口几何
# ---------------------------------------------------------------------------
def screen_size(widget) -> "tuple[int, int]":
    """读取真实屏幕像素尺寸 (失败时退化为 1280x800)"""
    try:
        w = int(widget.winfo_screenwidth())
        h = int(widget.winfo_screenheight())
    except Exception:
        w, h = 1280, 800
    return max(320, w), max(240, h)


def is_compact(widget) -> bool:
    """当前屏幕是否需要紧凑模式 (800x480 这类小屏返回 True)"""
    w, h = screen_size(widget)
    return w < COMPACT_SCREEN_WIDTH or h < COMPACT_SCREEN_HEIGHT


def apply_window(root: tk.Misc, ideal=(1280, 820), min_size=(720, 420),
                 offset=(0, 0)):
    """
    按真实屏幕尺寸设置窗口几何。

    窗口取 ideal 与屏幕的交集 (不超过屏幕), 并放在左上角 —— 没有窗口管理器
    时这是唯一能保证整窗可见的摆法。minsize 同样不能大于屏幕, 否则在小屏上
    会被强行撑出可视区。

    Returns:
        (width, height) 实际请求的窗口尺寸
    """
    sw, sh = screen_size(root)
    w = max(320, min(int(ideal[0]), sw))
    h = max(240, min(int(ideal[1]), sh))
    try:
        root.geometry(f"{w}x{h}+{int(offset[0])}+{int(offset[1])}")
    except Exception:
        pass
    try:
        root.minsize(max(320, min(int(min_size[0]), sw)),
                     max(240, min(int(min_size[1]), sh)))
    except Exception:
        pass
    return w, h


def window_size(root: tk.Misc) -> "tuple[int, int]":
    """当前窗口实际像素尺寸 (未映射时回退到屏幕尺寸)"""
    try:
        w = int(root.winfo_width())
        h = int(root.winfo_height())
    except Exception:
        w = h = 1
    if w <= 1 or h <= 1:
        return screen_size(root)
    return w, h


# ---------------------------------------------------------------------------
# 度量: 字号 / 内边距
# ---------------------------------------------------------------------------
def ui_font(size: int, bold: bool = False, mono: bool = False) -> "tuple":
    """统一字体族, 只调整字号 (不改动字体族配置)"""
    return (MONO_FONT if mono else UI_FONT, int(size), "bold") if bold \
        else (MONO_FONT if mono else UI_FONT, int(size))


def ui_metrics(compact: bool) -> dict:
    """
    界面度量。紧凑模式用于 800x480, 常规模式用于 1280x820+ / 远程镜像。

    所有值都只是「初值」, 控件依旧 fill/expand, 窗口拉大时正常铺满。
    """
    if compact:
        return {
            "compact": True,
            "fs": 8,            # 常规正文
            "fs_small": 7,      # 次要说明
            "fs_bold": 8,       # 强调
            "fs_title": 9,      # 区块标题
            "fs_big": 14,       # 大字读数
            "pad": 3,           # 通用内边距
            "pad_x": 5,
            "pad_y": 2,
            "gap": 2,           # 控件间距
            "btn_padx": 5,
            "btn_pady": 1,
            "bar_padx": 6,
            "bar_pady": 3,
            "tab_pad": [9, 3],
            "tree_row_h": 18,
            "log_h": 2,         # 日志/状态文本行数
            "progress_h": 12,
        }
    return {
        "compact": False,
        "fs": 9,
        "fs_small": 8,
        "fs_bold": 9,
        "fs_title": 9,
        "fs_big": 18,
        "pad": 8,
        "pad_x": 10,
        "pad_y": 6,
        "gap": 4,
        "btn_padx": 10,
        "btn_pady": 3,
        "bar_padx": 12,
        "bar_pady": 6,
        "tab_pad": [16, 6],
        "tree_row_h": 22,
        "log_h": 3,
        "progress_h": 16,
    }


def font_of(m: dict, key: str, bold: bool = False, mono: bool = False) -> "tuple":
    """从度量字典取字号并生成字体元组"""
    return ui_font(m.get(key, m["fs"]), bold=bold, mono=mono)


# ---------------------------------------------------------------------------
# 可滚动容器 (兜底, 保证内容永不被永久裁掉)
# ---------------------------------------------------------------------------
class ScrolledFrame(ttk.Frame):
    """
    垂直可滚动容器。

    用法:
        sf = ScrolledFrame(parent, bg="#1e1e24")
        sf.pack(fill=tk.BOTH, expand=True)
        tk.Label(sf.body, text="...")      # 所有子控件加到 sf.body

    内容不超过可视高度时自动隐藏滚动条, 超过时才显示, 因此在大屏上不会有
    多余的滚动条影响观感。
    """

    def __init__(self, parent, bg: str = "#1e1e24", **kwargs):
        super().__init__(parent, **kwargs)
        self._bg = bg

        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0,
                                takefocus=0)
        self.vsb = ttk.Scrollbar(self, orient=tk.VERTICAL,
                                 command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll_set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.body = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.body,
                                             anchor="nw")
        self._sb_shown = False

        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    # -- 滚动条显隐 --------------------------------------------------------
    def _on_scroll_set(self, first, last):
        if float(first) <= 0.0 and float(last) >= 1.0:
            if self._sb_shown:
                self.vsb.pack_forget()
                self._sb_shown = False
        else:
            if not self._sb_shown:
                self.vsb.pack(side=tk.RIGHT, fill=tk.Y)
                self._sb_shown = True
        self.vsb.set(first, last)

    def _on_body_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        # 让内部 frame 始终与可视宽度一致 -> 内容只纵向滚动, 不会被横向裁掉
        self.canvas.itemconfigure(self._win, width=max(1, event.width))

    # -- 鼠标滚轮 ----------------------------------------------------------
    def _bind_wheel(self, _event=None):
        self.canvas.bind_all("<Button-4>", self._on_wheel_up)
        self.canvas.bind_all("<Button-5>", self._on_wheel_down)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel_win)

    def _unbind_wheel(self, _event=None):
        for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            try:
                self.canvas.unbind_all(seq)
            except Exception:
                pass

    def _scrollable(self) -> bool:
        bbox = self.canvas.bbox("all")
        if not bbox:
            return False
        return bbox[3] > self.canvas.winfo_height()

    def _on_wheel_up(self, _event=None):
        if self._scrollable():
            self.canvas.yview_scroll(-2, "units")

    def _on_wheel_down(self, _event=None):
        if self._scrollable():
            self.canvas.yview_scroll(2, "units")

    def _on_wheel_win(self, event):
        if self._scrollable():
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")


# ---------------------------------------------------------------------------
# 尺寸辅助
# ---------------------------------------------------------------------------
def fit_canvas_font(m: dict, default_key: str = "fs") -> "tuple":
    """坐标画布用字体 (小屏更小, 避免刻度文字互相重叠)"""
    return font_of(m, default_key)


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


def scale_to_fit(src_w: int, src_h: int, box_w: int, box_h: int,
                 max_scale: float = 4.0) -> "tuple[int, int]":
    """
    把 src 等比缩放到 box 内 (不放大超过 max_scale, 不返回 0)。

    界面里的视频/图像一律走这里, 尺寸永远取自控件实际大小, 因此窗口拉大或
    缩小都能正确铺满, 不会因为写死尺寸而被裁切。
    """
    src_w = max(1, int(src_w))
    src_h = max(1, int(src_h))
    box_w = max(1, int(box_w))
    box_h = max(1, int(box_h))
    scale = min(box_w / src_w, box_h / src_h, max_scale)
    if scale <= 0:
        scale = 1.0
    return max(1, int(src_w * scale)), max(1, int(src_h * scale))
