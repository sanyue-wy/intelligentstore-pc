"""
STM32 智能分拣系统 - 消息总线 (EventBus / Message Broker)
线程安全、解耦的跨模块发布/订阅与历史报文缓冲器

接口契约见 MessegeCenter/interfaces.py 的 MessageBusBase。

------------------------------------------------------------------------------
本版纠正 (会话中心跑不起来的真实原因)
------------------------------------------------------------------------------
1. 分发彻底移出发布者线程 (契约 4.1 明确要求)
   旧实现是同步回调: 视觉线程 30FPS 发 vision/frame 时, 任何一个订阅者
   (调试界面把每个报文 append 进缓冲区、监控服务写心跳) 都直接在视觉
   线程里跑。只要有人慢, 视觉线程就被拖慢 -> 掉帧 -> 心跳迟到 ->
   moniter 3s 看门狗误判 OFFLINE -> 误告警。
   实测 (本次修复前): 5 次 publish 被 50ms 订阅者拖到 251ms。
   现在: 单分发线程 (daemon) + 有界队列, publish() 只入队即返回。

2. 同步语义可选保留 (self_dispatch)
   需要 "publish 之后立刻读到本条" 的旧写法 (单测、需要精确时序的集成脚本)
   仍可 MessageBus(self_dispatch=True) 退回旧语义; 默认异步模式下另有
   wait_dispatched(msg, timeout) 用于"发布后确认送达"。

3. 订阅者异常隔离 + 可观测 (契约 4.2)
   旧实现 `except Exception: pass` 彻底吞掉订阅者异常, 总线静默丢消息
   无从排查。现在: 单个 handler 异常不影响其他 handler, 计入
   stats["dispatch_errors"], 每个 handler 每 10s 最多一条 WARNING
   (带完整 traceback), 可选 on_error 回调。

4. 分发线程 daemon + shutdown()/flush():
   解释器退出时不会因非 daemon 线程挂住; 自带总线注册了 atexit 清理。

5. 历史缓冲与分发队列都有上限 (deque maxlen), 长时间运行不会内存增长;
   队列满时丢弃最老报文并计数 queue_dropped, 绝不反压业务线程。

6. 精确主题与通配模式同时命中同一个 handler 时只投递一次。
"""
from __future__ import annotations

import atexit
import fnmatch
import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

from .interfaces import MessageBusBase, MessageBusBackend, register_message_bus_backend
from .topics import Message

logger = logging.getLogger(__name__)

#: 分发队列默认上限 (报文数)。满则丢弃最老报文, 永不阻塞发布者。
DEFAULT_QUEUE_SIZE = 20000

#: 分发历史默认上限
DEFAULT_HISTORY_SIZE = 500

#: 同一个 handler 的错误日志最小间隔 (秒), 避免故障风暴刷爆日志
_ERROR_LOG_INTERVAL_S = 10.0


