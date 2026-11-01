"""
STM32 智能分拣系统 - Windows 桌面调试与监控上位机
对齐 D:\\stm32\\stm32cubemx\\IntelligentSorting\\User\\Module\\Communication\\HOST_COMM_GUIDE.md

功能模块:
1. USB CDC 串口与虚拟仿真切换
2. 一键急停 (ESTOP) 与故障复位 (CLEAR)
3. 机构状态、夹爪、错误掩码、流水号实时遥测大字显示
4. 2D 平台机械臂运动轨迹与工位实时监控
5. 单动点动控制 (JOG)、坐标定点移动、直接夹爪动作
6. 全流程自动化分拣闭环测试 (支持自动/手动许可触发)
7. 原始 32 字节 Hex 报文与业务日志实时滚动记录
"""

from __future__ import annotations
import sys
import os
import time
import queue
import threading
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# 确保能正确引入 communication 模块
# (本文件位于 debug/communication_debug/, 需上溯两级到项目根)
CUR_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(os.path.dirname(CUR_DIR))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from communication import (
    IntelligentSortingClient, HostStatus, HostCmd,
    SysState, GripperState, ZState, TrayState, ResFlags, ResStage, ErrorMask,
    GripCmd, ZCmd, TrayCmd, StorageCmd, CmdFlags,
    create_comm_client,
)
from debug.common.canvas_view import PlatformCanvasView
from debug.common.theme import apply_dark_theme
from debug.common.responsive import (
    apply_window, is_compact, screen_size, ui_metrics, font_of, ScrolledFrame,
)


