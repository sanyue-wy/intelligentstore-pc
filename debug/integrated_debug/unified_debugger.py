"""
STM32 智能分拣系统 - 全系统联合调试与排障工作台 (Unified System Debugger)
集成本地所有核心模块 (Communication, Vision, MessegeCenter, Moniter):
- Tab 1: 整体综合调试 (视觉引导自动分拣、端到端闭环流水线、全系统联调)
- Tab 2: 通讯模块单体调试 (USB CDC 串口交互、32B 二进制帧、JOG点动与单步动作)
- Tab 3: 视觉模块单体调试 (实时相机画面、HSV阈值调节滑块、二值掩膜、坐标标定)
- Tab 4: 消息中心独立监听 (全主题报文流、实时过滤、测试事件手动注入)
- Tab 5: 程序监控与健康看板 (心跳看门狗、主机CPU/内存监控、活动告警诊断)
"""

from __future__ import annotations
import sys
import os
import time
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import cv2
import numpy as np
from PIL import Image, ImageTk

CUR_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(os.path.dirname(CUR_DIR))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

# 导入所有子模块 (只依赖接口契约, 不直接引用各模块的具体实现类)
from communication import create_comm_client
from vision import create_vision_service
from moniter import create_monitor_service
from MessegeCenter import (
    MessageBus, get_message_bus, MessageRouter, Message, create_message_bus,
    TOPIC_COMM_STATUS, TOPIC_COMM_ESTOP, TOPIC_COMM_CLEAR,
    TOPIC_VISION_TARGET, TOPIC_TASK_START, TOPIC_TASK_PROGRESS,
    TOPIC_TASK_DONE, TOPIC_MONITOR_HEARTBEAT, VisionTarget, ModuleHeartbeat
)
from debug.common.canvas_view import PlatformCanvasView
from debug.common.theme import apply_dark_theme
from debug.common.responsive import (
    apply_window, is_compact, screen_size, window_size,
    ui_metrics, font_of, scale_to_fit, ScrolledFrame,
)


