# communication/ 使用指导（通讯模块）

> 本模块本轮**未改动**。契约：`interfaces.py`（门面 `SortingClientBase`），帧格式：`protocol.py`。

## 1. 职责
上位机 ⇄ STM32 下位机，USB CDC 虚拟串口，**32 字节定长二进制帧（小端）**。下位机 20Hz 上报 `HostStatus`；上位机发 `HostCmd`，带 SEQ + ACK 重发。

| 文件 | 作用 |
|---|---|
| `protocol.py` | 帧打包/解包、全部枚举（CmdFlags、SysState、ErrorMask…），纯数据无 I/O |
| `client.py` | `IntelligentSortingClient`：连接、收包线程、ACK、指令、任务执行器 |
| `serial_discovery.py` | 串口自动发现：显式参数 > `STM_PORT` > 扫描 `/dev/ttyACM*`、`/dev/ttyUSB*` |
| `mock_device.py` | `MockSTM32` 仿真下位机（行为与固件任务流程一致） |
| `test_comm.py` | 单元测试 |

## 2. 快速使用
```python
from communication import create_comm_client
c = create_comm_client()                 # 默认后端 usb_cdc_32b
c.connect(None)                          # None/"auto" = 自动发现; 失败返回 False, 不抛异常
# c.connect_auto(fallback_mock=False)    # 无人值守入口
# c.connect_mock()                       # 无硬件联调

c.send_clear()                           # 清报警
c.send_storage_slot(2)                   # 储物盘转到 2 号格 (0~5)
res = c.execute_sorting_task_ex(task_id=1, target_x_mm=40.0, target_y_mm=-12.5,
                                timeout_s=12.0, permit_delay_s=0.0)
print(res.success, res.is_grip_fail, res.message)
c.disconnect()
```

## 3. 分拣任务时序（固件闭环，务必按此使用）
```
MOTION(x,y) ──▶ 下位机去取件位 ──▶ RES_FLAGS|=PICK (等待许可)
PERMIT      ──▶ 下潜夹取 ─▶ 运送 ─▶ 放置 ─▶ 归位 ──▶ RES_FLAGS = DONE|OK
异常: GRIPFAIL(抓空) / ESTOP / ABORT / TIMEOUT
```
`execute_sorting_task_ex` 已完整实现以上流程，**不要**再手动拼 `send_move + z_down + grip`（旧 controller 就是这样错的：不发 PERMIT，机械臂永远停在取件位）。

## 4. 常用 API
| 方法 | 说明 |
|---|---|
| `latest_status` / `read_status(timeout)` | 最近一帧 `HostStatus` |
| `is_connected` / `is_mock` / `is_link_alive` | 连接状态（`is_link_alive`：最近是否收到 20Hz 状态） |
| `send_move / send_permit / send_cancel` | 任务控制 |
| `send_estop / send_clear` | 急停 / 清报警 |
| `send_grip_open/close`, `send_z_up/down` | 单动作（调试用） |
| `send_tray_shake / send_tray_home` | 托盘抖料 / 回原点 |
| `send_storage_slot(slot)` / `send_storage_step(ccw)` | 储物盘定位 (0~5) / 单步 60° |
| `execute_sorting_task_ex(...)` | 一次完整分拣闭环，返回 `SortingTaskResult` |
| 回调列表 `on_status_callbacks` 等 | 由 `MessegeCenter/router.py` 桥接到总线 |

`HostStatus` 上层依赖属性见 `interfaces.py` 第 4 节（`cur_x_mm`、`is_task_done`、`is_grip_fail`、`storage_slot`…）。

## 5. 替换后端
实现 `SortingClientBase` 并 `register_comm_backend(CommBackend(name=..., client_factory=...))`。
**注意**：比赛流程额外依赖 `send_storage_slot / send_tray_shake / send_tray_home / execute_sorting_task_ex`（见 `UI/interfaces.py` 的 `SortingClientLike`），替换时必须一并提供。

## 6. 调试
`./is.sh web` 后浏览器打开 `:8090` 的「通讯调试」页：串口发现/连接/模拟、JOG、夹爪、32B Hex 报文监视。

## 7. 环境变量
| 变量 | 说明 |
|---|---|
| `STM_PORT` | 指定串口，如 `/dev/ttyACM0`；缺省自动发现 |


## 兼容兜底：固件不上报 RES_FLAGS.PICK 时（方案B）
`execute_sorting_task_ex` 增加"运动到位即发许可"的兜底：若固件（如 app.c 业务层）到达取件位后不置
`RES_FLAGS bit2 (PICK=0x04)`，上位机会在机械臂运动到目标附近（默认 ≤ `PERMIT_ARRIVE_TOL_MM`=5mm）并
连续稳定 `PERMIT_STABLE_S`（默认 1.0s）后，主动视为到位并下发 `PERMIT`，从而兼容新旧固件。
- 关闭：`PERMIT_FALLBACK=0`
- 调容差：`PERMIT_ARRIVE_TOL_MM`（mm）
- 调稳定判定：`PERMIT_STABLE_S`；最小耗时：`PERMIT_MIN_ELAPSED_S`
报警/急停/有 error 锁存时不会兜底发许可。单测见 `communication/test_permit_fallback.py`。
