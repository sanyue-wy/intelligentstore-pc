"""
STM32 智能分拣系统 - 程序监控模块独立运行与健康诊断工具
功能:
- 监控各子模块心跳在线状态与看门狗超时警报 (Watchdog)
- 主机 PC 性能监控 (CPU / 内存占用率 / 运行线程数)
- 活跃告警与历史告警日志查询及模拟故障注入
"""

from __future__ import annotations
import sys
import os
import time
import tkinter as tk
from tkinter import ttk, messagebox

CUR_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(os.path.dirname(CUR_DIR))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from MessegeCenter import MessageBus, get_message_bus, ModuleHeartbeat
from moniter import MonitorService, AlertManager, HealthWatchdog, create_monitor_service
from debug.common.theme import apply_dark_theme
from debug.common.responsive import (
    apply_window, is_compact, screen_size, ui_metrics, font_of,
)


class MonitorStandaloneDebugger:
    """监控模块独立调试诊断器"""

    def __init__(self, root: tk.Tk, bus: MessageBus = None, backend: str = None):
        self.root = root
        self.root.title("程序监控与健康诊断工具 - [Monitor Standalone Dashboard]")

        # 屏幕自适应: 物理屏 800x480, 写死 1000x680 会被裁切
        self.sw, self.sh = screen_size(self.root)
        self.compact = is_compact(self.root)
        self.m = ui_metrics(self.compact)
        apply_window(self.root, ideal=(1000, 680), min_size=(680, 420))
        self.root.configure(bg="#1e1e24")

        self.bus = bus or get_message_bus()
        # 经后端注册表获取, 不绑定具体实现
        self.service = create_monitor_service(self.bus, backend=backend)
        self.service.start(interval_s=0.5)

        self._setup_style()
        self._build_layout()

        # 刷新定时器
        self.root.after(200, self._periodic_refresh)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_style(self):
        apply_dark_theme(self.root, metrics=self.m)

    def _build_layout(self):
        # 1. 顶部主机资源遥测卡片
        m = self.m
        sys_f = tk.LabelFrame(self.root, text=" 主机性能与运行指标 ", bg="#1e1e24",
                              fg="#00aaff", font=font_of(m, "fs_bold", bold=True),
                              padx=4 if self.compact else 10, pady=2 if self.compact else 8)
        sys_f.pack(fill=tk.X, padx=3 if self.compact else 8, pady=2 if self.compact else 6)

        # 紧凑模式: 2 行摆放, 避免 CPU/内存/线程三项 (合计 ~640px) 连排溢出
        c_row = tk.Frame(sys_f, bg="#1e1e24")
        c_row.pack(fill=tk.X)
        bar_len = 70 if self.compact else 120
        lbl_font = font_of(m, "fs_small")
        val_font = font_of(m, "fs_small", bold=True)

        # CPU
        tk.Label(c_row, text="CPU:", bg="#1e1e24", fg="#bbbbcc", font=lbl_font).pack(side=tk.LEFT, padx=(0, 2))
        self.cpu_bar = ttk.Progressbar(c_row, length=bar_len, mode="determinate")
        self.cpu_bar.pack(side=tk.LEFT, padx=(0, 3))
        self.cpu_label = tk.Label(c_row, text="0.0%", bg="#1e1e24", fg="#00ffaa", font=val_font, width=5)
        self.cpu_label.pack(side=tk.LEFT, padx=(0, 8))

        # 内存
        tk.Label(c_row, text="内存:", bg="#1e1e24", fg="#bbbbcc", font=lbl_font).pack(side=tk.LEFT, padx=(0, 2))
        self.mem_bar = ttk.Progressbar(c_row, length=bar_len, mode="determinate")
        self.mem_bar.pack(side=tk.LEFT, padx=(0, 3))
        self.mem_label = tk.Label(c_row, text="0.0%", bg="#1e1e24", fg="#00ffaa", font=val_font, width=5)
        self.mem_label.pack(side=tk.LEFT, padx=(0, 8))

        # 线程数 (小屏时换行显示, 用 pack 的 fill 保证不被裁)
        self.threads_label = tk.Label(c_row, text="线程数: 0", bg="#1e1e24", fg="#cccccc",
                                      font=lbl_font, anchor="w")
        self.threads_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 2. 中部: 各模块在线状态与心跳
        hb_f = tk.LabelFrame(
            self.root, text=" 各子模块健康状态与看门狗 (Heartbeat Watchdog) ", bg="#1e1e24",
            fg="#00aaff", font=font_of(m, "fs_bold", bold=True),
            padx=3 if self.compact else 8, pady=2 if self.compact else 6)
        hb_f.pack(fill=tk.BOTH, expand=True, padx=3 if self.compact else 8,
                  pady=2 if self.compact else 4)

        columns = ("name", "status", "fps", "elapsed", "details")
        self.tree_modules = ttk.Treeview(hb_f, columns=columns, show="headings", height=1)
        self.tree_modules.heading("name", text="子模块名称")
        self.tree_modules.heading("status", text="在线状态")
        self.tree_modules.heading("fps", text="运行速率 (FPS/Hz)")
        self.tree_modules.heading("elapsed", text="距上次心跳 (秒)")
        self.tree_modules.heading("details", text="内部运行参数")

        # 列宽按屏幕宽度等比缩放 (原来合计 890px, 800px 屏幕上会被裁掉最后一列)
        k = (self.sw - 20) / 890.0 if self.compact else 1.0
        for col, base in (("name", 140), ("status", 110), ("fps", 120),
                          ("elapsed", 120), ("details", 400)):
            self.tree_modules.column(col, width=max(40, int(base * k)),
                                     anchor="center" if col != "details" else "w",
                                     stretch=True)
        self.tree_modules.pack(fill=tk.BOTH, expand=True)

        # 3. 底部: 活跃告警与历史告警日志
        alert_f = tk.LabelFrame(
            self.root, text=" 监控告警中心与故障记录 ", bg="#1e1e24", fg="#ff4444",
            font=font_of(m, "fs_bold", bold=True),
            padx=3 if self.compact else 8, pady=2 if self.compact else 6)
        alert_f.pack(fill=tk.BOTH, expand=True, padx=3 if self.compact else 8,
                     pady=2 if self.compact else 6)

        btn_row = tk.Frame(alert_f, bg="#1e1e24")
        btn_row.pack(fill=tk.X, pady=(0, 4))

        # 清除按钮先 pack 到右侧, 保证小屏也完整可见
        tk.Button(btn_row, text="清除告警" if self.compact else "清除所有在位告警",
                  bg="#7f2020", fg="#ffffff", relief=tk.FLAT, padx=m["btn_padx"],
                  pady=m["btn_pady"], font=font_of(m, "fs_small"),
                  command=self.service.clear_all_alerts).pack(side=tk.RIGHT)

        if not self.compact:
            tk.Label(btn_row, text="模拟测试按键:", bg="#1e1e24", fg="#888899").pack(side=tk.LEFT)
        tk.Button(btn_row, text="视觉心跳" if self.compact else "模拟视觉模块心跳",
                  bg="#2b2b36", fg="#d0d0e0", relief=tk.FLAT, padx=3, pady=m["btn_pady"],
                  font=font_of(m, "fs_small"),
                  command=self._sim_vision_heartbeat).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_row, text="通讯心跳" if self.compact else "模拟通讯模块心跳",
                  bg="#2b2b36", fg="#d0d0e0", relief=tk.FLAT, padx=3, pady=m["btn_pady"],
                  font=font_of(m, "fs_small"),
                  command=self._sim_comm_heartbeat).pack(side=tk.LEFT, padx=2)

        alert_cols = ("time", "level", "source", "code", "msg")
        self.tree_alerts = ttk.Treeview(alert_f, columns=alert_cols, show="headings", height=1)
        self.tree_alerts.heading("time", text="发生时间")
        self.tree_alerts.heading("level", text="告警级别")
        self.tree_alerts.heading("source", text="来源模块")
        self.tree_alerts.heading("code", text="错误码")
        self.tree_alerts.heading("msg", text="告警详细描述")

        k2 = (self.sw - 20) / 880.0 if self.compact else 1.0
        for col, base, anchor in (("time", 100, "center"), ("level", 90, "center"),
                                  ("source", 110, "center"), ("code", 80, "center"),
                                  ("msg", 500, "w")):
            self.tree_alerts.column(col, width=max(40, int(base * k2)), anchor=anchor,
                                    stretch=True)
        self.tree_alerts.pack(fill=tk.BOTH, expand=True)

    def _sim_vision_heartbeat(self):
        self.bus.publish("monitor/heartbeat", ModuleHeartbeat("vision", status="OK", fps=30.0, details={"source": "manual_sim"}))

    def _sim_comm_heartbeat(self):
        self.bus.publish("monitor/heartbeat", ModuleHeartbeat("communication", status="OK", fps=20.0, details={"source": "manual_sim"}))

    def _periodic_refresh(self):
        # 1. 更新主机指标
        metrics = self.service.get_system_metrics()
        cpu = metrics["cpu_percent"]
        mem = metrics["memory_percent"]
        self.cpu_bar["value"] = cpu
        self.cpu_label.config(text=f"{cpu:.1f}%")
        self.mem_bar["value"] = mem
        self.mem_label.config(text=f"{mem:.1f}%")
        if self.compact:
            self.threads_label.config(text=f"线程 {metrics['threads_count']} | 内存 {metrics['memory_used_mb']:.0f}MB")
        else:
            self.threads_label.config(
                text=f"进程线程数: {metrics['threads_count']} (内存使用: {metrics['memory_used_mb']} MB)")

        # 2. 更新模块状态表
        module_snapshots = self.service.check_all_modules()
        for item in self.tree_modules.get_children():
            self.tree_modules.delete(item)

        for name, rec in module_snapshots.items():
            elapsed_str = f"{rec.elapsed_since_last:.1f}s" if rec.last_heartbeat_time > 0 else "未上报"
            st_text = rec.status
            if st_text == "OK":
                st_text = "● 正常 (OK)"
            elif st_text == "OFFLINE":
                st_text = "○ 离线 (OFFLINE)"

            self.tree_modules.insert("", tk.END, values=(
                name, st_text, f"{rec.fps:.1f}", elapsed_str, str(rec.details)
            ))

        # 3. 更新告警表
        alerts = self.service.get_active_alerts()
        for item in self.tree_alerts.get_children():
            self.tree_alerts.delete(item)

        for a in alerts:
            t_str = time.strftime("%H:%M:%S", time.localtime(a.timestamp))
            self.tree_alerts.insert("", tk.END, values=(
                t_str, a.level, a.source, a.code, a.message
            ))

        self.root.after(300, self._periodic_refresh)

    def _on_close(self):
        self.service.stop()
        self.root.destroy()


def run_monitor_standalone(backend: str = None):
    root = tk.Tk()
    app = MonitorStandaloneDebugger(root, backend=backend)
    root.mainloop()


if __name__ == "__main__":
    run_monitor_standalone()
