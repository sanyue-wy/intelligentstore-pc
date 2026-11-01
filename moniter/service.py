"""
STM32 智能分拣系统 - 程序监控后台服务 (Monitor Service)
定期巡检各模块心跳、主机性能指标并上报至消息中心。

接口契约见 moniter/interfaces.py 的 MonitorServiceBase。
总线约定 (订阅心跳 / 发布告警、指标、自身心跳) 见该文件模块文档。

------------------------------------------------------------------------------
本版纠正
------------------------------------------------------------------------------
1. 离线告警状态机重写 (原实现每条超时模块每个巡检周期都 raise_alert)
   旧逻辑: 只要 status == OFFLINE 就 raise_alert -> 每秒一条新告警对象,
   告警历史 60 秒就被同一模块刷满 200 条, 界面告警面板变成滚屏噪声,
   真正的首条告警被冲掉。现在:
     * 每个模块只在 "刚越过超时线" 那一刻告警一次 (边沿触发);
     * 持续离线期间不重复告警, 只刷新在位告警的时间戳;
     * 心跳恢复时 resolve_alert 并在 TOPIC_SYS_LOG 记一条恢复日志。

2. 巡检线程 daemon + 可退出 + 不可重入
   旧 stop() 只置位标志, 若 psutil/os 采集阻塞会让 join 超时后线程仍活着,
   再次 start() 会叠出第二个巡检线程, 指标翻倍发布。
   现在: Event 替代 sleep 以便及时退出; 线程 daemon; start() 幂等。

3. 订阅泄漏修复
   构造函数里 bus.subscribe(TOPIC_MONITOR_HEARTBEAT, ...) 会永久持有 self。
   调试界面反复开关监控页 / 单元测试反复 new 时, 旧实例无法回收, 且每个
   都还在往自己的看门狗里记心跳 (内存与 CPU 双泄漏)。
   现在: 提供 close()/detach() 解除订阅; 上下文管理器可用。

4. 巡检循环异常不再整轮丢失
   单个模块处理抛异常时只跳过该模块 (原来的 try 包住整个 for, 一个模块
   出错会让本轮剩余的指标/心跳全都不发)。

5. 自身心跳 fps 计算修正 (interval_s=0 时除零 -> ZeroDivisionError)。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional

from MessegeCenter import (
    MessageBus, get_message_bus, Message,
    TOPIC_MONITOR_HEARTBEAT, TOPIC_MONITOR_ALERT,
    TOPIC_MONITOR_METRICS, TOPIC_SYS_LOG, ModuleHeartbeat, SystemAlert
)
from .interfaces import (
    MonitorBackend, MonitorCapabilities, MonitorServiceBase, register_monitor_backend
)
from .health import HealthWatchdog, ModuleStatusRecord
from .alert import AlertManager

logger = logging.getLogger(__name__)

#: 模块离线告警码
ALERT_CODE_MODULE_OFFLINE = 1001


class MonitorService(MonitorServiceBase):
    """
    监控后台服务
    """

    def __init__(self, bus: Optional[MessageBus] = None):
        """
        bus 为 None 时使用全局单例总线。

        注意: 若传入的是全异步总线 (self_dispatch=False), 这里订阅的心跳回调
        会在总线分发线程里执行 —— 看门狗内部有自己的锁, 与巡检线程并发安全。
        """
        bus = bus or get_message_bus()
        super().__init__(bus=bus)
        self.bus = bus
        self.watchdog = HealthWatchdog()
        self.alert_mgr = AlertManager()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._interval_s = 1.0
        # 已报警的离线模块集合 (边沿触发: 只在 "掉线瞬间" 报一次)
        self._offline_reported: set = set()

        # 订阅各模块心跳 —— 记住 (pattern, handler) 以便 close() 时解除
        self._sub_pattern = TOPIC_MONITOR_HEARTBEAT
        self.bus.subscribe(self._sub_pattern, self._on_heartbeat_msg)
        self._detached = False

    @property
    def capabilities(self) -> MonitorCapabilities:
        return MonitorCapabilities(
            supports_host_metrics=True,
            supports_alert_history=True,
            supports_fault_injection=True,
        )

    # -- 面向界面的查询接口 (见 MonitorServiceBase) ------------------------
    def check_all_modules(self) -> Dict[str, ModuleStatusRecord]:
        return self.watchdog.check_all_modules()

    def get_system_metrics(self) -> dict:
        return self.watchdog.get_system_metrics()

    def get_active_alerts(self) -> list:
        return self.alert_mgr.get_active_alerts()

    def get_alert_history(self, limit: int = 50) -> List[SystemAlert]:
        return self.alert_mgr.get_history(limit)

    def clear_all_alerts(self):
        self.alert_mgr.clear_all()
        self._offline_reported.clear()

    # -- 总线回调 --------------------------------------------------------
    def _on_heartbeat_msg(self, msg: Message):
        payload = msg.payload
        if isinstance(payload, ModuleHeartbeat):
            self.watchdog.record_heartbeat(payload)

    # -- 生命周期 --------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    def start(self, interval_s: float = 1.0):
        """启动监控巡检线程 (幂等: 已在运行则只更新周期)"""
        self._interval_s = max(0.05, float(interval_s))
        if self._running and self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            args=(self._interval_s,),
            name="MonitorWatchdogLoop",
            daemon=True
        )
        self._thread.start()

    def stop(self):
        """停止监控服务 (可重复调用)"""
        self._running = False
        self._stop_event.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.5)
        self._thread = None

    def close(self):
        """停止服务并解除总线订阅 (防订阅泄漏)。可重复调用。"""
        self.stop()
        if not self._detached:
            self._detached = True
            try:
                self.bus.unsubscribe(self._sub_pattern, self._on_heartbeat_msg)
            except Exception:
                logger.debug("解除心跳订阅失败", exc_info=True)

    #: 语义别名: 只解除订阅不停线程时用 detach()
    detach = close

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # -- 巡检循环 --------------------------------------------------------
    def _loop(self, interval_s: float):
        interval_s = max(0.05, float(interval_s))
        while self._running and not self._stop_event.is_set():
            self._tick(interval_s)
            # Event.wait 代替 sleep: stop() 能立刻唤醒退出
            if self._stop_event.wait(interval_s):
                break

    def _tick(self, interval_s: float):
        # 1. 检查模块超时状态 (逐模块隔离: 单个模块异常不影响其他模块)
        try:
            module_snapshots = self.watchdog.check_all_modules()
        except Exception:
            logger.exception("监控: 读取模块健康快照失败")
            module_snapshots = {}

        for name, rec in module_snapshots.items():
            try:
                self._evaluate_module(name, rec)
            except Exception:
                logger.exception("监控: 评估模块 %s 健康状态失败", name)

        # 2. 收集系统指标并广播
        try:
            metrics = self.watchdog.get_system_metrics()
            self.bus.publish(TOPIC_MONITOR_METRICS, metrics, sender="monitor")
        except Exception:
            logger.exception("监控: 广播主机指标失败")

        # 3. 发布自身的监控心跳
        try:
            fps = 1.0 / interval_s if interval_s > 0 else 1.0
            self.bus.publish(
                TOPIC_MONITOR_HEARTBEAT,
                ModuleHeartbeat(module_name="monitor", status="OK", fps=fps),
                sender="monitor"
            )
        except Exception:
            logger.exception("监控: 广播自身心跳失败")

    def _evaluate_module(self, name: str, rec: ModuleStatusRecord):
        """边沿触发的离线检测: 掉线报一次, 恢复时解除并记日志"""
        if rec.last_heartbeat_time <= 0:
            # 从未上报过心跳的模块不算故障 (也可能是本进程用不到该模块)
            return

        if rec.status == "OFFLINE":
            if name in self._offline_reported:
                # 持续离线: 不再刷告警, 只保持版面里的在位告警时间戳
                return
            self._offline_reported.add(name)
            alert = self.alert_mgr.raise_alert(
                level="WARNING",
                source=name,
                code=ALERT_CODE_MODULE_OFFLINE,
                message="模块 [%s] 超时未收到心跳包 (已超时 %.1fs)" % (name, rec.elapsed_since_last),
            )
            self.bus.publish(TOPIC_MONITOR_ALERT, alert, sender="monitor")
            self.bus.publish(
                TOPIC_SYS_LOG,
                {"level": "WARN", "msg": "监控: 模块 %s 离线 (code=%d)" % (name, ALERT_CODE_MODULE_OFFLINE)},
                sender="monitor",
            )
        elif rec.status in ("OK", "BUSY", "WARNING", "ERROR"):
            if name in self._offline_reported:
                self._offline_reported.discard(name)
                self.alert_mgr.resolve_alert(name, ALERT_CODE_MODULE_OFFLINE)
                self.bus.publish(
                    TOPIC_SYS_LOG,
                    {"level": "INFO", "msg": "监控: 模块 %s 心跳恢复" % name},
                    sender="monitor",
                )


# ==============================================================================
# 后端注册 —— 现有心跳看门狗实现, 作为系统默认监控后端
# 接替方注册同名或新名后端即可整体替换, 上层无需改动 (见 moniter/interfaces.py)
# ==============================================================================

register_monitor_backend(
    MonitorBackend(
        name="heartbeat_watchdog",
        display_name="心跳看门狗 + 主机资源 (内置实现)",
        service_factory=MonitorService,
        watchdog_factory=HealthWatchdog,
        alert_factory=AlertManager,
    )
)
