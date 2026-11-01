# MessegeCenter/ 使用指导（信息处理中心）

> 契约：`interfaces.py`（`MessageBusBase` / `MessageRouterBase`）；主题与数据类：`topics.py`（全系统唯一定义处）；比赛信息账本：`race.py`。

## 1. 职责
| 部分 | 内容 |
|---|---|
| `bus.py` 消息总线 | 进程内发布/订阅：线程安全、可选同步/异步分发（**当前默认同步**，见下）、fnmatch 通配、历史回溯、吞吐统计；订阅者异常被隔离 |
| `topics.py` 数据契约 | 全部主题常量 + `VisionTarget / ModuleHeartbeat / SystemAlert / RaceState / SortRecord / RaceRule` |
| `race.py` 比赛信息账本 | 按《智能分拣》赛规汇总、复核每一轮的分拣信息（第 3 节） |
| `router.py` 调试路由 | 把通讯事件桥接到总线、按 `task/*` 下发分拣（供 Tk 调试工作台用，延迟导入，不装 pyserial 也能用总线） |

比赛主循环直接调用视觉和通讯，**不经总线编排**；总线是旁路：心跳、告警、比赛信息复核、调试界面。总线出问题不影响比赛动作。

## 2. 快速使用
```python
from MessegeCenter import get_message_bus, TOPIC_VISION_TARGET, VisionTarget
bus = get_message_bus()                         # 全局单例
bus.subscribe("vision/*", lambda m: print(m.topic, m.payload))
bus.publish(TOPIC_VISION_TARGET, VisionTarget(color="red"), sender="vision")
bus.get_history("race/*", limit=20)
```
**分发模式（已知偏差）**：`get_message_bus()` 默认 `self_dispatch=True`，即在 `publish()` 调用线程里同步回调订阅者。这与 `interfaces.py` 契约「分发不得阻塞发布者」不一致，是历史遗留：各调用方都按同步语义调好了，赛前不改默认值。
- 订阅回调要快（几毫秒内），重活扔给自己的线程；RaceLedger、监控都满足。
- 高负载场景（如综合工作台 30FPS 视频流）首次取总线时用 `get_message_bus(self_dispatch=False)` 得到全异步总线。
- 赛后计划：默认改异步，并逐个回归调用方。

## 3. 比赛信息账本 RaceLedger（race.py）

数据来源：`UI/run_report.py` 记录每轮/每件/每个事件时顺手发布（`publish_state / publish_item / publish_event`），账本在第一次比赛时自动启动。

| 赛规 | 账本怎么查 | 结果 |
|---|---|---|
| 一键启动，计时开始 | `race/state running` 清零重开一轮 | 记开始时间 |
| 形状、颜色、信息完全一致才进同一盒 | category ↔ 盒号一一对应 | 同类两个盒 / 一盒两类 → error |
| 6 个储物盒 | 类别数 | 第 7 类 → error |
| 每盒保证能装 4 件 | 每盒件数 | 第 5 件 → warn |
| 每件显示：序号、货物名称、图片、分拣成功总数量 | `SortRecord` 字段 | 序号跳号 / 缺名称或图片 / 总数对不上 → warn |
| 超过 15s 没有动作或没有显示本次信息，本轮结束 | 任何 `race/*` 消息都算一次活动 | 12s warn，15s error（只记录与告警，动作由主循环看门狗 9s 保证） |
| 货物不少于 12 件（初赛） | `EXPECTED_TOTAL` | 结束时件数不足 → warn |

每条结论发布到 `race/rule`（`RaceRule`），同时发 `monitor/alert`（`source="race", code=400`）；本轮结束时 `report.json` 的 `ledger` 里有成功数、尝试数、失败数和全部结论。

```python
from MessegeCenter import get_message_bus, RaceLedger
led = RaceLedger(get_message_bus()).start()
led.snapshot().to_dict()      # {"state","total_done","attempts","failures","slots":{盒号:{label,count}},"rules":[...]}
```

## 4. 主题
| 主题 | 载荷 | 发布方 |
|---|---|---|
| `race/state` | `RaceState(state=idle/running/finished, result, total_done, expected_total)` | run_report |
| `race/item` | `SortRecord(seq, label, category, slot, result, total, image, conf, x_mm, y_mm, elapsed_s)` | run_report |
| `race/event` | `{"level","msg"}` | run_report |
| `race/rule` | `RaceRule(code, level, message)` | RaceLedger |
| `vision/target` | `VisionTarget` 或列表 | 视觉（`scan()` 发列表，预览发单个） |
| `monitor/heartbeat` | `ModuleHeartbeat` | 各模块 ≥1Hz |
| `monitor/alert` `monitor/metrics` | `SystemAlert` / dict | 监控、RaceLedger |
| `comm/status` `comm/cmd/send` `comm/estop` `comm/clear` | `HostStatus` / `HostCmd` | router ↔ 通讯（调试工作台） |
| `task/start` `task/permit` `task/cancel` `task/progress` `task/done` | dict / VisionTarget | 调试工作台 ↔ router |
| `sys/log` | dict / `LogMessage` | 任意 |

## 5. VisionTarget 字段
`target_id, color, shape, confidence, u_pixel, v_pixel, x_mm, y_mm`（原有）+ `class_name, label, info, category, bbox, angle_deg, hits`（YOLO 管线，均有默认值）。`x_mm/y_mm` 已经过去畸变、视差、托盘坐标系换算。

## 6. 约定
- 数据类只在 `topics.py` 定义，别处 re-export（router 用 `isinstance` 判断）；
- 替换为 ZeroMQ/MQTT：实现 `MessageBusBase` 并 `register_message_bus_backend()`；
- 调试：`./is.sh web` 后浏览器打开 `:8090` 的「完整调试」页，可查看总线事件（含 `race/*`）。
