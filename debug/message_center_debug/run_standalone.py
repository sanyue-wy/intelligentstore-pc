"""
STM32 智能分拣系统 - 消息中心独立调试与事件注入工具
功能:
- 实时监听消息总线上的所有主题报文 (Pub/Sub Stream)
- 按主题模式过滤消息 (如 'comm/*', 'vision/*', 'monitor/*')
- 手动消息注入/模拟发布: 允许工程师向任意主题注入自定义数据 (测试各模块响应)
- 消息吞吐率与主题发布频次统计
"""

from __future__ import annotations
import sys
import os
import json
import time
import tkinter as tk
from tkinter import ttk, messagebox

CUR_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(os.path.dirname(CUR_DIR))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from MessegeCenter import (
    MessageBus, get_message_bus, Message, create_message_bus,
    TOPIC_COMM_STATUS, TOPIC_COMM_CMD_SEND, TOPIC_COMM_ESTOP,
    TOPIC_COMM_CLEAR, TOPIC_VISION_TARGET, TOPIC_TASK_START,
    TOPIC_TASK_CANCEL, TOPIC_MONITOR_ALERT, VisionTarget
)
from debug.common.theme import apply_dark_theme
from debug.common.responsive import (
    apply_window, is_compact, screen_size, ui_metrics, font_of,
)