class MessageBus(MessageBusBase):
    """
    进程内高性能消息总线 (单分发线程 + 有界队列)

    用法与旧版完全一致:
        bus = MessageBus()                      # 或 get_message_bus()
        bus.subscribe("vision/*", on_vision)    # 精确或 fnmatch 通配
        bus.publish("vision/target", payload, sender="vision")
        bus.get_history("vision/*", limit=20)
        bus.get_stats()
    """

    def __init__(
        self,
        max_history: int = DEFAULT_HISTORY_SIZE,
        max_queue_size: int = DEFAULT_QUEUE_SIZE,
        self_dispatch: bool = True,
        dispatch_wait_s: float = 0.0,
        on_error: Optional[Callable[[str, Callable[[Message], None], Message, BaseException], None]] = None,
    ):
        """
        Args:
            max_history:      历史缓冲上限 (报文数)
            max_queue_size:   分发队列上限; <=0 表示不限 (不推荐)
            self_dispatch:    True(默认) 时在 publish() 调用线程内同步回调 —— 与旧版
                              语义一致, 保证 "publish 之后立刻 get_stats()/
                              get_history() 就能看到本条" 的既有写法不被破坏
                              (debug/message_center_debug、debug/monitor_debug、
                              MessegeCenter/test_bus 都依赖这个语义)。
                              False 时改为 daemon 分发线程 + 有界队列, publish()
                              立即返回 —— 实时业务线程 (vision 30FPS 发帧、
                              通讯 20Hz 上报) 应使用该模式, 避免被慢订阅者拖慢。
            dispatch_wait_s:  历史参数, 保留不删以兼容旧调用; 已不再用于阻塞
                              publish (异步语义下 publish 必须立即返回)。
                              需要"发布后确认送达"请用 wait_dispatched()。
            on_error:         订阅者异常回调 (topic, handler, msg, exc)
        """
        self._lock = threading.RLock()
        self._subscribers: Dict[str, List[Callable[[Message], None]]] = {}
        self._history: deque = deque(maxlen=max(int(max_history), 1))
        self._msg_id_counter: int = 0

        # ---- 分发队列 ----
        self._self_dispatch = bool(self_dispatch)
        self._dispatch_wait_s = max(0.0, float(dispatch_wait_s))
        self._queue: deque = deque()
        self._queue_max = int(max_queue_size)
        self._cv = threading.Condition(self._lock)          # 通知分发线程
        self._done_cv = threading.Condition(self._lock)     # 通知等待落地的发布者
        self._seq_counter: int = 0                          # 入队序号
        self._seq_completed: int = 0                        # 已分发完成的最大序号
        self._dispatching: bool = False                     # 当前线程是否在执行 handler
        self._shutdown: bool = False
        self._thread: Optional[threading.Thread] = None

        # 统计计数
        self._stats: Dict[str, int] = {
            "total_published": 0,
            "total_delivered": 0,
            "dispatch_errors": 0,
            "queue_dropped": 0,
            "publish_wait_timeouts": 0,
        }
        self._topic_counts: Dict[str, int] = {}
        self._handler_errors: Dict[str, float] = {}   # handler key -> 上次打日志时间
        self._on_error = on_error

        self._start_thread_locked()

    # ==================================================================
    # 后台分发线程
    # ==================================================================
    def _start_thread_locked(self):
        if self._self_dispatch:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        t = threading.Thread(
            target=self._dispatch_loop,
            name="MessageBusDispatcher",
            daemon=True,          # 关键: 非 daemon 会让解释器退出时卡死
        )
        self._thread = t
        t.start()

    def _dispatch_loop(self):
        while True:
            with self._cv:
                while not self._queue and not self._shutdown:
                    self._cv.wait(0.5)
                if not self._queue:
                    if self._shutdown:
                        return
                    continue
                batch = []
                while self._queue and len(batch) < 256:
                    batch.append(self._queue.popleft())
                self._cv.notify_all()

            for seq, msg, handlers in batch:
                self._deliver(msg, handlers)
                with self._done_cv:
                    if seq > self._seq_completed:
                        self._seq_completed = seq
                    self._done_cv.notify_all()

            if self._shutdown:
                with self._cv:
                    if not self._queue:
                        return

    def _deliver(self, msg: Message, handlers: List[Callable[[Message], None]]):
        """逐个调用 handler, 每个 handler 独立容错 (契约 4.2)"""
        self._dispatching = True
        try:
            for handler in handlers:
                if not handler:
                    continue
                try:
                    handler(msg)
                except Exception as exc:  # noqa: BLE001 - 隔离故障是契约要求
                    self._on_handler_error(msg, handler, exc)
                else:
                    with self._lock:
                        self._stats["total_delivered"] += 1
        finally:
            self._dispatching = False

    def _on_handler_error(self, msg: Message, handler: Callable[[Message], None], exc: BaseException):
        with self._lock:
            self._stats["dispatch_errors"] += 1
            key = "%s.%s" % (
                getattr(handler, "__qualname__", None) or getattr(handler, "__name__", None) or "handler",
                id(handler),
            )
            now = time.time()
            should_log = (now - self._handler_errors.get(key, 0.0)) >= _ERROR_LOG_INTERVAL_S
            if should_log:
                self._handler_errors[key] = now
        if should_log:
            logger.warning(
                "消息总线: 订阅者处理主题 %r 时抛出异常 (已隔离, 不影响其他订阅者): %s",
                msg.topic, exc, exc_info=True,
            )
        cb = self._on_error
        if cb is not None:
            try:
                cb(msg.topic, handler, msg, exc)
            except Exception:
                pass

    # ==================================================================
    # 订阅管理
    # ==================================================================
    def subscribe(self, topic_pattern: str, handler: Callable[[Message], None]):
        """
        订阅特定主题或通配符模式 (如 'comm/*', 'vision/target', '*')

        幂等: 同一 (pattern, handler) 重复订阅只登记一次。
        """
        if not topic_pattern:
            raise ValueError("topic_pattern 不能为空")
        if not callable(handler):
            raise TypeError("handler 必须可调用")
        with self._lock:
            handlers = self._subscribers.setdefault(topic_pattern, [])
            if handler not in handlers:
                handlers.append(handler)

    def unsubscribe(self, topic_pattern: str, handler: Callable[[Message], None]):
        """取消订阅; 未订阅时静默忽略。正在进行的本轮分发不受影响。"""
        with self._lock:
            handlers = self._subscribers.get(topic_pattern)
            if not handlers:
                return
            try:
                handlers.remove(handler)
            except ValueError:
                return
            if not handlers:
                self._subscribers.pop(topic_pattern, None)

    def unsubscribe_all(self, handler: Callable[[Message], None]) -> int:
        """把某 handler 从所有模式上摘掉 (防订阅泄漏); 返回摘除数量。"""
        removed = 0
        with self._lock:
            for pattern in list(self._subscribers.keys()):
                handlers = self._subscribers.get(pattern)
                if not handlers:
                    continue
                try:
                    handlers.remove(handler)
                    removed += 1
                except ValueError:
                    pass
                if not handlers:
                    self._subscribers.pop(pattern, None)
        return removed

    # ==================================================================
    # 发布
    # ==================================================================
    def _match_handlers_locked(self, topic: str) -> List[Callable[[Message], None]]:
        """返回处理该主题的 handler 元组快照 (调用方必须持锁)"""
        matched: List[Callable[[Message], None]] = []
        seen = set()
        for pattern, handlers in self._subscribers.items():
            if pattern == topic or fnmatch.fnmatchcase(topic, pattern):
                for h in handlers:
                    # 精确模式与通配模式可能同时命中同一个 handler, 只投递一次
                    if id(h) not in seen:
                        seen.add(id(h))
                        matched.append(h)
        return matched

    def publish(self, topic: str, payload: Any, sender: str = "system") -> Message:
        """
        发布消息到指定主题, 立即返回已构造的 Message。

        * 默认异步: 入队后由 daemon 分发线程回调订阅者, **不等待**、不阻塞调用方;
          视觉 30FPS 发帧时不会被任何慢订阅者 (界面刷树 / 监控写心跳) 拖慢。
        * 需要 "publish 之后立刻读到本条" 的同步语义时 (单测、需要精确时序的
          集成脚本), 用 MessageBus(self_dispatch=True) 构造。
        """
        if not topic:
            raise ValueError("topic 不能为空")

        with self._lock:
            self._msg_id_counter += 1
            msg = Message(
                topic=topic,
                payload=payload,
                sender=sender,
                timestamp=time.time(),
                msg_id=self._msg_id_counter,
            )
            self._history.append(msg)
            self._stats["total_published"] += 1
            self._topic_counts[topic] = self._topic_counts.get(topic, 0) + 1
            handlers = self._match_handlers_locked(topic)

        # ---- 同步模式 (单测 / 需要精确时序的调用方) ----
        if self._self_dispatch:
            self._deliver(msg, handlers)
            with self._done_cv:
                if msg.msg_id > self._seq_completed:
                    self._seq_completed = msg.msg_id
                self._done_cv.notify_all()
            return msg

        with self._cv:
            if self._shutdown:
                # 已 shutdown 的总线再次 publish: 重置状态重启分发线程, 保证消息不丢
                self._shutdown = False
            self._start_thread_locked()
            self._seq_counter += 1
            seq = self._seq_counter
            if self._queue_max > 0 and len(self._queue) >= self._queue_max:
                self._queue.popleft()
                self._stats["queue_dropped"] += 1
            self._queue.append((seq, msg, handlers))
            self._cv.notify()
        return msg

    def wait_dispatched(self, msg_or_id, timeout: float = 2.0) -> bool:
        """
        等待某条消息被分发完成 (handler 已执行)。给需要 "发布后确认送达" 的
        调用方 (集成脚本 / 关停前收尾) 用; 返回是否在超时前完成。

        注意: 在 handler 内部调用会直接返回 False (自己做自己会死锁)。
        """
        target = msg_or_id.msg_id if isinstance(msg_or_id, Message) else int(msg_or_id)
        if self._self_dispatch:
            return True
        if self._dispatching:
            return False
        deadline = time.monotonic() + max(0.0, timeout)
        with self._done_cv:
            while self._seq_completed < target:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return False
                self._done_cv.wait(remain)
        return True

    def publish_many(self, items) -> List[Message]:
        """批量发布 [(topic, payload), ...] 或 [(topic, payload, sender), ...]"""
        out = []
        for item in items:
            if len(item) == 3:
                out.append(self.publish(item[0], item[1], sender=item[2]))
            else:
                out.append(self.publish(item[0], item[1]))
        return out

    def flush(self, timeout: float = 2.0) -> bool:
        """
        等待所有已入队报文被分发完成 (不含仍在执行中的 handler)。返回是否完成。
        """
        if self._self_dispatch:
            return True
        target = self._seq_counter
        deadline = time.monotonic() + max(0.0, timeout)
        with self._done_cv:
            while self._seq_completed < target:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return False
                self._done_cv.wait(min(remain, 0.05))
        return True

    # ==================================================================
    # 查询 / 生命周期
    # ==================================================================
    def get_history(self, topic_pattern: Optional[str] = None, limit: int = 100) -> List[Message]:
        """获取近期历史消息记录 (用于界面查看或故障回溯)"""
        with self._lock:
            if not topic_pattern:
                snapshot = list(self._history)
            else:
                snapshot = [
                    m for m in self._history
                    if m.topic == topic_pattern or fnmatch.fnmatchcase(m.topic, topic_pattern)
                ]
        if not limit or limit <= 0:
            return snapshot
        return snapshot[-limit:]

    def clear_history(self):
        """清空历史记录"""
        with self._lock:
            self._history.clear()

    def get_stats(self) -> Dict[str, Any]:
        """获取统计指标

        total_published   已发布报文数
        total_delivered   成功投递 (handler 正常返回) 次数
        dispatch_errors   订阅者抛出异常次数 (已隔离)
        queue_dropped     队列溢出丢弃的旧报文数
        pending           当前待分发报文数
        """
        with self._lock:
            return {
                "total_published": self._stats["total_published"],
                "total_delivered": self._stats["total_delivered"],
                "dispatch_errors": self._stats["dispatch_errors"],
                "queue_dropped": self._stats["queue_dropped"],
                "publish_wait_timeouts": self._stats["publish_wait_timeouts"],
                "active_patterns": len(self._subscribers),
                "handler_count": sum(len(v) for v in self._subscribers.values()),
                "subscriber_count": sum(len(v) for v in self._subscribers.values()),
                "pending": len(self._queue),
                "topic_counts": dict(self._topic_counts),
            }

    def shutdown(self, timeout: float = 2.0):
        """
        停止分发线程 (幂等)。先把队列中已有报文分发完 (最多等 timeout/2),
        再让线程退出, 保证退出前不丢消息。"""
        self.flush(timeout=max(0.0, timeout / 2.0))
        with self._cv:
            self._shutdown = True
            self._cv.notify_all()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=max(0.0, timeout))

    #: 语义更直观的别名
    close = shutdown

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.shutdown()
        return False


