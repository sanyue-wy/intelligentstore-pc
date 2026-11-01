"""
STM32 智能分拣系统 - 消息路由器与业务协调器 (Message Router & Coordinator)
在消息总线之上建立各物理模块 (Communication / Vision / Monitor) 之间的解耦桥梁。

接口契约见 MessegeCenter/interfaces.py 的 MessageRouterBase。
"""

from __future__ import annotations
import threading
import time
from typing import Optional, Dict, Any, Callable
from .bus import MessageBus, get_message_bus
from .interfaces import MessageRouterBase
from .topics import (
    TOPIC_COMM_STATUS, TOPIC_COMM_CMD_SEND, TOPIC_COMM_ESTOP,
    TOPIC_COMM_CLEAR, TOPIC_COMM_CONNECTION, TOPIC_VISION_TARGET,
    TOPIC_TASK_START, TOPIC_TASK_PROGRESS, TOPIC_TASK_PERMIT,
    TOPIC_TASK_DONE, TOPIC_TASK_CANCEL, TOPIC_SYS_LOG,
    VisionTarget, Message
)
from communication import IntelligentSortingClient, HostStatus, HostCmd


class MessageRouter(MessageRouterBase):
    """
    系统全局业务协调器与事件路由器
    """

    def __init__(self, bus: Optional[MessageBus] = None):
        super().__init__(bus=bus or get_message_bus())
        self.bus = bus or get_message_bus()
        self.comm_client: Optional[IntelligentSortingClient] = None
        self._running = False
        self._lock = threading.Lock()

        # 当前执行中的任务线程
        self._active_task_thread: Optional[threading.Thread] = None
        self._task_cancel_flag = False

    def attach_communication(self, client: IntelligentSortingClient):
        """挂载下位机通讯客户端并打通总线双向消息"""
        self.comm_client = client

        # 1. 订阅下位机状态事件，转发展开至消息总线
        def on_comm_status(st: HostStatus):
            self.bus.publish(TOPIC_COMM_STATUS, st, sender="communication")

        # 2. 原始日志与报警桥接
        def on_comm_alarm(st: HostStatus):
            self.bus.publish(
                TOPIC_SYS_LOG,
                {"level": "WARN", "msg": f"下位机告警: {st.error_desc}"},
                sender="communication"
            )

        client.on_status_callbacks.append(on_comm_status)
        client.on_alarm_callbacks.append(on_comm_alarm)

        # 3. 订阅总线命令下发请求，转调下位机 API
        self.bus.subscribe(TOPIC_COMM_CMD_SEND, self._handle_comm_cmd)
        self.bus.subscribe(TOPIC_COMM_ESTOP, lambda m: self.comm_client.send_estop())
        self.bus.subscribe(TOPIC_COMM_CLEAR, lambda m: self.comm_client.send_clear())
        self.bus.subscribe(TOPIC_TASK_START, self._handle_task_start)
        self.bus.subscribe(TOPIC_TASK_CANCEL, self._handle_task_cancel)
        self.bus.subscribe(TOPIC_TASK_PERMIT, self._handle_task_permit)

    def _handle_comm_cmd(self, msg: Message):
        if not self.comm_client or not self.comm_client.is_connected:
            return
        payload = msg.payload
        if isinstance(payload, HostCmd):
            self.comm_client.send_cmd(
                flags=payload.flags,
                task_id=payload.task_id,
                x_mm=payload.target_x_mm,
                y_mm=payload.target_y_mm,
                grip_cmd=payload.grip_cmd,
                timeout_ms=payload.timeout_ms
            )

    def _handle_task_permit(self, msg: Message):
        task_id = int(msg.payload if isinstance(msg.payload, (int, str)) else 0)
        if self.comm_client:
            self.comm_client.send_permit(task_id)

    def _handle_task_cancel(self, msg: Message):
        self._task_cancel_flag = True
        if self.comm_client:
            self.comm_client.send_cancel()

    def _handle_task_start(self, msg: Message):
        """收到启动分拣任务请求，开启任务状态机闭环"""
        if self._active_task_thread and self._active_task_thread.is_alive():
            self.bus.publish(
                TOPIC_SYS_LOG,
                {"level": "WARN", "msg": "已有分拣任务在执行中，拒绝重复启动"},
                sender="router"
            )
            return

        payload = msg.payload
        # payload 可为 VisionTarget 对象或字典 {"task_id": 101, "x_mm": 40.0, "y_mm": 20.0}
        if isinstance(payload, VisionTarget):
            task_id = payload.target_id
            x_mm = payload.x_mm
            y_mm = payload.y_mm
        elif isinstance(payload, dict):
            task_id = payload.get("task_id", 101)
            x_mm = payload.get("x_mm", 40.0)
            y_mm = payload.get("y_mm", 20.0)
        else:
            return

        self._task_cancel_flag = False
        self._active_task_thread = threading.Thread(
            target=self._run_sorting_workflow,
            args=(task_id, x_mm, y_mm),
            name="RouterWorkflowThread",
            daemon=True
        )
        self._active_task_thread.start()

    def _run_sorting_workflow(self, task_id: int, x_mm: float, y_mm: float):
        if not self.comm_client or not self.comm_client.is_connected:
            self.bus.publish(
                TOPIC_TASK_DONE,
                {"task_id": task_id, "success": False, "reason": "下位机未连接"},
                sender="router"
            )
            return

        def on_prog(stage_str: str, pct: int):
            self.bus.publish(
                TOPIC_TASK_PROGRESS,
                {"task_id": task_id, "stage": stage_str, "progress": pct},
                sender="router"
            )

        success = self.comm_client.execute_sorting_task(
            task_id=task_id,
            target_x_mm=x_mm,
            target_y_mm=y_mm,
            timeout_s=25.0,
            progress_callback=on_prog,
            cancel_token=lambda: self._task_cancel_flag
        )

        self.bus.publish(
            TOPIC_TASK_DONE,
            {"task_id": task_id, "success": success},
            sender="router"
        )