class MessageCenterDebugger:
    """消息中心独立调试监控器"""

    def __init__(self, root: tk.Tk, bus: MessageBus = None, backend: str = None):
        self.root = root
        self.root.title("消息中心独立调试工具 - [MessageCenter Inspector & Injector]")

        # 屏幕自适应: 物理屏 800x480, 写死 1000x680 会被裁切
        self.sw, self.sh = screen_size(self.root)
        self.compact = is_compact(self.root)
        self.m = ui_metrics(self.compact)
        apply_window(self.root, ideal=(1000, 680), min_size=(680, 420))
        self.root.configure(bg="#1e1e24")

        # 经后端注册表获取, 不绑定具体实现
        self.bus = bus or create_message_bus(backend=backend)
        self._setup_style()
        self._build_layout()

        # 订阅全局所有主题
        self.bus.subscribe("*", self._on_message_received)
        self._msg_buffer = []

        # 刷新定时器
        self.root.after(100, self._periodic_refresh)

    def _setup_style(self):
        apply_dark_theme(self.root, metrics=self.m)

    def _build_layout(self):
        m = self.m
        # 顶部统计与过滤栏 (统计先 pack 到右侧, 保证小屏完整可见)
        top_bar = tk.Frame(self.root, bg="#262630", padx=m["bar_padx"], pady=m["bar_pady"])
        top_bar.pack(fill=tk.X, side=tk.TOP)

        self.stat_label = tk.Label(top_bar, text="已发布: 0 条", bg="#262630", fg="#00ffaa",
                                   font=font_of(m, "fs_bold", bold=True))
        self.stat_label.pack(side=tk.RIGHT, padx=2)

        tk.Label(top_bar, text="主题过滤:", bg="#262630", fg="#cccccc",
                 font=font_of(m, "fs_small")).pack(side=tk.LEFT, padx=(0, 3))
        self.filter_entry = ttk.Entry(top_bar, width=10 if self.compact else 18,
                                      font=font_of(m, "fs_small"))
        self.filter_entry.insert(0, "*")
        self.filter_entry.pack(side=tk.LEFT, padx=(0, 4))

        tk.Button(top_bar, text="清空", bg="#3a3a48", fg="#ffffff", relief=tk.FLAT,
                  padx=m["btn_padx"], pady=m["btn_pady"], font=font_of(m, "fs_small"),
                  command=self._clear_tree).pack(side=tk.LEFT)

        # 中部: 实时报文表格
        mid_f = tk.Frame(self.root, bg="#1e1e24")
        mid_f.pack(fill=tk.BOTH, expand=True, padx=3 if self.compact else 8,
                   pady=2 if self.compact else 6)

        # 列宽按屏幕宽度缩放: 原来合计 930px, 在 800px 屏幕上必然出现横向裁切
        columns = ("id", "time", "sender", "topic", "payload")
        self.tree = ttk.Treeview(mid_f, columns=columns, show="headings",
                                 selectmode="browse", height=1)
        for col, title, base_w, anchor in (
                ("id", "序号", 60, "center"),
                ("time", "时间戳", 100, "center"),
                ("sender", "来源模块", 110, "center"),
                ("topic", "消息主题 (Topic)", 180, "w"),
                ("payload", "消息载荷内容 (Payload)", 480, "w")):
            self.tree.heading(col, text=title)
            w = int(base_w * self.sw / 1000.0) if self.compact else base_w
            self.tree.column(col, width=max(34, w), anchor=anchor,
                             stretch=(col in ("topic", "payload")))

        tree_scroll = ttk.Scrollbar(mid_f, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(fill=tk.BOTH, expand=True)

        # 底部: 手动消息注入区 (Injector)
        inject_box = tk.LabelFrame(
            self.root, text=" 手动消息注入器 (Message Injector) ", bg="#1e1e24",
            fg="#00aaff", font=font_of(m, "fs_bold", bold=True),
            padx=4 if self.compact else 10, pady=2 if self.compact else 8)
        inject_box.pack(fill=tk.X, padx=3 if self.compact else 8,
                        pady=(0, 3 if self.compact else 8))

        row1 = tk.Frame(inject_box, bg="#1e1e24")
        row1.pack(fill=tk.X, pady=2)

        if not self.compact:
            tk.Label(row1, text="目标主题:", bg="#1e1e24", fg="#bbbbcc").pack(side=tk.LEFT, padx=2)
        self.topic_combo = ttk.Combobox(row1, width=13 if self.compact else 24, values=[
            TOPIC_TASK_START, TOPIC_COMM_ESTOP, TOPIC_COMM_CLEAR,
            TOPIC_VISION_TARGET, TOPIC_TASK_CANCEL, "custom/test"
        ], font=font_of(m, "fs_small"))
        self.topic_combo.set(TOPIC_TASK_START)
        self.topic_combo.pack(side=tk.LEFT, padx=3)

        # 预设按钮: 小屏用短文案, 4 个按钮挤在一行也不会溢出
        if self.compact:
            presets = [
                ("启动任务", TOPIC_TASK_START, '{"task_id": 105, "x_mm": 40.0, "y_mm": 20.0}'),
                ("急停", TOPIC_COMM_ESTOP, '{"reason": "manual_estop"}'),
                ("清报警", TOPIC_COMM_CLEAR, '{}'),
                ("工件目标", TOPIC_VISION_TARGET, '{"target_id": 1, "color": "red", "x_mm": 45.0, "y_mm": 25.0}'),
            ]
        else:
            tk.Label(row1, text="快捷预设:", bg="#1e1e24", fg="#bbbbcc").pack(side=tk.LEFT, padx=(10, 2))
            presets = [
                ("启动分拣任务", TOPIC_TASK_START, '{"task_id": 105, "x_mm": 40.0, "y_mm": 20.0}'),
                ("模拟急停", TOPIC_COMM_ESTOP, '{"reason": "manual_estop"}'),
                ("模拟清除报警", TOPIC_COMM_CLEAR, '{}'),
                ("模拟工件目标", TOPIC_VISION_TARGET, '{"target_id": 1, "color": "red", "x_mm": 45.0, "y_mm": 25.0}'),
            ]
        for name, p_top, p_data in presets:
            tk.Button(row1, text=name, bg="#2b2b36", fg="#d0d0e0",
                      font=font_of(m, "fs_small"), relief=tk.FLAT, padx=3,
                      command=lambda t=p_top, d=p_data: self._apply_preset(t, d)
                      ).pack(side=tk.LEFT, padx=1, expand=True, fill=tk.X)

        row2 = tk.Frame(inject_box, bg="#1e1e24")
        row2.pack(fill=tk.X, pady=(6, 2))

        tk.Label(row2, text="内容:" if self.compact else "消息载荷 (JSON/文本):",
                 bg="#1e1e24", fg="#bbbbcc", font=font_of(m, "fs_small")).pack(side=tk.LEFT, padx=2)
        self.payload_entry = ttk.Entry(row2, font=font_of(m, "fs_small"))
        self.payload_entry.insert(0, '{"task_id": 101, "x_mm": 40.0, "y_mm": 20.0}')
        self.payload_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        tk.Button(row2, text="发布" if self.compact else "发布 / 注入消息",
                  bg="#007acc", fg="#ffffff", font=font_of(m, "fs_bold", bold=True),
                  relief=tk.FLAT, padx=m["btn_padx"], pady=m["btn_pady"],
                  command=self._on_inject_message).pack(side=tk.RIGHT)

    def _apply_preset(self, topic: str, data_str: str):
        self.topic_combo.set(topic)
        self.payload_entry.delete(0, tk.END)
        self.payload_entry.insert(0, data_str)

    def _on_inject_message(self):
        topic = self.topic_combo.get().strip()
        if not topic:
            return
        raw_text = self.payload_entry.get().strip()
        try:
            payload = json.loads(raw_text)
        except Exception:
            payload = raw_text

        self.bus.publish(topic, payload, sender="injector")

    def _on_message_received(self, msg: Message):
        self._msg_buffer.append(msg)

    def _periodic_refresh(self):
        import fnmatch
        pattern = self.filter_entry.get().strip() or "*"

        if self._msg_buffer:
            to_insert = list(self._msg_buffer)
            self._msg_buffer.clear()

            for msg in to_insert:
                if pattern == "*" or fnmatch.fnmatch(msg.topic, pattern):
                    payload_summary = str(msg.payload)
                    if len(payload_summary) > 120:
                        payload_summary = payload_summary[:117] + "..."
                    self.tree.insert("", 0, values=(
                        msg.msg_id, msg.time_str, msg.sender, msg.topic, payload_summary
                    ))

            # 保持列表不超过 300 行
            children = self.tree.get_children()
            if len(children) > 300:
                for c in children[300:]:
                    self.tree.delete(c)

        stats = self.bus.get_stats()
        if self.compact:
            self.stat_label.config(text=f"发布 {stats['total_published']} | 分发 {stats['total_delivered']}")
        else:
            self.stat_label.config(text=f"总发布消息: {stats['total_published']} 条 | 分发数: {stats['total_delivered']}")
        self.root.after(100, self._periodic_refresh)

    def _clear_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)


def run_message_center_standalone(backend: str = None):
    root = tk.Tk()
    app = MessageCenterDebugger(root, backend=backend)
    root.mainloop()


if __name__ == "__main__":
    run_message_center_standalone()