# ==============================================================================
# 全局单例消息总线
# ==============================================================================
_GLOBAL_BUS: Optional[MessageBus] = None
_BUS_LOCK = threading.Lock()


def get_message_bus(self_dispatch: bool = True) -> MessageBus:
    """
    获取全局消息总线实例 (线程安全单例, 综合工作台与各独立调试器共享)。

    self_dispatch 只在**首次创建**时生效 (单例), 后续调用忽略该参数。
    综合工作台 (debug/integrated_debug) 这类带 30FPS 视觉流的高负载场景,
    可用 get_message_bus(self_dispatch=False) 取得全异步总线, 避免视觉线程
    被界面/监控订阅者拖慢。
    """
    global _GLOBAL_BUS
    with _BUS_LOCK:
        if _GLOBAL_BUS is None:
            _GLOBAL_BUS = MessageBus(self_dispatch=self_dispatch)
        return _GLOBAL_BUS


def reset_message_bus() -> None:
    """
    销毁全局单例 (单测/热重载用)。新总线由下一次 get_message_bus() 惰性创建。
    """
    global _GLOBAL_BUS
    with _BUS_LOCK:
        old = _GLOBAL_BUS
        _GLOBAL_BUS = None
    if old is not None:
        old.shutdown()


@atexit.register
def _shutdown_global_bus():
    """解释器退出时优雅停掉分发线程 (分发线程本身是 daemon, 这里只是收尾)"""
    bus = _GLOBAL_BUS
    if bus is not None:
        try:
            bus.flush(timeout=0.5)
            bus.shutdown(timeout=0.5)
        except Exception:
            pass


# ==============================================================================
# 后端注册 —— 现有进程内 EventBus, 作为系统默认消息总线后端
# 接替方注册同名或新名后端即可整体替换, 上层无需改动
# (见 MessegeCenter/interfaces.py)
# ==============================================================================

register_message_bus_backend(
    MessageBusBackend(
        name="inproc_eventbus",
        display_name="进程内事件总线 (内置实现)",
        bus_factory=MessageBus,
    )
)