class UnifiedSystemDebugger:
    """全功能综合调试工作台"""

    def __init__(self, root: tk.Tk, vision_backend: str = None,
                 comm_backend: str = None, monitor_backend: str = None,
                 msg_backend: str = None):
        self.root = root
        self.root.title("STM32 智能分拣系统 - 全系统联合调试与排障工作台 (Unified Debugger)")

        # ------------------------------------------------------------------
        # 屏幕自适应: 物理屏只有 800x480, 且 X11 上没有窗口管理器,
        # 写死 1280x820 会被直接裁掉。这里一律按 winfo_screenwidth/height 推导。
        # ------------------------------------------------------------------
        self.sw, self.sh = screen_size(self.root)
        self.compact = is_compact(self.root)
        self.m = ui_metrics(self.compact)
        apply_window(self.root, ideal=(1280, 820), min_size=(760, 460))
        self.root.configure(bg="#1e1e24")

        # 1. 初始化消息中心与路由
        #    未指定后端时用全局单例, 保证与独立调试器共享同一总线;
        #    指定后端时新建实例, 避免污染全局单例影响其他代码。
        self.bus = get_message_bus() if msg_backend is None else create_message_bus(backend=msg_backend)
        self.router = MessageRouter(self.bus)

        # 2. 初始化通讯模块 (经后端注册表获取, 默认仿真，可连硬件串口)
        self.comm_client = create_comm_client(backend=comm_backend)
        self.router.attach_communication(self.comm_client)

        # 3. 初始化视觉服务 (经后端注册表获取, 不绑定具体实现)
        self.vision_service = create_vision_service(self.bus, backend=vision_backend)
        self.vision_service.set_camera_source(use_synthetic=True)
        self.vision_service.start()

        # 4. 初始化程序监控服务 (经后端注册表获取)
        self.monitor_service = create_monitor_service(self.bus, backend=monitor_backend)
        self.monitor_service.start(interval_s=0.5)

        # 内部状态记录与跨线程安全队列
        self._ui_queue: queue.Queue = queue.Queue()
        self._tab4_buffer = []
        self._tab4_lock = threading.Lock()
        self._task_thread: Optional[threading.Thread] = None
        self._task_cancel = False
        self.next_task_id = 101

        # 界面构建
        self._setup_style()
        self._build_top_bar()
        self._build_tabs()

        # 订阅关键业务主题
        self.bus.subscribe(TOPIC_TASK_PROGRESS, self._on_task_progress)
        self.bus.subscribe(TOPIC_TASK_DONE, self._on_task_done)

        # 启动主线程 30Hz 定时刷新
        self.root.after(30, self._periodic_ui_loop)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_style(self):
        style = apply_dark_theme(self.root, metrics=self.m)
        style.configure("Treeview", rowheight=self.m["tree_row_h"])

    # ==========================================================================
    # 顶部常驻快捷控制栏
    # ==========================================================================

    def _build_top_bar(self):
        """
        顶部常驻快捷控制栏。

        800x480 下: 左右两侧的常驻按钮 (急停/清警 + 串口/视频源) 固定为第一行,
        状态徽章是「可被压缩」的那一项 —— 空间不足时它先让位, 保证急停键永远
        完整可见、不被裁切。屏幕/窗口变大时, 所有控件按原样铺开。
        """
        m = self.m
        bg = "#262630"
        top_bar = tk.Frame(self.root, bg=bg, padx=m["bar_padx"], pady=m["bar_pady"])
        top_bar.pack(fill=tk.X, side=tk.TOP)

        # ---- 右侧: 紧急制动与清警 (最先 pack, 保证永远保留) ----
        tk.Button(top_bar, text="紧急停止 (ESTOP)", bg="#d92525", fg="#ffffff", relief=tk.FLAT,
                  padx=10 if self.compact else 16, pady=m["btn_pady"],
                  font=font_of(m, "fs_title", bold=True),
                  command=lambda: self.bus.publish(TOPIC_COMM_ESTOP, {})).pack(side=tk.RIGHT, padx=(3, 0))

        tk.Button(top_bar, text="清除报警 / 复位", bg="#e67e22", fg="#ffffff", relief=tk.FLAT,
                  padx=7 if self.compact else 10, pady=m["btn_pady"],
                  font=font_of(m, "fs_bold", bold=True),
                  command=lambda: self.bus.publish(TOPIC_COMM_CLEAR, {})).pack(side=tk.RIGHT, padx=(3, 0))

        # ---- 左侧: 下位机连接 ----
        tk.Label(top_bar, text="下位机:", bg=bg, fg="#bbbbcc",
                 font=font_of(m, "fs_bold", bold=True)).pack(side=tk.LEFT, padx=(0, 3))

        self.comm_btn = tk.Button(
            top_bar, text="虚拟下位机 (MOCK)", bg="#7f5a00", fg="#ffdd00", relief=tk.FLAT,
            padx=m["btn_padx"], pady=m["btn_pady"], font=font_of(m, "fs_small", bold=True),
            command=self._toggle_mock_comm)
        self.comm_btn.pack(side=tk.LEFT, padx=(0, 8 if self.compact else 15))

        # ---- 左侧: 视觉相机源 (紧凑模式下收窄下拉框) ----
        tk.Label(top_bar, text="视频源:", bg=bg, fg="#bbbbcc",
                 font=font_of(m, "fs_bold", bold=True)).pack(side=tk.LEFT, padx=(0, 3))

        # 下拉框: 小屏用短标签, 避免文字被截成 "虚拟仿真 (Synt"
        if self.compact:
            src_labels = ["仿真视频源", "USB相机 0"]
            combo_w = 10
        else:
            src_labels = ["虚拟仿真 (Synthetic)", "USB 相机 (ID=0)"]
            combo_w = 18
        self._vision_src_labels = src_labels
        self.vision_source_combo = ttk.Combobox(
            top_bar, width=combo_w, values=src_labels, state="readonly",
            font=font_of(m, "fs_small"))
        self.vision_source_combo.current(0)
        self.vision_source_combo.pack(side=tk.LEFT, padx=(0, 3))

        tk.Button(top_bar, text="切换", bg="#3a3a48", fg="#ffffff", relief=tk.FLAT,
                  padx=m["btn_padx"], pady=m["btn_pady"], font=font_of(m, "fs_small"),
                  command=self._switch_vision_source).pack(side=tk.LEFT, padx=(0, 8))

        # ---- 状态徽章: 最后 pack, 空间不足时优先被压缩/让位 ----
        self.sys_status_badge = tk.Label(
            top_bar, text="系统待机就绪", bg="#2d5a27", fg="#55ff55",
            font=font_of(m, "fs_small", bold=True), padx=6 if self.compact else 10,
            pady=1, anchor="w")
        self.sys_status_badge.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _toggle_mock_comm(self):
        if self.comm_client.is_connected:
            self.comm_client.disconnect()
            self.comm_btn.config(text="连接虚拟仿真下位机", bg="#28a745")
        else:
            self.comm_client.connect_mock()
            self.comm_btn.config(text="断开下位机连接", bg="#c0392b")

    def _switch_vision_source(self):
        idx = self.vision_source_combo.current()
        if idx == 0:
            self.vision_service.set_camera_source(use_synthetic=True)
        else:
            self.vision_service.set_camera_source(use_synthetic=False, device_id=0)

    # ==========================================================================
    # 多 Tab 分页构建
    # ==========================================================================

    def _build_tabs(self):
        m = self.m
        pad_x = 3 if self.compact else 6
        pad_y = 2 if self.compact else 4

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=pad_x, pady=pad_y)

        # 800x480 下 5 个完整标签会超出 800px 导致后面的 Tab 点不到,
        # 因此紧凑模式使用短标签; 大屏保留完整描述。
        if self.compact:
            titles = ("综合调试", "通讯", "视觉", "消息", "监控")
        else:
            titles = (
                " 整体综合调试 (System Integrated) ",
                " 通讯模块单体调试 (Communication) ",
                " 视觉模块单体调试 (Vision) ",
                " 消息中心独立监听 (MessageCenter) ",
                " 监控与健康诊断 (Monitor) ",
            )

        builders = (
            self._build_tab1_integrated,
            self._build_tab2_comm,
            self._build_tab3_vision,
            self._build_tab4_message_center,
            self._build_tab5_monitor,
        )

        self._tab_frames = []
        for title, builder in zip(titles, builders):
            frame = ttk.Frame(self.notebook)
            self.notebook.add(frame, text=title)
            builder(frame)
            self._tab_frames.append(frame)

    # --------------------------------------------------------------------------
    # Tab 1: 整体综合调试
    # --------------------------------------------------------------------------
    def _build_tab1_integrated(self, parent):
        """
        左右分栏: 左=实时视觉画面, 右=2D 平台坐标图 + 底部操作按钮。

        用 grid + 权重而不是固定像素: 800x480 下按比例各占一半 (都可见、不裁切),
        窗口拉大时按同一权重同步铺满。compact 模式只收紧内边距与字号。
        """
        m = self.m
        pad = 2 if self.compact else 4

        parent.columnconfigure(0, weight=1, uniform="t1")
        parent.columnconfigure(1, weight=1, uniform="t1")
        parent.rowconfigure(0, weight=1)

        # ---- 左屏: 实时视频与视觉识别叠加 ----
        left_box = ttk.LabelFrame(parent, text=" 实时视觉画面 " if self.compact
                                  else " 实时视觉画面 (Vision Stream) ", padding=pad)
        left_box.grid(row=0, column=0, sticky="nsew", padx=(pad, pad // 2), pady=pad)
        left_box.columnconfigure(0, weight=1)
        left_box.rowconfigure(0, weight=1)

        self.tab1_video_lbl = tk.Label(left_box, bg="#131318")
        self.tab1_video_lbl.grid(row=0, column=0, sticky="nsew")

        # 视觉识别状态与当前锁定目标
        # wraplength 必须给一个合理初值: 设为 1 会让标签为了折行而撑到几百像素高,
        # 反而把整个左栏顶出屏幕。这里先按屏幕宽度估一个值, 映射后再按实际宽度精修。
        self.tab1_target_lbl = tk.Label(
            left_box, text="当前未锁定工件", bg="#181820", fg="#00ffaa",
            font=font_of(m, "fs_small", bold=True), pady=1, anchor="w",
            justify=tk.LEFT, wraplength=max(120, self.sw // 2 - 30))
        self.tab1_target_lbl.grid(row=1, column=0, sticky="ew", pady=(pad, 0))

        # ---- 右屏: 2D 机械臂运动平台与闭环测试流水线 ----
        right_box = ttk.LabelFrame(parent, text=" 机械臂平台与闭环测试 " if self.compact
                                   else " 机械臂平台运动与端到端闭环测试 ", padding=pad)
        right_box.grid(row=0, column=1, sticky="nsew", padx=(pad // 2, pad), pady=pad)
        right_box.columnconfigure(0, weight=1)
        right_box.rowconfigure(0, weight=1)     # 画布吃掉多余空间

        # 2D 画布: 不写死高度, 由 grid 权重分配
        self.tab1_canvas = PlatformCanvasView(right_box, height=1 if self.compact else 260,
                                              compact=self.compact)
        self.tab1_canvas.grid(row=0, column=0, sticky="nsew")

        row = 1
        # 机械臂实时读数
        read_f = tk.Frame(right_box, bg="#181820", padx=4 if self.compact else 8, pady=1)
        read_f.grid(row=row, column=0, sticky="ew", pady=(pad, 0))
        row += 1
        self.tab1_coord_lbl = tk.Label(
            read_f, text="机械臂坐标: X=0.00 mm | Y=0.00 mm",
            bg="#181820", fg="#00ffaa", font=font_of(m, "fs_small", bold=True, mono=True),
            anchor="w")
        self.tab1_coord_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.tab1_stage_lbl = tk.Label(
            read_f, text="阶段: 待机", bg="#181820", fg="#bbbbcc",
            font=font_of(m, "fs_small"), anchor="e")
        self.tab1_stage_lbl.pack(side=tk.RIGHT)

        # 进度条
        self.tab1_prog_bar = ttk.Progressbar(right_box, orient=tk.HORIZONTAL,
                                             mode="determinate")
        self.tab1_prog_bar.grid(row=row, column=0, sticky="ew", pady=1)
        row += 1

        # 一键闭环分拣按键 (底部操作区, 必须完整可见)
        action_f = tk.Frame(right_box, bg="#1e1e24", pady=1 if self.compact else 4)
        action_f.grid(row=row, column=0, sticky="ew")
        row += 1
        action_f.columnconfigure(0, weight=1)

        self.tab1_auto_btn = tk.Button(
            action_f, text="一键执行【视觉引导自动分拣】" if not self.compact
            else "一键自动分拣",
            bg="#27ae60", fg="#ffffff", font=font_of(m, "fs_bold", bold=True),
            relief=tk.FLAT, pady=m["btn_pady"], command=self._start_vision_guided_sort)
        self.tab1_auto_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3))

        tk.Button(
            action_f, text="取消" if self.compact else "取消任务",
            bg="#7f8c8d", fg="#ffffff", font=font_of(m, "fs_bold", bold=True),
            relief=tk.FLAT, pady=m["btn_pady"], padx=6,
            command=lambda: self.bus.publish("task/cancel", {})
        ).grid(row=0, column=1, sticky="ew")

        # 状态流水追踪日志
        self.tab1_status_log = tk.Label(
            right_box, text="等待下发指令...", bg="#131318", fg="#9999aa",
            height=m["log_h"], relief=tk.GROOVE, anchor="w", justify=tk.LEFT,
            padx=4, font=font_of(m, "fs_small"))
        self.tab1_status_log.grid(row=row, column=0, sticky="ew", pady=(2, 0))

        # 目标文字换行宽度随左栏实际宽度变化, 避免长文本把左栏撑宽/撑高
        def _rewrap_target(event):
            try:
                self.tab1_target_lbl.configure(wraplength=max(80, event.width - 10))
            except Exception:
                pass

        left_box.bind("<Configure>", _rewrap_target)

    def _start_vision_guided_sort(self):
        """视觉引导闭环分拣: 取当前视野中最优先的工件，发送分拣任务"""
        if not self.comm_client.is_connected:
            self.comm_client.connect_mock()
            self.comm_btn.config(text="断开下位机连接", bg="#c0392b")

        _, targets = self.vision_service.get_latest_frame_and_targets()
        if not targets:
            messagebox.showinfo("提示", "当前视觉视野中未发现任何颜色工件！")
            return

        target = targets[0]
        task_id = self.next_task_id
        self.next_task_id += 1

        self.tab1_canvas.set_target(target.x_mm, target.y_mm)
        self.tab1_status_log.config(
            text=f"已锁定工件 #{target.target_id} [{target.color.upper()}]: (X={target.x_mm:.1f}, Y={target.y_mm:.1f})，启动分拣...",
            fg="#00e676"
        )

        # 通过消息中心发布任务
        self.bus.publish(TOPIC_TASK_START, {
            "task_id": task_id,
            "x_mm": target.x_mm,
            "y_mm": target.y_mm
        }, sender="gui_tab1")

    # --------------------------------------------------------------------------
    # Tab 2: 通讯单体调试
    # --------------------------------------------------------------------------
    def _build_tab2_comm(self, parent):
        """左=机构动作/单步调试, 右=32B Hex 报文监视。窄屏时同样左右并排且都可见。"""
        m = self.m
        pad = 3 if self.compact else 8
        gap = 3 if self.compact else 6

        parent.columnconfigure(0, weight=1, uniform="t2")
        parent.columnconfigure(1, weight=1, uniform="t2")
        parent.rowconfigure(0, weight=1)

        # ---- 左侧: 机构动作与单步调试 ----
        ctl_box = ttk.LabelFrame(parent, text=" 机构动作与单步调试 ", padding=pad)
        ctl_box.grid(row=0, column=0, sticky="nsew", padx=(pad, gap // 2), pady=pad)
        ctl_box.columnconfigure(0, weight=1)

        # 移动指令行
        m_row = tk.Frame(ctl_box, bg="#1e1e24")
        m_row.grid(row=0, column=0, sticky="ew", pady=2)
        tk.Label(m_row, text="目标 X(mm):", bg="#1e1e24", fg="#cccccc",
                 font=font_of(m, "fs_small")).pack(side=tk.LEFT)
        self.tab2_x_entry = ttk.Entry(m_row, width=5 if self.compact else 8)
        self.tab2_x_entry.insert(0, "40.0")
        self.tab2_x_entry.pack(side=tk.LEFT, padx=2)

        tk.Label(m_row, text="Y(mm):" if self.compact else "目标 Y(mm):",
                 bg="#1e1e24", fg="#cccccc", font=font_of(m, "fs_small")).pack(side=tk.LEFT, padx=(4, 0))
        self.tab2_y_entry = ttk.Entry(m_row, width=5 if self.compact else 8)
        self.tab2_y_entry.insert(0, "20.0")
        self.tab2_y_entry.pack(side=tk.LEFT, padx=2)

        tk.Button(m_row, text="发送移动", bg="#007acc", fg="#ffffff", relief=tk.FLAT,
                  padx=m["btn_padx"], font=font_of(m, "fs_small", bold=True),
                  command=self._tab2_send_move).pack(side=tk.LEFT, padx=(4, 0))

        # 预设点 (紧凑模式用短名称, 4 个按钮挤在一行也不溢出)
        pre_row = tk.Frame(ctl_box, bg="#1e1e24")
        pre_row.grid(row=1, column=0, sticky="ew", pady=2)
        if self.compact:
            presets = [("原点", 0, 0), ("取料1", 40, 20), ("取料2", 60, 30), ("放置", 80, 0)]
        else:
            presets = [("原点 (0,0)", 0, 0), ("取料1 (40,20)", 40, 20),
                       ("取料2 (60,30)", 60, 30), ("放置 (80,0)", 80, 0)]
        for i, (name, px, py) in enumerate(presets):
            pre_row.columnconfigure(i, weight=1, uniform="preset")
            tk.Button(pre_row, text=name, bg="#2b2b36", fg="#d0d0e0", relief=tk.FLAT,
                      padx=2, pady=m["btn_pady"], font=font_of(m, "fs_small"),
                      command=lambda x=px, y=py: (
                          self.tab2_x_entry.delete(0, tk.END), self.tab2_x_entry.insert(0, str(x)),
                          self.tab2_y_entry.delete(0, tk.END), self.tab2_y_entry.insert(0, str(y)))
                      ).grid(row=0, column=i, sticky="ew", padx=1)

        # 夹爪与动作
        g_row = tk.Frame(ctl_box, bg="#1e1e24")
        g_row.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        for i, (txt, cmd) in enumerate((
                ("夹爪张开" if self.compact else "夹爪张开 (OPEN)",
                 lambda: (self.comm_client.send_grip_open(), self._tab2_echo("→ 夹爪张开 OPEN"))),
                ("夹爪闭合" if self.compact else "夹爪闭合 (CLOSE)",
                 lambda: (self.comm_client.send_grip_close(), self._tab2_echo("→ 夹爪闭合 CLOSE"))),
                ("抓取许可" if self.compact else "抓取许可 (PERMIT)",
                 lambda: (self.comm_client.send_permit(0), self._tab2_echo("→ 抓取许可 PERMIT"))))):
            g_row.columnconfigure(i, weight=1, uniform="grip")
            tk.Button(g_row, text=txt, bg="#2c3e50" if i < 2 else "#27ae60", fg="#ffffff",
                      relief=tk.FLAT, padx=2, pady=m["btn_pady"],
                      font=font_of(m, "fs_small", bold=True), command=cmd
                      ).grid(row=0, column=i, sticky="ew", padx=1)

        # 左栏剩余空间: 显示最近一次下发的指令回显 (同时消除大片空白)
        ctl_box.rowconfigure(3, weight=1)
        tk.Label(ctl_box, text="指令回显:", bg="#1e1e24", fg="#8899aa",
                 font=font_of(m, "fs_small"), anchor="w").grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self.tab2_echo_text = tk.Text(
            ctl_box, bg="#131318", fg="#88ccff", height=2, wrap=tk.WORD,
            font=font_of(m, "fs_small", mono=True), relief=tk.FLAT)
        self.tab2_echo_text.grid(row=4, column=0, sticky="nsew")
        ctl_box.rowconfigure(4, weight=1)
        self.tab2_echo_text.insert("1.0", "等待下发指令...")

        # ---- 右侧: 32 字节 Hex 实时报文监视器 ----
        hex_box = ttk.LabelFrame(parent, text=" 32 字节 Hex 报文 ", padding=pad)
        hex_box.grid(row=0, column=1, sticky="nsew", padx=(gap // 2, pad), pady=pad)
        hex_box.columnconfigure(0, weight=1)
        hex_box.rowconfigure(0, weight=1)

        self.tab2_log_text = tk.Text(
            hex_box, bg="#0f0f14", fg="#00ffaa", wrap=tk.NONE, height=1,
            font=font_of(m, "fs_small", mono=True))
        self.tab2_log_text.grid(row=0, column=0, sticky="nsew")

        hex_sb = ttk.Scrollbar(hex_box, orient=tk.VERTICAL, command=self.tab2_log_text.yview)
        self.tab2_log_text.configure(yscrollcommand=hex_sb.set)
        hex_sb.grid(row=0, column=1, sticky="ns")

        def on_raw_rx(data: bytes):
            if len(data) >= 32:
                hex_str = " ".join(f"{b:02X}" for b in data[:32])
                t_str = time.strftime("%H:%M:%S")

                def append_log():
                    try:
                        self.tab2_log_text.insert(tk.END, f"[{t_str}] RX: {hex_str}\n")
                        self.tab2_log_text.see(tk.END)
                    except Exception:
                        pass
                self.root.after_idle(append_log)

        self.comm_client.on_raw_rx_callbacks.append(on_raw_rx)

    def _tab2_send_move(self):
        try:
            x = float(self.tab2_x_entry.get().strip())
            y = float(self.tab2_y_entry.get().strip())
            self.comm_client.send_move(task_id=0, x_mm=x, y_mm=y)
            self._tab2_echo(f"→ 移动指令 MOVE X={x:.1f} Y={y:.1f} mm")
        except Exception as e:
            messagebox.showerror("错误", str(e))

    def _tab2_echo(self, text: str):
        """在左栏指令回显区追加一行 (最多保留 20 行)"""
        try:
            widget = getattr(self, "tab2_echo_text", None)
            if widget is None:
                return
            line = f"[{time.strftime('%H:%M:%S')}] {text}\n"
            widget.insert(tk.END, line)
            widget.see(tk.END)
            if int(widget.index("end-1c").split(".")[0]) > 20:
                widget.delete("1.0", "2.0")
        except Exception:
            pass

    # --------------------------------------------------------------------------
    # Tab 3: 视觉单体调试
    # --------------------------------------------------------------------------
    def _build_tab3_vision(self, parent):
        """左=视频画面 (可伸缩), 右=HSV 标定控制卡 (固定窄列)。"""
        m = self.m
        pad = 3 if self.compact else 8
        gap = 3 if self.compact else 6

        parent.columnconfigure(0, weight=1)                  # 视频占满剩余宽度
        parent.columnconfigure(1, weight=0)                  # 控制卡保持自然宽度
        parent.rowconfigure(0, weight=1)

        # 视频画面
        self.tab3_video_lbl = tk.Label(parent, bg="#131318")
        self.tab3_video_lbl.grid(row=0, column=0, sticky="nsew",
                                 padx=(pad, gap // 2), pady=pad)

        # HSV 调节卡片
        ctl = ttk.LabelFrame(parent, text=" 颜色提取与标定 ", padding=pad)
        ctl.grid(row=0, column=1, sticky="nsew", padx=(gap // 2, pad), pady=pad)
        ctl.columnconfigure(0, weight=1)
        ctl.rowconfigure(2, weight=1)                        # 目标列表吃掉纵向空间

        tk.Label(ctl, text="调参 (HSV/标定/托盘/畸变/相机) 已统一到浏览器:\nhttp://<板子IP>:8090",
                 bg="#1e1e24", fg="#ffcc66", font=font_of(m, "fs_small"), anchor="w",
                 justify=tk.LEFT).grid(row=0, column=0, sticky="ew")
        tk.Button(ctl, text="启动浏览器调参台 (释放本工作台相机)", bg="#3a3a48", fg="#ffffff",
                  relief=tk.FLAT, font=font_of(m, "fs_small"),
                  command=self._tab3_open_web).grid(row=1, column=0, sticky="ew", pady=2)

        self.tab3_calib_lbl = tk.Label(
            ctl, text="标定: 4 点透视映射生效" if self.compact else "标定状态: 默认 4 点透视映射生效中",
            bg="#1e1e24", fg="#00e676", font=font_of(m, "fs_small"), anchor="w")
        self.tab3_calib_lbl.grid(row=3, column=0, sticky="ew", pady=(2, 2))

        # 目标读数列表: 不写死 height, 交给 grid 权重
        self.tab3_target_text = tk.Text(
            ctl, width=14 if self.compact else 32, height=1,
            bg="#131318", fg="#00ffaa", font=font_of(m, "fs_small", mono=True),
            wrap=tk.WORD)
        self.tab3_target_text.grid(row=2, column=0, sticky="nsew", pady=1)

        # 控制卡宽度: 小屏收窄, 大屏给足, 但始终不超过屏幕的 45%
        ctl.configure(width=170 if self.compact else 300)

    def _tab3_open_web(self):
        """停掉本工作台的视觉服务 (相机只能一个进程占用), 拉起 tools/vision_tool.py web。"""
        import subprocess
        if getattr(self, "_web_proc", None) and self._web_proc.poll() is None:
            messagebox.showinfo("提示", "浏览器调参台已在运行: http://<板子IP>:8090")
            return
        try:
            self.vision_service.stop()
        except Exception:
            pass
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self._web_proc = subprocess.Popen([sys.executable, os.path.join(root, "tools", "vision_tool.py"),
                                           "web", "--force"], cwd=root)
        self.tab3_calib_lbl.config(text="已启动: 浏览器打开 http://<板子IP>:8090")

        def show_token(tries=10):
            try:
                with open(os.path.join(root, "tools", ".web_token"), encoding="utf-8") as f:
                    tok = f.read().strip()
            except OSError:
                tok = ""
            if tok:
                self.tab3_calib_lbl.config(text=f"已启动: http://<板子IP>:8090  口令 {tok}")
            elif tries > 0:
                self.root.after(500, lambda: show_token(tries - 1))
        self.root.after(500, show_token)

    def _build_tab4_message_center(self, parent):
        m = self.m
        pad = 3 if self.compact else 8

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)     # 报文表吃掉纵向空间
        parent.rowconfigure(1, weight=0)     # 注入器固定高度, 始终可见

        columns = ("id", "time", "sender", "topic", "payload")
        self.tab4_tree = ttk.Treeview(parent, columns=columns, show="headings", height=1)
        for col, title, width, anchor in (
                ("id", "序号", 40, "center"),
                ("time", "时间", 66, "center"),
                ("sender", "来源", 78, "center"),
                ("topic", "主题", 150, "w"),
                ("payload", "内容", 400, "w")):
            self.tab4_tree.heading(col, text=title)
            self.tab4_tree.column(col, width=width, anchor=anchor, stretch=(col in ("topic", "payload")))
        self.tab4_tree.grid(row=0, column=0, sticky="nsew", padx=(pad, 0), pady=(pad, 0))

        tree_scroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.tab4_tree.yview)
        self.tab4_tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.grid(row=0, column=1, sticky="ns", pady=(pad, 0), padx=(0, pad))

        # 注入工具
        inj_f = tk.LabelFrame(parent, text=" 事件注入测试器 " if not self.compact else " 事件注入 ",
                              bg="#1e1e24", fg="#00aaff", font=font_of(m, "fs_bold", bold=True),
                              padx=pad + 2, pady=pad)
        inj_f.grid(row=1, column=0, columnspan=2, sticky="ew", padx=pad, pady=pad)
        inj_f.columnconfigure(1, weight=0)
        inj_f.columnconfigure(3, weight=1)

        tk.Label(inj_f, text="主题:", bg="#1e1e24", fg="#cccccc",
                 font=font_of(m, "fs_small")).grid(row=0, column=0, sticky="w")
        self.tab4_topic_entry = ttk.Entry(inj_f, width=13 if self.compact else 20,
                                          font=font_of(m, "fs_small"))
        self.tab4_topic_entry.insert(0, TOPIC_TASK_START)
        self.tab4_topic_entry.grid(row=0, column=1, sticky="ew", padx=3)

        tk.Label(inj_f, text="内容:", bg="#1e1e24", fg="#cccccc",
                 font=font_of(m, "fs_small")).grid(row=0, column=2, sticky="w")
        self.tab4_payload_entry = ttk.Entry(inj_f, font=font_of(m, "fs_small"))
        self.tab4_payload_entry.insert(0, '{"task_id": 999, "x_mm": 50.0, "y_mm": 20.0}')
        self.tab4_payload_entry.grid(row=0, column=3, sticky="ew", padx=3)

        tk.Button(inj_f, text="发布消息" if not self.compact else "发布", bg="#007acc", fg="#ffffff",
                  relief=tk.FLAT, padx=m["btn_padx"], font=font_of(m, "fs_small", bold=True),
                  command=self._tab4_inject).grid(row=0, column=4, sticky="e")

        def _on_tab4_msg(msg: Message):
            with self._tab4_lock:
                self._tab4_buffer.append(msg)
        self.bus.subscribe("*", _on_tab4_msg)

    def _tab4_inject(self):
        topic = self.tab4_topic_entry.get().strip()
        raw = self.tab4_payload_entry.get().strip()
        try:
            pl = json.loads(raw)
        except Exception:
            pl = raw
        self.bus.publish(topic, pl, sender="manual_gui")

    # --------------------------------------------------------------------------
    # Tab 5: 程序监控与健康看板
    # --------------------------------------------------------------------------
    def _build_tab5_monitor(self, parent):
        m = self.m
        pad = 3 if self.compact else 8

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=0)     # CPU 行
        parent.rowconfigure(1, weight=1)     # 模块表
        parent.rowconfigure(2, weight=1)     # 告警表

        # 主机 CPU/内存
        m_row = tk.Frame(parent, bg="#181820", padx=4 if self.compact else 10, pady=2 if self.compact else 6)
        m_row.grid(row=0, column=0, sticky="ew", pady=(pad, 2))

        self.tab5_cpu_lbl = tk.Label(m_row, text="CPU: 0.0%", bg="#181820", fg="#00ffaa",
                                     font=font_of(m, "fs_small", bold=True))
        self.tab5_cpu_lbl.pack(side=tk.LEFT, padx=4)
        self.tab5_mem_lbl = tk.Label(m_row, text="内存: 0.0%", bg="#181820", fg="#00ffaa",
                                     font=font_of(m, "fs_small", bold=True))
        self.tab5_mem_lbl.pack(side=tk.LEFT, padx=4)
        self.tab5_th_lbl = tk.Label(m_row, text="线程数: 0", bg="#181820", fg="#cccccc",
                                    font=font_of(m, "fs_small"))
        self.tab5_th_lbl.pack(side=tk.LEFT, padx=4)

        # 模块健康列表
        mod_box = ttk.LabelFrame(parent, text=" 模块在线监控与看门狗 " if self.compact
                                 else " 模块在线监控与看门狗状态 ", padding=pad)
        mod_box.grid(row=1, column=0, sticky="nsew", padx=pad, pady=2)
        mod_box.columnconfigure(0, weight=1)
        mod_box.rowconfigure(0, weight=1)

        cols = ("name", "status", "fps", "elapsed")
        self.tab5_tree = ttk.Treeview(mod_box, columns=cols, show="headings", height=1)
        for col, title, width in (("name", "子模块", 130), ("status", "在线状态", 100),
                                  ("fps", "速率(FPS)", 90), ("elapsed", "距心跳(s)", 90)):
            self.tab5_tree.heading(col, text=title)
            self.tab5_tree.column(col, width=width, anchor="center", stretch=True)
        self.tab5_tree.grid(row=0, column=0, sticky="nsew")

        # 告警日志
        alert_box = ttk.LabelFrame(parent, text=" 系统告警记录 ", padding=pad)
        alert_box.grid(row=2, column=0, sticky="nsew", padx=pad, pady=2)
        alert_box.columnconfigure(0, weight=1)
        alert_box.rowconfigure(0, weight=1)

        a_cols = ("time", "level", "source", "msg")
        self.tab5_alert_tree = ttk.Treeview(alert_box, columns=a_cols, show="headings", height=1)
        for col, title, width in (("time", "时间", 70), ("level", "级别", 60),
                                  ("source", "来源", 90), ("msg", "告警描述", 300)):
            self.tab5_alert_tree.heading(col, text=title)
            self.tab5_alert_tree.column(col, width=width, anchor="center" if col != "msg" else "w",
                                        stretch=True)
        self.tab5_alert_tree.grid(row=0, column=0, sticky="nsew")

    # ==========================================================================
    # 周期主更新循环 (30ms)
    # ==========================================================================

    def _periodic_ui_loop(self):
        # 1. 刷新视觉画面 (Tab 1 和 Tab 3)
        frame, targets = self.vision_service.get_latest_frame_and_targets()
        if frame is not None:
            # 缩放至 UI 适配大小
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, _ = rgb.shape

            # 画面尺寸一律取自控件实际大小: 写死 480x320 在 800x480 上会超出
            # 可用区域, 窗口拉大时又会留下大片空白, 两者都不是真正的自适应。
            cur = self.notebook.index("current")

            # Tab 1 视频
            nw1, nh1 = scale_to_fit(w, h,
                                    max(40, self.tab1_video_lbl.winfo_width()),
                                    max(30, self.tab1_video_lbl.winfo_height()))
            self._refresh_label_image(
                self.tab1_video_lbl,
                cv2.resize(rgb, (nw1, nh1), interpolation=cv2.INTER_LINEAR),
                cache_attr="_img1_size", cache_size=(nw1, nh1))

            # Tab 3 视频 (仅在视觉页可见时刷新, 省 CPU)
            if cur == 2:
                nw3, nh3 = scale_to_fit(w, h,
                                        max(40, self.tab3_video_lbl.winfo_width()),
                                        max(30, self.tab3_video_lbl.winfo_height()))
                self._refresh_label_image(
                    self.tab3_video_lbl,
                    cv2.resize(rgb, (nw3, nh3), interpolation=cv2.INTER_LINEAR),
                    cache_attr="_img3_size", cache_size=(nw3, nh3))

                # 右侧目标读数列表: 原实现从未填充, 视觉单体调试页右侧一直是空框
                lines = [f"=== 识别到 {len(targets)} 个目标 ==="]
                for t in targets:
                    lines.append(f"#{t.target_id} [{t.color.upper()}] {t.confidence * 100:.0f}%")
                    if not self.compact:
                        lines.append(f"  像素:(u={t.u_pixel}, v={t.v_pixel})")
                    lines.append(f"  X={t.x_mm:+.1f} Y={t.y_mm:+.1f} mm")
                self._update_text_widget(self.tab3_target_text, "\n".join(lines))

            # 更新目标信息
            if targets:
                p = targets[0]
                self.tab1_target_lbl.config(
                    text=f"锁定目标: #{p.target_id} [{p.color.upper()}] 坐标: (X={p.x_mm:+.1f}mm, Y={p.y_mm:+.1f}mm) | 置信度: {p.confidence*100:.0f}%",
                    fg="#00e676"
                )
            else:
                self.tab1_target_lbl.config(text="视野中未检测到工件目标", fg="#888899")

        # 2. 刷新下位机坐标与画布
        st = self.comm_client.latest_status
        if st is not None:
            self.tab1_canvas.update_position(st.cur_x_mm, st.cur_y_mm)
            self.tab1_coord_lbl.config(text=f"机械臂坐标: X={st.cur_x_mm:+.2f} mm | Y={st.cur_y_mm:+.2f} mm")
            self.tab1_stage_lbl.config(text=f"阶段: {st.stage_desc} ({st.progress}%)")
            self.tab1_prog_bar["value"] = st.progress

            if st.is_estop:
                self.sys_status_badge.config(text="急停生效中 (ESTOP)", bg="#8b0000", fg="#ff5555")
            elif st.is_alarm:
                self.sys_status_badge.config(text=f"报警: {st.error_desc}", bg="#6e3b00", fg="#ffaa00")
            elif st.is_busy:
                self.sys_status_badge.config(text="执行中 (RUNNING)", bg="#1a3d6b", fg="#4db8ff")
            else:
                self.sys_status_badge.config(text="系统待机空闲 (IDLE)", bg="#2d5a27", fg="#55ff55")

        # 0. 消费跨线程安全更新队列 (在 Tkinter 主线程中执行)
        while not self._ui_queue.empty():
            try:
                action = self._ui_queue.get_nowait()
                action()
            except Exception:
                pass

        # 3. 刷新 Tab 4 消息中心树
        with self._tab4_lock:
            to_add = list(self._tab4_buffer)
            self._tab4_buffer.clear()

        if to_add:
            for m in to_add:
                pl_str = str(m.payload)
                if len(pl_str) > 100:
                    pl_str = pl_str[:97] + "..."
                self.tab4_tree.insert("", 0, values=(m.msg_id, m.time_str, m.sender, m.topic, pl_str))

            kids = self.tab4_tree.get_children()
            if len(kids) > 150:
                for k in kids[150:]:
                    self.tab4_tree.delete(k)

        # 4. 刷新 Tab 5 监控树与告警 (每 10 次循环更新一次)
        if hasattr(self, "_cnt"):
            self._cnt += 1
        else:
            self._cnt = 0

        if self._cnt % 10 == 0:
            metrics = self.monitor_service.get_system_metrics()
            self.tab5_cpu_lbl.config(text=f"CPU: {metrics['cpu_percent']:.1f}%")
            self.tab5_mem_lbl.config(text=f"内存: {metrics['memory_percent']:.1f}%")
            self.tab5_th_lbl.config(text=f"线程数: {metrics['threads_count']}")

            mods = self.monitor_service.check_all_modules()
            for item in self.tab5_tree.get_children():
                self.tab5_tree.delete(item)
            for name, rec in mods.items():
                st_str = "正常 (OK)" if rec.status == "OK" else "离线 (OFFLINE)"
                el_str = f"{rec.elapsed_since_last:.1f}" if rec.last_heartbeat_time > 0 else "-"
                self.tab5_tree.insert("", tk.END, values=(name, st_str, f"{rec.fps:.1f}", el_str))

            # 刷新活动告警列表
            alerts = self.monitor_service.get_active_alerts()
            for item in self.tab5_alert_tree.get_children():
                self.tab5_alert_tree.delete(item)
            for a in alerts:
                t_str = time.strftime("%H:%M:%S", time.localtime(a.timestamp))
                self.tab5_alert_tree.insert("", tk.END, values=(t_str, a.level, a.source, a.message))

        self.root.after(30, self._periodic_ui_loop)

    def _update_text_widget(self, widget: tk.Text, content: str, cache_attr: str = None):
        """内容变化时才重写 Text 控件, 避免 30Hz 无谓重绘造成闪烁"""
        key = cache_attr or f"_txt_cache_{id(widget)}"
        if getattr(self, key, None) == content:
            return
        setattr(self, key, content)
        try:
            widget.delete("1.0", tk.END)
            widget.insert(tk.END, content)
        except Exception:
            pass

    def _refresh_label_image(self, label: tk.Label, rgb_array,
                             cache_attr: str = None, cache_size=None):
        """
        刷新 Label 上的视频画面。

        尺寸不变时复用同一个 PhotoImage 只更新像素 (避免 30Hz 下每帧新建 Tk
        图像对象导致内存增长); 尺寸变化或复用失败时重建。任何异常都回退到
        重建, 保证画面不会因为优化而停止刷新。
        """
        pil = Image.fromarray(rgb_array)
        if cache_attr is not None:
            unchanged = (cache_size == getattr(self, cache_attr, None))
            if unchanged:
                img_tk = getattr(label, "image", None)
                try:
                    if img_tk is not None:
                        img_tk.paste(pil)
                        return
                except Exception:
                    pass
            else:
                setattr(self, cache_attr, cache_size)

        img_tk = ImageTk.PhotoImage(image=pil)
        label.configure(image=img_tk)
        label.image = img_tk

    def _on_task_progress(self, msg: Message):
        payload = msg.payload
        if isinstance(payload, dict):
            stage = payload.get("stage", "")
            pct = payload.get("progress", 0)
            self._ui_queue.put(lambda: self.tab1_status_log.config(text=f"[{pct}%] {stage}", fg="#00e676"))

    def _on_task_done(self, msg: Message):
        payload = msg.payload
        if isinstance(payload, dict) and payload.get("success"):
            self._ui_queue.put(lambda: self.tab1_status_log.config(text="分拣闭环流程已成功完成！机械臂归位。", fg="#00e676"))
        else:
            self._ui_queue.put(lambda: self.tab1_status_log.config(text="分拣流程已结束或已中止。", fg="#ff7675"))

    def _on_close(self):
        self.vision_service.stop()
        self.monitor_service.stop()
        self.comm_client.disconnect()
        self.root.destroy()


def run_unified_debugger(vision_backend: str = None,
                         comm_backend: str = None,
                         monitor_backend: str = None,
                         msg_backend: str = None):
    root = tk.Tk()
    app = UnifiedSystemDebugger(
        root,
        vision_backend=vision_backend,
        comm_backend=comm_backend,
        monitor_backend=monitor_backend,
        msg_backend=msg_backend,
    )
    root.mainloop()


if __name__ == "__main__":
    run_unified_debugger()
