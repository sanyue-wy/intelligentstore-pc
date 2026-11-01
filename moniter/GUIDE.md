# moniter/ 使用指导（监控模块）

> 本模块本轮**未改动**。契约：`interfaces.py`（门面 `MonitorServiceBase`）。

## 1. 职责
订阅各模块心跳，3 秒未收到判定离线并发 `code=1001` 告警；周期发布主机 CPU/内存/线程指标；管理在位告警与历史。

| 文件 | 作用 |
|---|---|
| `health.py` | `HealthWatchdog`：心跳记录、超时判定、psutil 资源采集 |
| `alert.py` | `AlertManager`：告警生命周期 |
| `service.py` | `MonitorService`：后台巡检线程 |

## 2. 快速使用
```python
from moniter import create_monitor_service
mon = create_monitor_service()          # 使用全局消息总线
mon.start(interval_s=1.0)
print(mon.check_all_modules())          # {name: ModuleStatusRecord}
print(mon.get_system_metrics(), mon.get_active_alerts())
mon.stop()
```

## 3. 各模块须遵守的心跳约定
```python
from MessegeCenter import get_message_bus, TOPIC_MONITOR_HEARTBEAT, ModuleHeartbeat
get_message_bus().publish(TOPIC_MONITOR_HEARTBEAT,
    ModuleHeartbeat(module_name="vision", status="OK", fps=25.0, details={}), sender="vision")
```
- 频率 ≥ 1Hz（阈值 3s）；`module_name` 用固定名：`communication / vision / message_center / coordinator / monitor`。
- 视觉服务在后台线程里每秒自动发心跳（比赛 `continuous_detect=False` 时也照发）。
- 从未上报过心跳的模块不算故障。

## 4. 比赛规则告警
信息处理中心 `MessegeCenter/race.py` 的 `RaceLedger` 发现赛规风险时，同时发布 `monitor/alert`：`SystemAlert(source="race", code=400)`，`level` 为 WARNING（盒超 4 件、序号跳号、显示信息不全、12s 无动作）或 ERROR（同类分到两个盒、一盒混两类、第 7 类、15s 无动作）。监控看板和日志里都能看到。

## 5. 替换后端
实现 `MonitorServiceBase`（可复用 `HealthWatchdogBase` / `AlertManagerBase`）并 `register_monitor_backend()`。

## 6. 调试
`python main.py --monitor`：心跳、资源、告警看板。