class SortingDebugApp:
    """
    上位机图形化调试应用程序
    """

    def __init__(self, root: tk.Tk, backend: str = None):
        self.root = root
        self.root.title("STM32 智能分拣系统 - 上位机下位机联合调试工具 v1.0")

        # ------------------------------------------------------------------
        # 屏幕自适应: 物理屏 800x480 且无窗口管理器, 写死 1280x800 会被裁掉。
        # 本界面原本是三栏并排 (需求宽度 ~1395px), 小屏必须换成分栏结构,
        # 否则中间控制台和右侧日志都会被推出屏幕。
        # ------------------------------------------------------------------
        self.sw, self.sh = screen_size(self.root)
        self.compact = is_compact(self.root)
        self.m = ui_metrics(self.compact)
        apply_window(self.root, ideal=(1280, 800), min_size=(760, 440))

        # 客户端与后台通讯 (经后端注册表获取, 不绑定具体实现)
        self.client = create_comm_client(backend=backend)
        self.client.on_status_callbacks.append(self._on_client_status)
        self.client.on_raw_rx_callbacks.append(self._on_client_raw_rx)
        self.client.on_raw_tx_callbacks.append(self._on_client_raw_tx)

        # 线程安全更新队列
        self.msg_queue: queue.Queue = queue.Queue()

        # 任务与执行线程
        self._task_thread: Optional[threading.Thread] = None
        self._task_cancel_flag: bool = False
        self.next_task_id: int = 101

        # 遥测帧率计算
        self._fps_count: int = 0
        self._fps_timer: float = time.time()
        self.current_fps: float = 0.0

        # 线程安全的标志缓存 (避免后台接收线程跨线程调用 Tkinter 变量引发死锁)
        self.hex_mode_enabled: bool = False

        # 构建界面
        # 注意顺序: 底部状态栏先 pack, 否则会被 expand=True 的主区挤成 0 高度
        # (pack 按调用顺序分配空间, 后 pack 的 expand 控件会把剩余空间吃光)。
        self._setup_style()
        self._build_top_bar()
        self._build_status_bar()
        self._build_main_layout()

        # 启动 UI 周期刷新定时器 (30ms ~ 33Hz)
        self.root.after(30, self._periodic_ui_update)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_style(self):
        style = apply_dark_theme(self.root, metrics=self.m)

        # 配色规范 (在通用深色主题之上的额外项)
        style.configure("Accent.TButton", font=font_of(self.m, "fs_bold", bold=True),
                        foreground="#ffffff", background="#007acc")

    # ==========================================================================
    # 顶部控制栏 (连接配置与紧急保护)
    # ==========================================================================

    def _build_top_bar(self):
        """
        顶部控制栏。

        800x480 下原来一行要放 ~1010px 的控件 (实测), 必然溢出。改成两行:
          * 第 1 行: 串口端口 / 波特率 / 连接 / 仿真勾选 / 状态胶囊
          * 第 2 行: 紧急停止 + 清除报警 (永远完整可见, 不会被裁)
        大屏仍是原来的单行布局。
        """
        m = self.m
        fs, fs_b = font_of(m, "fs"), font_of(m, "fs_bold", bold=True)
        bg = "#262630"

        top_frame = tk.Frame(self.root, bg=bg, padx=m["bar_padx"], pady=m["bar_pady"])
        top_frame.pack(fill=tk.X, side=tk.TOP)

        # 小屏: 危险按钮单独占一行并置顶优先保证; 大屏: 与连接控制同一行右侧
        if self.compact:
            danger_row = tk.Frame(self.root, bg=bg, padx=m["bar_padx"], pady=1)
            danger_row.pack(fill=tk.X, side=tk.TOP, after=top_frame)
            estop_host, clear_host = danger_row, danger_row
        else:
            estop_host = clear_host = top_frame

        # ---- 右侧紧急按钮 (先 pack, 保证小屏也完整) ----
        self.estop_btn = tk.Button(
            estop_host, text="紧急停止 (ESTOP)", bg="#d92525", fg="#ffffff",
            font=font_of(m, "fs_title", bold=True), relief=tk.FLAT,
            padx=10 if self.compact else 16, pady=m["btn_pady"],
            command=self._on_click_estop)
        self.estop_btn.pack(side=tk.RIGHT, padx=(3, 0))

        self.clear_btn = tk.Button(
            clear_host, text="清除报警 / 复位", bg="#e67e22", fg="#ffffff",
            font=fs_b, relief=tk.FLAT, padx=8 if self.compact else 10,
            pady=m["btn_pady"], command=self._on_click_clear)
        self.clear_btn.pack(side=tk.RIGHT, padx=(3, 0))

        # ---- 左侧连接配置 ----
        tk.Label(top_frame, text="串口:", bg=bg, fg="#cccccc", font=fs_b).pack(side=tk.LEFT, padx=(0, 3))
        self.port_combobox = ttk.Combobox(top_frame, width=12 if self.compact else 18,
                                          state="readonly", font=font_of(m, "fs_small"))
        self.port_combobox.pack(side=tk.LEFT, padx=(0, 4))

        tk.Button(
            top_frame, text="刷新", bg="#3a3a48", fg="#ffffff", relief=tk.FLAT,
            padx=m["btn_padx"], pady=m["btn_pady"], font=font_of(m, "fs_small"),
            command=self._refresh_ports
        ).pack(side=tk.LEFT, padx=(0, 6))

        tk.Label(top_frame, text="波特率:", bg=bg, fg="#cccccc", font=fs_b).pack(side=tk.LEFT, padx=(0, 3))
        self.baud_combobox = ttk.Combobox(
            top_frame, width=7 if self.compact else 10,
            values=["115200", "921600", "12000000"], state="readonly",
            font=font_of(m, "fs_small"))
        self.baud_combobox.set("115200")
        self.baud_combobox.pack(side=tk.LEFT, padx=(0, 6))

        # 连接/断开按钮
        self.connect_btn = tk.Button(
            top_frame, text="连接设备", bg="#28a745", fg="#ffffff", font=fs_b,
            relief=tk.FLAT, padx=8 if self.compact else 12, pady=m["btn_pady"],
            command=self._toggle_connect
        )
        self.connect_btn.pack(side=tk.LEFT, padx=(0, 8))

        # 虚拟仿真模式勾选
        self.mock_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            top_frame, text="仿真模式" if self.compact else "虚拟下位机仿真模式",
            variable=self.mock_var, bg=bg, fg="#ffcc00", selectcolor="#1e1e24",
            activebackground=bg, font=fs_b
        ).pack(side=tk.LEFT, padx=(0, 6))

        # 状态指示胶囊 (最后 pack, 空间不足时优先压缩)
        self.conn_badge = tk.Label(
            top_frame, text="● 未连接", bg="#444455", fg="#aaaaaa",
            font=font_of(m, "fs_small", bold=True), padx=6 if self.compact else 10,
            pady=1, anchor="w"
        )
        self.conn_badge.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 初始化串口列表
        self._refresh_ports()

    # ==========================================================================
    # 主体布局 (左: 遥测与2D视口, 中: 控制台, 右: 日志与报文)
    # ==========================================================================

    def _build_main_layout(self):
        """
        主体布局。

        大屏 (>=1000px 宽): 保持原有三栏并排 —— 左看板/画布, 中控制台, 右日志。
        小屏 (800x480): 三栏并排至少需要 ~1395px, 必然裁切。改为
          * 上半部分: 左右两栏 (左 = 遥测 + 2D 画布, 右 = 单动控制 + 闭环测试)
          * 下半部分: 日志 / Hex 面板 (固定较小高度)
        三块核心区域都完整可见, 只是改成了「上两栏 + 下日志」的结构。
        """
        pad = 3 if self.compact else 8
        gap = 3 if self.compact else 6

        if self.compact:
            # ------------------------------------------------------------------
            # 800x480 紧凑布局
            #
            # 三栏并排需要 ~1395px 宽度, 两栏 + 底部日志需要 ~722px 高度,
            # 两者都远超 800x480。实测右侧「单动控制 (406px) + 闭环测试 (174px)」
            # 就需要 580px 高度, 会把底部日志顶出屏幕。
            #
            # 因此紧凑模式改用 Notebook 三页, 每页内容都完整可见、互不挤压,
            # 顶部状态栏 (含急停) 与底部协议栏始终常驻:
            #   [监控] 遥测数据 + 2D 平台画布
            #   [控制] 机构单动控制 + 自动化闭环测试
            #   [日志] 通信与调试日志 / Hex 报文
            # ------------------------------------------------------------------
            self.body_notebook = ttk.Notebook(self.root)
            self.body_notebook.pack(fill=tk.BOTH, expand=True, padx=pad, pady=2)

            mon_tab = ttk.Frame(self.body_notebook)
            self.body_notebook.add(mon_tab, text=" 监控 ")
            self._build_telemetry_panel(mon_tab)
            self._build_canvas_panel(mon_tab)

            ctl_tab = ttk.Frame(self.body_notebook)
            self.body_notebook.add(ctl_tab, text=" 控制 ")
            # 控制页两块内容纵向叠放需要 ~504px, 超过 800x480 的可用高度 (~376px)。
            # 两者宽度都很窄 (232 / 163), 因此并排成两列: 高度取 max(~330, 174) 即可放下。
            ctl_tab.columnconfigure(0, weight=3, uniform="ctl")
            ctl_tab.columnconfigure(1, weight=2, uniform="ctl")
            ctl_tab.rowconfigure(0, weight=1)

            left_ctl = ttk.Frame(ctl_tab)
            left_ctl.grid(row=0, column=0, sticky="nsew", padx=(0, gap // 2))
            right_ctl = ttk.Frame(ctl_tab)
            right_ctl.grid(row=0, column=1, sticky="nsew", padx=(gap // 2, 0))

            self._build_motion_control_panel(left_ctl)
            self._build_workflow_panel(right_ctl)

            log_tab = ttk.Frame(self.body_notebook)
            self.body_notebook.add(log_tab, text=" 日志 ")
            self._build_log_panel(log_tab)
            return

        main_paned = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=pad, pady=6)

        # 左侧看板与 2D 画布
        left_frame = ttk.Frame(main_paned)
        main_paned.add(left_frame, weight=3)
        self._build_telemetry_panel(left_frame)
        self._build_canvas_panel(left_frame)

        # 中间动作操作台
        center_frame = ttk.Frame(main_paned)
        main_paned.add(center_frame, weight=3)
        self._build_motion_control_panel(center_frame)
        self._build_workflow_panel(center_frame)

        # 右侧日志与底层 Hex 监控
        right_frame = ttk.Frame(main_paned)
        main_paned.add(right_frame, weight=4)
        self._build_log_panel(right_frame)

    # --------------------------------------------------------------------------
    # 左栏: 实时遥测数据看板
    # --------------------------------------------------------------------------
    def _build_telemetry_panel(self, parent):
        m = self.m
        t_group = ttk.LabelFrame(parent, text=" [1] 下位机状态与遥测数据 ", padding=m["pad"])
        t_group.pack(fill=tk.X, padx=4, pady=4)

        # 状态徽章行 1: 主状态与夹爪
        badge_row1 = tk.Frame(t_group, bg="#1e1e24")
        badge_row1.pack(fill=tk.X, pady=(0, 3))

        tk.Label(badge_row1, text="主状态: ", bg="#1e1e24", fg="#aaaaaa").pack(side=tk.LEFT)
        self.state_label = tk.Label(
            badge_row1, text="待机空闲 (IDLE)", bg="#2d5a27", fg="#55ff55",
            font=("Segoe UI", 9, "bold"), padx=8, pady=1
        )
        self.state_label.pack(side=tk.LEFT, padx=(0, 15))

        tk.Label(badge_row1, text="夹爪: ", bg="#1e1e24", fg="#aaaaaa").pack(side=tk.LEFT)
        self.grip_label = tk.Label(
            badge_row1, text="未知 (UNKNOWN)", bg="#334455", fg="#88ccff",
            font=("Segoe UI", 9, "bold"), padx=8, pady=1
        )
        self.grip_label.pack(side=tk.LEFT)

        # 状态徽章行 2: Z 轴与储物盘槽位
        badge_row2 = tk.Frame(t_group, bg="#1e1e24")
        badge_row2.pack(fill=tk.X, pady=(0, 6))

        tk.Label(badge_row2, text="Z 轴: ", bg="#1e1e24", fg="#aaaaaa").pack(side=tk.LEFT)
        self.z_label = tk.Label(
            badge_row2, text="抬起安全位", bg="#2c3e50", fg="#5dade2",
            font=("Segoe UI", 8, "bold"), padx=6, pady=1
        )
        self.z_label.pack(side=tk.LEFT, padx=(0, 15))

        tk.Label(badge_row2, text="储物盘: ", bg="#1e1e24", fg="#aaaaaa").pack(side=tk.LEFT)
        self.storage_label = tk.Label(
            badge_row2, text="槽位 0 (0°)", bg="#145a32", fg="#58d68d",
            font=("Segoe UI", 8, "bold"), padx=6, pady=1
        )
        self.storage_label.pack(side=tk.LEFT)

        # 醒目大字当前坐标
        coord_box = tk.Frame(t_group, bg="#131318", padx=6 if self.compact else 10,
                             pady=2 if self.compact else 8,
                             highlightbackground="#2a2a38", highlightthickness=1)
        coord_box.pack(fill=tk.X, pady=4)

        x_frame = tk.Frame(coord_box, bg="#131318")
        x_frame.pack(side=tk.LEFT, expand=True)
        tk.Label(x_frame, text="当前 X 轴", bg="#131318", fg="#888899", font=("Segoe UI", 8)).pack()
        self.coord_x_label = tk.Label(
            x_frame, text="0.00 mm", bg="#131318", fg="#00ffaa",
            font=font_of(self.m, "fs_big", bold=True, mono=True)
        )
        self.coord_x_label.pack()

        sep = tk.Frame(coord_box, bg="#333344", width=1, height=24 if self.compact else 40)
        sep.pack(side=tk.LEFT, fill=tk.Y, padx=10)

        y_frame = tk.Frame(coord_box, bg="#131318")
        y_frame.pack(side=tk.LEFT, expand=True)
        tk.Label(y_frame, text="当前 Y 轴", bg="#131318", fg="#888899", font=("Segoe UI", 8)).pack()
        self.coord_y_label = tk.Label(
            y_frame, text="0.00 mm", bg="#131318", fg="#00ffaa",
            font=font_of(self.m, "fs_big", bold=True, mono=True)
        )
        self.coord_y_label.pack()

        # 任务进度与阶段
        prog_row = tk.Frame(t_group, bg="#1e1e24")
        prog_row.pack(fill=tk.X, pady=(6, 2))

        self.stage_text_label = tk.Label(
            prog_row, text="阶段: 无", bg="#1e1e24", fg="#bbbbcc", font=("Segoe UI", 8)
        )
        self.stage_text_label.pack(side=tk.LEFT)

        self.prog_pct_label = tk.Label(
            prog_row, text="0%", bg="#1e1e24", fg="#bbbbcc", font=("Segoe UI", 8, "bold")
        )
        self.prog_pct_label.pack(side=tk.RIGHT)

        self.prog_bar = ttk.Progressbar(t_group, orient=tk.HORIZONTAL, mode="determinate")
        self.prog_bar.pack(fill=tk.X, pady=(0, 6))

        # 错误报警提示栏
        self.error_box = tk.Label(
            t_group, text="系统无故障告警", bg="#1c2826", fg="#2ecc71",
            font=("Segoe UI", 8, "bold"), padx=6, pady=4, anchor="w"
        )
        self.error_box.pack(fill=tk.X, pady=(2, 4))

        # 通信链路统计
        comm_stat = tk.Frame(t_group, bg="#1e1e24")
        comm_stat.pack(fill=tk.X)
        self.comm_stat_label = tk.Label(
            comm_stat, text="TX: 0 | RX: 0 | 速率: 0.0 Hz | SEQ: 0 | ACK: 0",
            bg="#1e1e24", fg="#777788", font=("Consolas", 8)
        )
        self.comm_stat_label.pack(side=tk.LEFT)

    # --------------------------------------------------------------------------
    # 左栏下部: 2D 平台画布视口
    # --------------------------------------------------------------------------
    def _build_canvas_panel(self, parent):
        c_group = ttk.LabelFrame(parent, text=" [2] 机械臂运动平台 2D 监控 ", padding=2 if self.compact else 4)
        c_group.pack(fill=tk.BOTH, expand=True, padx=3, pady=2 if self.compact else 4)

        self.canvas_view = PlatformCanvasView(
            c_group,
            compact=self.compact,
            on_coordinate_picked=self._on_canvas_coord_picked
        )
        self.canvas_view.pack(fill=tk.BOTH, expand=True)

        btn_row = tk.Frame(c_group, bg="#1e1e24")
        btn_row.pack(fill=tk.X, pady=(4, 0))

        if not self.compact:
            tk.Label(
                btn_row, text="提示: 可直接在上方画布点击设置目标坐标",
                bg="#1e1e24", fg="#666677", font=font_of(self.m, "fs_small")
            ).pack(side=tk.LEFT)

        tk.Button(
            btn_row, text="清除轨迹", bg="#2b2b36", fg="#cccccc",
            font=font_of(self.m, "fs_small"), relief=tk.FLAT,
            command=self.canvas_view.clear_trail
        ).pack(side=tk.RIGHT)

    # --------------------------------------------------------------------------
    # 中栏: 运动与硬件单动控制
    # --------------------------------------------------------------------------
    def _build_motion_control_panel(self, parent):
        m_group = ttk.LabelFrame(parent, text=" [3] 机构单动控制与点动 ", padding=self.m["pad"])
        m_group.pack(fill=tk.X, padx=3, pady=2 if self.compact else 4)

        # 定点移动输入
        # 小屏下 4 个标签/输入框 + 按钮横排会顶出右边界 (实测 "发送移动" 落到 x=742),
        # 因此紧凑模式拆成两行: 第 1 行输入坐标, 第 2 行整行按钮。
        m = self.m
        grid_f = tk.Frame(m_group, bg="#1e1e24")
        grid_f.pack(fill=tk.X, pady=2)
        grid_f.columnconfigure(1, weight=1)
        grid_f.columnconfigure(3, weight=1)

        lbl_font = font_of(m, "fs_small")
        ent_w = 5 if self.compact else 8

        tk.Label(grid_f, text="X(mm):" if self.compact else "目标 X(mm):",
                 bg="#1e1e24", fg="#cccccc", font=lbl_font).grid(row=0, column=0, sticky="w", pady=2)
        self.target_x_entry = ttk.Entry(grid_f, width=ent_w)
        self.target_x_entry.insert(0, "40.0")
        self.target_x_entry.grid(row=0, column=1, sticky="ew", padx=3, pady=2)

        tk.Label(grid_f, text="Y(mm):" if self.compact else "目标 Y(mm):",
                 bg="#1e1e24", fg="#cccccc", font=lbl_font).grid(row=0, column=2, sticky="w", pady=2)
        self.target_y_entry = ttk.Entry(grid_f, width=ent_w)
        self.target_y_entry.insert(0, "20.0")
        self.target_y_entry.grid(row=0, column=3, sticky="ew", padx=3, pady=2)

        self.move_btn = tk.Button(
            grid_f, text="发送移动", bg="#007acc", fg="#ffffff",
            font=font_of(m, "fs_small", bold=True),
            relief=tk.FLAT, padx=8, pady=m["btn_pady"], command=self._on_click_move
        )
        if self.compact:
            self.move_btn.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(2, 0))
        else:
            self.move_btn.grid(row=0, column=4, padx=6, pady=2)

        # 快速预设工位按钮
        pre_f = tk.Frame(m_group, bg="#1e1e24")
        pre_f.pack(fill=tk.X, pady=(4, 6))

        if self.compact:
            presets = [("原点", 0.0, 0.0), ("取料1", 40.0, 20.0),
                       ("取料2", 60.0, 30.0), ("放置", 80.0, 0.0)]
        else:
            presets = [
                ("待机原点 (0,0)", 0.0, 0.0),
                ("取料点1 (40,20)", 40.0, 20.0),
                ("取料点2 (60,30)", 60.0, 30.0),
                ("放置区 (80,0)", 80.0, 0.0),
            ]
        for text, px, py in presets:
            tk.Button(
                pre_f, text=text, bg="#2b2b36", fg="#d0d0e0",
                font=font_of(self.m, "fs_small"),
                relief=tk.FLAT, padx=2, pady=self.m["btn_pady"],
                command=lambda x=px, y=py: self._apply_preset_coord(x, y)
            ).pack(side=tk.LEFT, padx=1, expand=True, fill=tk.X)

        # JOG 点动步进键盘
        jog_box = tk.LabelFrame(m_group, text="步进点动 (JOG)", bg="#1e1e24", fg="#00aaff",
                                font=font_of(self.m, "fs_small", bold=True),
                                padx=4, pady=2)
        jog_box.pack(fill=tk.X, pady=2 if self.compact else 4)

        step_row = tk.Frame(jog_box, bg="#1e1e24")
        step_row.pack(fill=tk.X, pady=2)
        tk.Label(step_row, text="步距:", bg="#1e1e24", fg="#aaaaaa",
                 font=font_of(self.m, "fs_small")).pack(side=tk.LEFT)
        self.jog_step_var = tk.DoubleVar(value=5.0)
        for s_val in [1.0, 5.0, 10.0, 20.0]:
            tk.Radiobutton(
                step_row, text=f"{int(s_val)}", value=s_val, variable=self.jog_step_var,
                bg="#1e1e24", fg="#cccccc", selectcolor="#131318", activebackground="#1e1e24",
                font=font_of(self.m, "fs_small")
            ).pack(side=tk.LEFT, padx=1)

        jfont = font_of(self.m, "fs_small")
        pad_f = tk.Frame(jog_box, bg="#1e1e24")
        pad_f.pack(fill=tk.X, pady=1)

        if self.compact:
            # 小屏: 单行 5 键 (Y+/X-/停/X+/Y-), 高度约 40px 而非 3 行网格的 128px
            for i, (txt, bg, cmd) in enumerate((
                    ("Y+", "#3a3a48", lambda: self._on_jog(0, 1)),
                    ("X-", "#3a3a48", lambda: self._on_jog(-1, 0)),
                    ("停", "#883333", self._on_click_cancel),
                    ("X+", "#3a3a48", lambda: self._on_jog(1, 0)),
                    ("Y-", "#3a3a48", lambda: self._on_jog(0, -1)))):
                pad_f.columnconfigure(i, weight=1, uniform="jog")
                tk.Button(pad_f, text=txt, bg=bg, fg="#ffffff", relief=tk.FLAT,
                          pady=1, font=jfont, command=cmd
                          ).grid(row=0, column=i, sticky="ew", padx=1)
        else:
            jw = 8
            tk.Button(pad_f, text="Y+ 上", bg="#3a3a48", fg="#ffffff", width=jw, relief=tk.FLAT,
                      font=jfont, command=lambda: self._on_jog(0, 1)).grid(row=0, column=1, pady=2)
            tk.Button(pad_f, text="X- 左", bg="#3a3a48", fg="#ffffff", width=jw, relief=tk.FLAT,
                      font=jfont, command=lambda: self._on_jog(-1, 0)).grid(row=1, column=0, padx=2)
            tk.Button(pad_f, text="停", bg="#883333", fg="#ffffff", width=4, relief=tk.FLAT,
                      font=jfont, command=self._on_click_cancel).grid(row=1, column=1, padx=2)
            tk.Button(pad_f, text="X+ 右", bg="#3a3a48", fg="#ffffff", width=jw, relief=tk.FLAT,
                      font=jfont, command=lambda: self._on_jog(1, 0)).grid(row=1, column=2, padx=2)
            tk.Button(pad_f, text="Y- 下", bg="#3a3a48", fg="#ffffff", width=jw, relief=tk.FLAT,
                      font=jfont, command=lambda: self._on_jog(0, -1)).grid(row=2, column=1, pady=2)

        # 执行机构单步动作控制 (夹爪 / Z轴 / 托盘 / 储物格分度)
        act_box = tk.LabelFrame(m_group, text="执行机构单步动作", bg="#1e1e24", fg="#00aaff",
                                font=font_of(self.m, "fs_small", bold=True), padx=4, pady=2)
        act_box.pack(fill=tk.X, pady=2 if self.compact else 4)
        lbl_w = 6 if self.compact else 8
        act_font = font_of(self.m, "fs_small", bold=True)

        # 1. 夹爪开合 (TIM1_CH2)
        grip_btn_row = tk.Frame(act_box, bg="#1e1e24")
        grip_btn_row.pack(fill=tk.X, pady=1)
        tk.Label(grip_btn_row, text="夹爪:", bg="#1e1e24", fg="#aaaaaa", font=font_of(self.m, "fs_small"), width=lbl_w, anchor="w").pack(side=tk.LEFT)
        open_btn = tk.Button(
            grip_btn_row, text="张开 (OPEN)" if not self.compact else "张开",
            bg="#2c3e50", fg="#ffffff", font=act_font,
            relief=tk.FLAT, command=self._on_click_grip_open
        )
        open_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        close_btn = tk.Button(
            grip_btn_row, text="闭合 (CLOSE)" if not self.compact else "闭合",
            bg="#2c3e50", fg="#ffffff", font=act_font,
            relief=tk.FLAT, command=self._on_click_grip_close
        )
        close_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)

        # 2. Z 轴升降 (TIM1_CH1)
        z_btn_row = tk.Frame(act_box, bg="#1e1e24")
        z_btn_row.pack(fill=tk.X, pady=1)
        tk.Label(z_btn_row, text="Z轴:", bg="#1e1e24", fg="#aaaaaa", font=font_of(self.m, "fs_small"), width=lbl_w, anchor="w").pack(side=tk.LEFT)
        z_up_btn = tk.Button(
            z_btn_row, text="抬起 (UP)" if not self.compact else "抬起",
            bg="#34495e", fg="#ffffff", font=act_font,
            relief=tk.FLAT, command=self._on_click_z_up
        )
        z_up_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        z_down_btn = tk.Button(
            z_btn_row, text="下潜 (DOWN)" if not self.compact else "下潜",
            bg="#34495e", fg="#ffffff", font=act_font,
            relief=tk.FLAT, command=self._on_click_z_down
        )
        z_down_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)

        # 3. 托盘控制 (TIM1_CH3)
        tray_btn_row = tk.Frame(act_box, bg="#1e1e24")
        tray_btn_row.pack(fill=tk.X, pady=1)
        tk.Label(tray_btn_row, text="托盘:", bg="#1e1e24", fg="#aaaaaa", font=font_of(self.m, "fs_small"), width=lbl_w, anchor="w").pack(side=tk.LEFT)
        tray_shake_btn = tk.Button(
            tray_btn_row, text="震动抖料" if self.compact else "托盘震动 / 翻转 (抖料)",
            bg="#4a235a", fg="#ffffff", font=act_font,
            relief=tk.FLAT, command=self._on_click_tray_shake
        )
        tray_shake_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tray_home_btn = tk.Button(
            tray_btn_row, text="复位" if self.compact else "托盘复位 (HOME 0°)",
            bg="#4a235a", fg="#ffffff", font=act_font,
            relief=tk.FLAT, command=self._on_click_tray_home
        )
        tray_home_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)

        # 4. 旋转储物格分度 (DJI 3508直驱，每格60°，逆时针为正)
        storage_row1 = tk.Frame(act_box, bg="#1e1e24")
        storage_row1.pack(fill=tk.X, pady=1)
        tk.Label(storage_row1, text="转位:", bg="#1e1e24", fg="#aaaaaa", font=font_of(self.m, "fs_small"), width=lbl_w, anchor="w").pack(side=tk.LEFT)
        storage_ccw_btn = tk.Button(
            storage_row1, text="逆时针 +60°" if self.compact else "逆时针 +60° (CCW 正)",
            bg="#117864", fg="#ffffff", font=act_font,
            relief=tk.FLAT, command=self._on_click_storage_ccw
        )
        storage_ccw_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        storage_cw_btn = tk.Button(
            storage_row1, text="顺时针 -60°" if self.compact else "顺时针 -60° (CW 负)",
            bg="#117864", fg="#ffffff", font=act_font,
            relief=tk.FLAT, command=self._on_click_storage_cw
        )
        storage_cw_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)

        storage_row2 = tk.Frame(act_box, bg="#1e1e24")
        storage_row2.pack(fill=tk.X, pady=1)
        tk.Label(storage_row2, text="槽位:", bg="#1e1e24", fg="#aaaaaa",
                 font=font_of(self.m, "fs_small"), width=lbl_w, anchor="w").pack(side=tk.LEFT)
        if self.compact:
            slot_vals = [f"槽位{i}" for i in range(6)]
            slot_w = 8
        else:
            slot_vals = ["槽位 0 (原点)", "槽位 1 (+60°)", "槽位 2 (+120°)",
                         "槽位 3 (180°)", "槽位 4 (+240°)", "槽位 5 (+300°)"]
            slot_w = 15
        self.storage_slot_combo = ttk.Combobox(
            storage_row2, values=slot_vals, width=slot_w, state="readonly",
            font=font_of(self.m, "fs_small")
        )
        self.storage_slot_combo.current(0)
        self.storage_slot_combo.pack(side=tk.LEFT, padx=2)

        slot_goto_btn = tk.Button(
            storage_row2, text="转到" if self.compact else "转到该槽位",
            bg="#196f3d", fg="#ffffff", font=act_font,
            relief=tk.FLAT, command=self._on_click_storage_goto_slot
        )
        slot_goto_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)

    # --------------------------------------------------------------------------
    # 中栏下部: 自动化闭环分拣测试
    # --------------------------------------------------------------------------
    def _build_workflow_panel(self, parent):
        m = self.m
        w_group = ttk.LabelFrame(parent, text=" [4] 自动化闭环分拣测试 ", padding=m["pad"])
        # 紧凑模式下这一卡在控制页底部, 用 expand 吃掉剩余高度, 保证内容不被压扁
        w_group.pack(fill=tk.BOTH, expand=True, padx=3, pady=2 if self.compact else 4)

        # 任务配置行
        t_row = tk.Frame(w_group, bg="#1e1e24")
        t_row.pack(fill=tk.X, pady=2)

        tk.Label(t_row, text="任务 ID:", bg="#1e1e24", fg="#cccccc",
                 font=font_of(m, "fs_small")).pack(side=tk.LEFT)
        self.task_id_entry = ttk.Entry(t_row, width=6 if self.compact else 8)
        self.task_id_entry.insert(0, str(self.next_task_id))
        self.task_id_entry.pack(side=tk.LEFT, padx=4)

        # 自动授权勾选
        self.auto_permit_var = tk.BooleanVar(value=True)
        auto_p_check = tk.Checkbutton(
            w_group, text="自动抓取许可" if self.compact else "到位后自动授予抓取许可 (Auto Permit)",
            variable=self.auto_permit_var,
            bg="#1e1e24", fg="#99ffaa", selectcolor="#131318", activebackground="#1e1e24",
            font=font_of(m, "fs_small", bold=True)
        )
        auto_p_check.pack(anchor="w", pady=4)

        # 手动抓取许可按钮 (在手动模式下到位高亮)
        self.permit_btn = tk.Button(
            w_group, text="手动抓取许可 (PERMIT)" if self.compact else "手动下发抓取许可 (PERMIT)",
            bg="#34495e", fg="#95a5a6", font=font_of(m, "fs_bold", bold=True),
            relief=tk.FLAT, pady=m["btn_pady"], state=tk.DISABLED,
            command=self._on_click_permit
        )
        self.permit_btn.pack(fill=tk.X, pady=2)

        # 一键测试与取消按钮行
        test_btn_row = tk.Frame(w_group, bg="#1e1e24")
        test_btn_row.pack(fill=tk.X, pady=(6, 4))

        self.start_task_btn = tk.Button(
            test_btn_row, text="开始分拣" if self.compact else "开始执行完整分拣流程",
            bg="#27ae60", fg="#ffffff", font=font_of(m, "fs_bold", bold=True),
            relief=tk.FLAT, padx=8, pady=m["btn_pady"] + 1,
            command=self._on_click_start_task
        )
        self.start_task_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4))

        self.cancel_task_btn = tk.Button(
            test_btn_row, text="取消", bg="#7f8c8d", fg="#ffffff",
            font=font_of(m, "fs_bold", bold=True), relief=tk.FLAT, padx=6,
            pady=m["btn_pady"] + 1, command=self._on_click_cancel
        )
        self.cancel_task_btn.pack(side=tk.RIGHT, padx=(4, 0))

        # 任务流程状态描述
        self.workflow_status_box = tk.Label(
            w_group, text="等待下发任务...", bg="#161b22", fg="#8b949e",
            font=font_of(m, "fs_small"), height=m["log_h"], relief=tk.GROOVE,
            anchor="w", justify=tk.LEFT, padx=4
        )
        self.workflow_status_box.pack(fill=tk.BOTH, expand=True, pady=4)

    # --------------------------------------------------------------------------
    # 右栏: 日志与原始报文监听
    # --------------------------------------------------------------------------
    def _build_log_panel(self, parent):
        m = self.m
        l_group = ttk.LabelFrame(parent, text=" [5] 通信与调试日志 (Hex / Event) ",
                                 padding=3 if self.compact else 6)
        l_group.pack(fill=tk.BOTH, expand=True, padx=3, pady=2 if self.compact else 4)

        # 顶部工具栏
        tool_row = tk.Frame(l_group, bg="#1e1e24")
        tool_row.pack(fill=tk.X, pady=(0, 4))

        self.hex_mode_var = tk.BooleanVar(value=False)
        hex_check = tk.Checkbutton(
            tool_row, text="Hex 报文" if self.compact else "显示 32B 原始 Hex 报文",
            variable=self.hex_mode_var, command=self._on_toggle_hex_mode,
            bg="#1e1e24", fg="#00ccff", selectcolor="#131318", activebackground="#1e1e24",
            font=font_of(m, "fs_small")
        )
        hex_check.pack(side=tk.LEFT)

        self.autoscroll_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            tool_row, text="自动滚屏", variable=self.autoscroll_var,
            bg="#1e1e24", fg="#aaaaaa", selectcolor="#131318", activebackground="#1e1e24",
            font=font_of(m, "fs_small")
        ).pack(side=tk.LEFT, padx=6)

        tk.Button(
            tool_row, text="清空", bg="#2b2b36", fg="#cccccc",
            font=font_of(m, "fs_small"), relief=tk.FLAT, command=self._clear_logs
        ).pack(side=tk.RIGHT, padx=1)

        tk.Button(
            tool_row, text="保存" if self.compact else "保存日志",
            bg="#2b2b36", fg="#cccccc", font=font_of(m, "fs_small"),
            relief=tk.FLAT, command=self._save_logs
        ).pack(side=tk.RIGHT, padx=1)

        # 多行文本日志框
        self.log_text = tk.Text(
            l_group,
            bg="#0f0f14",
            fg="#cccccc",
            insertbackground="#ffffff",
            font=font_of(m, "fs_small", mono=True),
            wrap=tk.WORD,
            padx=6,
            pady=4
        )
        log_scroll = ttk.Scrollbar(l_group, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)

        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 日志高亮颜色标签
        self.log_text.tag_configure("INFO", foreground="#70a1ff")
        self.log_text.tag_configure("WARN", foreground="#eccc68")
        self.log_text.tag_configure("ERROR", foreground="#ff4757")
        self.log_text.tag_configure("SUCCESS", foreground="#2ed573")
        self.log_text.tag_configure("TX", foreground="#a4b0be")
        self.log_text.tag_configure("RX", foreground="#2ed573")

        self.log("INFO", "调试上位机启动完毕，准备连接设备...")

    # --------------------------------------------------------------------------
    # 底部状态栏
    # --------------------------------------------------------------------------
    def _build_status_bar(self):
        status_bar = tk.Frame(self.root, bg="#181820", height=24, padx=8)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        self.status_bar_label = tk.Label(
            status_bar,
            text="协议: 32B (0xAA 0x55) | 就绪" if self.compact
            else "协议: 32B 定长二进制 (0xAA 0x55) | 固件接口: host_comm.c | 就绪",
            bg="#181820", fg="#666677", font=font_of(self.m, "fs_small")
        )
        self.status_bar_label.pack(side=tk.LEFT)

        version_label = tk.Label(
            status_bar, text="v1.0" if self.compact else "IntelligentSorting Host v1.0",
            bg="#181820", fg="#444455", font=font_of(self.m, "fs_small")
        )
        version_label.pack(side=tk.RIGHT)

    # ==========================================================================
    # 串口连接控制逻辑
    # ==========================================================================

    def _refresh_ports(self):
        ports = IntelligentSortingClient.list_serial_ports()
        port_names = [p[0] for p in ports]
        self.port_combobox["values"] = port_names
        if port_names and not self.port_combobox.get():
            self.port_combobox.current(0)

    def _toggle_connect(self):
        if self.client.is_connected:
            self.client.disconnect()
            self._update_conn_ui(False)
            self.log("WARN", "已断开连接")
        else:
            if self.mock_var.get():
                # 虚拟仿真模式
                try:
                    self.client.connect_mock()
                    self._update_conn_ui(True, mock=True)
                    self.log("SUCCESS", "已启动虚拟仿真模式 (Mock STM32)")
                except Exception as e:
                    messagebox.showerror("错误", f"启动虚拟仿真失败: {e}")
            else:
                port = self.port_combobox.get().strip()
                if not port:
                    messagebox.showwarning("提示", "请先选择可用串口端口！")
                    return
                baud = int(self.baud_combobox.get() or "115200")
                try:
                    self.client.connect(port, baudrate=baud)
                    self._update_conn_ui(True, mock=False, port=port)
                    self.log("SUCCESS", f"已成功连接串口 {port} (波特率 {baud})")
                except Exception as e:
                    messagebox.showerror("连接失败", f"无法打开串口 {port}:\n{e}")

    def _update_conn_ui(self, connected: bool, mock: bool = False, port: str = ""):
        if connected:
            self.connect_btn.config(text="断开连接", bg="#c0392b")
            if mock:
                self.conn_badge.config(text="● 仿真运行中 (MOCK)", bg="#7f5a00", fg="#ffdd00")
            else:
                self.conn_badge.config(text=f"● 已连接 ({port})", bg="#1b5e20", fg="#00e676")
        else:
            self.connect_btn.config(text="连接设备", bg="#28a745")
            self.conn_badge.config(text="● 未连接", bg="#444455", fg="#aaaaaa")

    # ==========================================================================
    # 动作下发槽函数
    # ==========================================================================

    def _on_click_estop(self):
        """紧急停止"""
        self.log("ERROR", ">>> 发送急停指令 (ESTOP) <<<")
        self._task_cancel_flag = True
        self.client.send_estop()

    def _on_click_clear(self):
        """清除报警与复位"""
        self.log("INFO", ">>> 发送清除报警与复位指令 (CLEAR) <<<")
        ok = self.client.send_clear()
        if not ok and self.client.is_connected:
            self.log("WARN", "清除指令未收到 ACK")

    def _on_click_cancel(self):
        """取消任务"""
        self.log("INFO", ">>> 发送取消任务指令 (CANCEL) <<<")
        self._task_cancel_flag = True
        self.client.send_cancel()

    def _on_click_grip_open(self):
        self.log("INFO", ">>> 夹爪张开 (OPEN) <<<")
        self.client.send_grip_open()

    def _on_click_grip_close(self):
        self.log("INFO", ">>> 夹爪闭合 (CLOSE) <<<")
        self.client.send_grip_close()

    def _on_click_z_up(self):
        self.log("INFO", ">>> Z 轴抬起至安全高度 (UP) <<<")
        ok = self.client.send_z_up()
        if not ok and self.client.is_connected:
            self.log("WARN", "Z 轴指令未收到 ACK")

    def _on_click_z_down(self):
        self.log("INFO", ">>> Z 轴下潜至取件高度 (DOWN) <<<")
        ok = self.client.send_z_down()
        if not ok and self.client.is_connected:
            self.log("WARN", "Z 轴指令未收到 ACK")

    def _on_click_tray_shake(self):
        self.log("INFO", ">>> 托盘震动 / 翻转 (SHAKE 抖料) <<<")
        ok = self.client.send_tray_shake()
        if not ok and self.client.is_connected:
            self.log("WARN", "托盘指令未收到 ACK")

    def _on_click_tray_home(self):
        self.log("INFO", ">>> 托盘复位至原点 (HOME 0°) <<<")
        ok = self.client.send_tray_home()
        if not ok and self.client.is_connected:
            self.log("WARN", "托盘指令未收到 ACK")

    def _on_click_storage_ccw(self):
        self.log("INFO", ">>> 储物盘逆时针旋转 60° (CCW 正向, +1格) <<<")
        ok = self.client.send_storage_step(ccw=True)
        if not ok and self.client.is_connected:
            self.log("WARN", "储物盘指令未收到 ACK")

    def _on_click_storage_cw(self):
        self.log("INFO", ">>> 储物盘顺时针旋转 60° (CW 负向, -1格) <<<")
        ok = self.client.send_storage_step(ccw=False)
        if not ok and self.client.is_connected:
            self.log("WARN", "储物盘指令未收到 ACK")

    def _on_click_storage_goto_slot(self):
        slot = self.storage_slot_combo.current()
        if slot < 0:
            slot = 0
        self.log("INFO", f">>> 储物盘定位至槽位 {slot} ({slot * 60}°) <<<")
        ok = self.client.send_storage_slot(slot)
        if not ok and self.client.is_connected:
            self.log("WARN", "储物盘指令未收到 ACK")

    def _on_click_permit(self):
        try:
            task_id = int(self.task_id_entry.get().strip() or "0")
        except ValueError:
            task_id = 0
        self.log("INFO", f">>> 手动授权抓取许可 (PERMIT), Task ID={task_id} <<<")
        self.client.send_permit(task_id)

    def _apply_preset_coord(self, x: float, y: float):
        self.target_x_entry.delete(0, tk.END)
        self.target_x_entry.insert(0, f"{x:.1f}")
        self.target_y_entry.delete(0, tk.END)
        self.target_y_entry.insert(0, f"{y:.1f}")
        self.canvas_view.set_target(x, y)

    def _on_canvas_coord_picked(self, x: float, y: float):
        self.target_x_entry.delete(0, tk.END)
        self.target_x_entry.insert(0, f"{x:.1f}")
        self.target_y_entry.delete(0, tk.END)
        self.target_y_entry.insert(0, f"{y:.1f}")
        self.log("INFO", f"画布选点: X={x:.2f}mm, Y={y:.2f}mm")

    def _on_click_move(self):
        if not self.client.is_connected:
            messagebox.showwarning("提示", "请先连接设备！")
            return
        try:
            x = float(self.target_x_entry.get().strip())
            y = float(self.target_y_entry.get().strip())
        except ValueError:
            messagebox.showerror("格式错误", "请输入合法的数值坐标！")
            return

        self.canvas_view.set_target(x, y)
        self.log("INFO", f">>> 发送移动指令: X={x:.2f}mm, Y={y:.2f}mm <<<")
        ok = self.client.send_move(task_id=0, x_mm=x, y_mm=y)
        if not ok:
            self.log("WARN", "移动指令发送未收到 ACK 响应！")

    def _on_jog(self, dir_x: int, dir_y: int):
        if not self.client.is_connected:
            return
        st = self.client.latest_status
        cur_x = st.cur_x_mm if st else 0.0
        cur_y = st.cur_y_mm if st else 0.0

        step = self.jog_step_var.get()
        new_x = cur_x + dir_x * step
        new_y = cur_y + dir_y * step

        self._apply_preset_coord(new_x, new_y)
        self._on_click_move()

    # ==========================================================================
    # 自动化任务闭环执行
    # ==========================================================================

    def _on_click_start_task(self):
        if not self.client.is_connected:
            messagebox.showwarning("提示", "请先连接设备！")
            return

        if self._task_thread and self._task_thread.is_alive():
            messagebox.showinfo("提示", "已有任务在执行中！")
            return

        try:
            target_x = float(self.target_x_entry.get().strip())
            target_y = float(self.target_y_entry.get().strip())
            task_id = int(self.task_id_entry.get().strip())
        except ValueError:
            messagebox.showerror("错误", "请输入有效的坐标与任务ID！")
            return

        self._task_cancel_flag = False
        self.start_task_btn.config(state=tk.DISABLED, bg="#555555")
        self.canvas_view.set_target(target_x, target_y)

        # 后台线程执行
        self._task_thread = threading.Thread(
            target=self._run_task_worker,
            args=(task_id, target_x, target_y, self.auto_permit_var.get()),
            name="SortingTaskWorker",
            daemon=True
        )
        self._task_thread.start()

    def _run_task_worker(self, task_id: int, x: float, y: float, auto_permit: bool):
        self.log("INFO", f"========== 开始执行分拣任务 ID: {task_id} (X={x:.2f}, Y={y:.2f}) ==========")

        def on_progress(msg: str, pct: int):
            self.msg_queue.put(("TASK_PROG", (msg, pct)))

        # 若是手动模式，当处于 WAIT_PERMIT 时等待用户点击许可
        def custom_workflow():
            if auto_permit:
                return self.client.execute_sorting_task(
                    task_id=task_id,
                    target_x_mm=x,
                    target_y_mm=y,
                    progress_callback=on_progress,
                    cancel_token=lambda: self._task_cancel_flag
                )
            else:
                # 半自动手动许可模式
                on_progress("移动前往取件点...", 20)
                ok = self.client.send_move(task_id, x, y)
                if not ok:
                    return False

                while not self._task_cancel_flag:
                    st = self.client.latest_status
                    if st:
                        if st.is_pick_ready:
                            on_progress("已到达取件点，等待人工点击【手动下发抓取许可】...", 40)
                            self.msg_queue.put(("ENABLE_PERMIT_BTN", True))
                            break
                        if st.is_alarm:
                            return False
                    time.sleep(0.05)

                if self._task_cancel_flag:
                    self.client.send_cancel(task_id)
                    return False

                # 等待阶段推进
                while not self._task_cancel_flag:
                    st = self.client.latest_status
                    if st:
                        if st.is_alarm:
                            return False
                        if st.is_task_done:
                            return st.is_success
                        on_progress(f"执行中: {st.stage_desc}...", int(st.progress))
                    time.sleep(0.05)
                return False

        try:
            success = custom_workflow()
            if success:
                self.msg_queue.put(("TASK_DONE", True))
                self.log("SUCCESS", f"========== 分拣任务 {task_id} 成功完成! ==========")
            else:
                self.msg_queue.put(("TASK_DONE", False))
                self.log("ERROR", f"========== 分拣任务 {task_id} 未能完成或已中止 ==========")
        except Exception as ex:
            self.msg_queue.put(("TASK_DONE", False))
            self.log("ERROR", f"分拣任务异常: {ex}")
        finally:
            self.msg_queue.put(("ENABLE_PERMIT_BTN", False))

    # ==========================================================================
    # 回调处理与 UI 异步队列
    # ==========================================================================

    def _on_toggle_hex_mode(self):
        self.hex_mode_enabled = bool(self.hex_mode_var.get())

    def _on_client_status(self, status: HostStatus):
        self._fps_count += 1
        self.msg_queue.put(("STATUS", status))

    def _on_client_raw_rx(self, data: bytes):
        if self.hex_mode_enabled and len(data) >= 32:
            hex_str = " ".join(f"{b:02X}" for b in data[:32])
            self.msg_queue.put(("RAW_HEX_RX", hex_str))

    def _on_client_raw_tx(self, data: bytes):
        if self.hex_mode_enabled and len(data) >= 32:
            hex_str = " ".join(f"{b:02X}" for b in data[:32])
            self.msg_queue.put(("RAW_HEX_TX", hex_str))

    def _periodic_ui_update(self):
        """主线程定时拉取队列更新 UI (30ms)"""
        # 计算每秒帧率 (FPS)
        now = time.time()
        if now - self._fps_timer >= 1.0:
            self.current_fps = self._fps_count / (now - self._fps_timer)
            self._fps_count = 0
            self._fps_timer = now

        # 保底自动实时遥测轮询 (若已连接且非仿真，每 100ms 无阻塞拉取一次最新状态)
        if self.client.is_connected and not self.client.is_mock:
            if now - getattr(self, "_last_poll_time", 0.0) >= 0.1:
                self._last_poll_time = now
                try:
                    self.client.send_poll(wait_ack=False)
                except Exception:
                    pass

        while not self.msg_queue.empty():
            try:
                m_type, val = self.msg_queue.get_nowait()
            except queue.Empty:
                break

            try:
                if m_type == "STATUS":
                    self._render_status(val)
                elif m_type == "TASK_PROG":
                    msg, pct = val
                    self.workflow_status_box.config(text=f"[{pct}%] {msg}", fg="#00e676" if pct == 100 else "#88ccff")
                    self.prog_bar["value"] = pct
                    self.prog_pct_label.config(text=f"{pct}%")
                elif m_type == "TASK_DONE":
                    self.start_task_btn.config(state=tk.NORMAL, bg="#27ae60")
                    if val:
                        # 自动递增下一个任务号
                        self.next_task_id += 1
                        self.task_id_entry.delete(0, tk.END)
                        self.task_id_entry.insert(0, str(self.next_task_id))
                elif m_type == "ENABLE_PERMIT_BTN":
                    if val:
                        self.permit_btn.config(state=tk.NORMAL, bg="#27ae60", fg="#ffffff")
                    else:
                        self.permit_btn.config(state=tk.DISABLED, bg="#34495e", fg="#95a5a6")
                elif m_type == "RAW_HEX_RX":
                    self.log("RX", f"RX: {val}")
                elif m_type == "RAW_HEX_TX":
                    self.log("TX", f"TX: {val}")
            except Exception as e:
                pass

        self.root.after(30, self._periodic_ui_update)

    def _render_status(self, st: HostStatus):
        # 1. 坐标与画布
        self.coord_x_label.config(text=f"{st.cur_x_mm:+.2f} mm")
        self.coord_y_label.config(text=f"{st.cur_y_mm:+.2f} mm")
        self.canvas_view.update_position(st.cur_x_mm, st.cur_y_mm)

        # 2. 主状态徽章
        state_colors = {
            SysState.IDLE: ("#2d5a27", "#55ff55"),
            SysState.BUSY: ("#1c2833", "#5dade2"),
            SysState.RUNNING: ("#1a3d6b", "#4db8ff"),
            SysState.PAUSED: ("#7d6608", "#f4d03f"),
            SysState.FAULT: ("#6e3b00", "#ffaa00"),
            SysState.ESTOP: ("#8b0000", "#ff5555"),
            SysState.TASK_DONE: ("#4a148c", "#ea80fc"),
        }
        bg_col, fg_col = state_colors.get(st.state, ("#333333", "#ffffff"))
        self.state_label.config(text=st.state_desc, bg=bg_col, fg=fg_col)

        # 3. 夹爪徽章
        grip_colors = {
            GripperState.OPEN: ("#1b4f72", "#5dade2"),
            GripperState.CLOSED: ("#512e5f", "#bb8fce"),
            GripperState.MOVING: ("#b7950b", "#f7dc6f"),
            GripperState.HELD: ("#1e8449", "#58d68d"),
            GripperState.OPENING: ("#2e4053", "#85929e"),
            GripperState.EMPTY: ("#78281f", "#ec7063"),
            GripperState.SLIP: ("#922b21", "#f1948a"),
            GripperState.FAULT: ("#78281f", "#ec7063"),
            GripperState.UNKNOWN: ("#334455", "#88ccff"),
        }
        gbg, gfg = grip_colors.get(st.gripper, ("#334455", "#88ccff"))
        self.grip_label.config(text=st.gripper_desc, bg=gbg, fg=gfg)

        # 3.1 Z轴与储物盘遥测状态
        z_colors = {
            ZState.UP: ("#2c3e50", "#5dade2"),
            ZState.DOWN: ("#512e5f", "#bb8fce"),
            ZState.MOVING: ("#b7950b", "#f7dc6f"),
        }
        z_bg, z_fg = z_colors.get(st.z_state, ("#334455", "#88ccff"))
        self.z_label.config(text=st.z_desc, bg=z_bg, fg=z_fg)
        self.storage_label.config(text=f"槽位 {st.storage_slot} ({st.storage_slot * 60}°)")

        # 4. 进度与阶段
        if not (self._task_thread and self._task_thread.is_alive()):
            self.prog_bar["value"] = st.progress
            self.prog_pct_label.config(text=f"{st.progress}%")
            self.stage_text_label.config(text=f"阶段: {st.stage_desc}")

        # 5. 错误告警栏
        if st.error != 0:
            self.error_box.config(
                text=f"[报警 0x{st.error:04X}] {st.error_desc}",
                bg="#5c1d1d", fg="#ff7675"
            )
        elif st.is_estop:
            self.error_box.config(
                text="[急停生效中] 已拒绝运动指令！请排除险情后点击【清除报警/复位】",
                bg="#5c1d1d", fg="#ff7675"
            )
        else:
            self.error_box.config(
                text="系统无故障告警 (OK)",
                bg="#1c2826", fg="#2ecc71"
            )

        # 6. 通信统计
        tx = self.client.stats["tx_frames"]
        rx = self.client.stats["rx_frames"]
        self.comm_stat_label.config(
            text=f"TX: {tx} | RX: {rx} | 速率: {self.current_fps:.1f} Hz | SEQ: {st.seq} | ACK: {st.ack_seq}"
        )

    # ==========================================================================
    # 日志输出与辅助
    # ==========================================================================

    def log(self, tag: str, message: str):
        now_str = datetime.now().strftime("%H:%M:%S.%f")[:12]
        line = f"[{now_str}] [{tag:<4}] {message}\n"
        self.log_text.insert(tk.END, line, tag)
        if self.autoscroll_var.get():
            self.log_text.see(tk.END)

    def _clear_logs(self):
        self.log_text.delete("1.0", tk.END)

    def _save_logs(self):
        file_path = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if file_path:
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(self.log_text.get("1.0", tk.END))
                messagebox.showinfo("成功", f"日志已成功保存到:\n{file_path}")
            except Exception as e:
                messagebox.showerror("错误", f"保存日志失败: {e}")

    def _on_close(self):
        self.client.disconnect()
        self.root.destroy()


def run_debug_gui(backend: str = None):
    """启动调试器主界面"""
    root = tk.Tk()
    app = SortingDebugApp(root, backend=backend)
    root.mainloop()


if __name__ == "__main__":
    run_debug_gui()
