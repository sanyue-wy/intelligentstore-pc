"""
STM32 智能分拣系统 - 调试界面共享配色与样式

各调试器界面共用同一套深色主题, 集中在此避免重复定义。
"""

from __future__ import annotations
from tkinter import ttk

#: 界面主色板
COLORS = {
    "bg": "#1e1e24",           # 窗口背景
    "panel": "#181820",        # 卡片 / 读数面板
    "toolbar": "#262630",      # 顶部工具栏
    "video_bg": "#131318",     # 视频显示区背景
    "text_bg": "#0f0f14",      # 日志 / 终端背景
    "grid": "#15151c",         # 表格背景
    "grid_head": "#2a2a38",    # 表头 / 分组背景
    "border": "#3a3a48",       # 边框与次级控件
    "text": "#e0e0e0",         # 主文字
    "text_dim": "#bbbbcc",     # 次要文字
    "text_faint": "#888899",   # 弱化文字
    "accent": "#00aaff",       # 主强调色 (分组标题)
    "ok": "#00ffaa",           # 正常 / 数值读数
    "info": "#4db8ff",         # 提示
    "warn": "#ffaa00",         # 警告
    "error": "#ff5555",        # 故障
}


def apply_dark_theme(widget=None, style: ttk.Style = None, metrics: dict = None):
    """
    应用统一的深色主题。

    Args:
        widget:  传入 tk 根窗口时会自动修改其背景色
        style:   可选的 ttk.Style; 不传则自行创建
        metrics: 可选的界面度量字典 (debug.common.responsive.ui_metrics)。
                 传入时按其中的字号/内边距配置样式, 用于 800x480 紧凑模式;
                 不传则维持原来的 1280x820 视觉规格。
                 注意: 只调整字号与内边距, 字体族仍为 Segoe UI。
    """
    if widget is not None:
        try:
            widget.configure(bg=COLORS["bg"])
        except Exception:
            pass

    if style is None:
        style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass

    c = COLORS

    # 度量: 未显式传入时保持原有 (1280x820) 规格, 行为完全向后兼容
    from debug.common.responsive import ui_metrics
    m = metrics if metrics is not None else ui_metrics(False)
    fs = m["fs"]
    fs_bold = m["fs_bold"]

    style.configure(".", background=c["bg"], foreground=c["text"], font=("Segoe UI", fs))
    style.configure("TLabel", background=c["bg"], foreground=c["text"], font=("Segoe UI", fs))
    style.configure("TFrame", background=c["bg"])
    style.configure("TButton", font=("Segoe UI", fs), padding=max(2, m["pad"] // 2))
    style.configure("TEntry", fieldbackground="#2b2b36", foreground="#ffffff")
    style.configure("TCheckbutton", background=c["bg"], foreground=c["text"], font=("Segoe UI", fs))
    style.configure("TCombobox", fieldbackground="#2b2b36", background=c["border"],
                    foreground="#ffffff", arrowcolor="#e0e0e0")
    # readonly 下拉框在 clam 主题下默认会退回浅底深字, 在深色工具栏里几乎看不清
    # (800x480 物理屏上尤其明显), 这里显式指定 readonly 状态的配色。
    style.map("TCombobox",
              fieldbackground=[("readonly", "#2b2b36"), ("disabled", "#242430")],
              foreground=[("readonly", "#ffffff"), ("disabled", "#777788")],
              selectbackground=[("readonly", "#2b2b36")],
              selectforeground=[("readonly", "#ffffff")],
              background=[("readonly", "#3a3a48"), ("active", "#4a4a5c")])
    # 下拉列表 (Listbox 部分) 配色
    try:
        style.configure("TCombobox.Listbox", background="#2b2b36", foreground="#ffffff",
                        selectbackground="#007acc", selectforeground="#ffffff")
    except Exception:
        pass
    style.configure("TNotebook", background=c["bg"], tabmargins=[2, 5, 2, 0])
    style.configure("TNotebook.Tab", background=c["grid_head"], foreground="#cccccc",
                    padding=list(m["tab_pad"]), font=("Segoe UI", fs_bold, "bold"))
    style.map("TNotebook.Tab",
              background=[("selected", "#007acc"), ("active", c["border"])],
              foreground=[("selected", "#ffffff"), ("active", "#ffffff")])
    # 下拉弹出列表 (tk Listbox) 的配色, 只能通过 option database 设置
    try:
        if widget is not None:
            widget.option_add("*TCombobox*Listbox.background", "#2b2b36")
            widget.option_add("*TCombobox*Listbox.foreground", "#ffffff")
            widget.option_add("*TCombobox*Listbox.selectBackground", "#007acc")
            widget.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    except Exception:
        pass

    style.configure("TLabelframe", background=c["bg"], foreground=c["accent"])
    style.configure("TLabelframe.Label", background=c["bg"], foreground=c["accent"],
                    font=("Segoe UI", fs_bold, "bold"))
    style.configure("Treeview", background=c["grid"], foreground="#d8d8d8",
                    fieldbackground=c["grid"], rowheight=m["tree_row_h"], font=("Segoe UI", fs))
    style.configure("Treeview.Heading", background=c["grid_head"], foreground="#00c8ff",
                    font=("Segoe UI", fs_bold, "bold"))
    style.configure("Treeview.Item", font=("Segoe UI", fs))
    style.configure("TProgressbar", thickness=m["progress_h"])

    return style