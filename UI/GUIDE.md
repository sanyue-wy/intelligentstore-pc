# UI/ 使用指导（比赛流程）

契约见 `UI/interfaces.py`。装置上的高亮屏只显示分拣成功页；操作都在笔记本浏览器的**执行页**。

## 1. 启动

```bash
./start_competition.sh            # 宿主上执行, 自动进容器; 启动 -> 自检 -> 待机
./is.sh race on                   # 或: 开机自动进入待机 (systemd), ./is.sh race off 关闭
# 无硬件联调:
ALLOW_MOCK=1 USE_SYNTHETIC_CAM=1 START_SOURCES=auto EXPECTED_TOTAL=3 ONESHOT=1 python3 UI/run_competition.py
```
比赛流程：调试结束 → 程序已在待机（自检、模型预热都已完成）→ 裁判下令 → **按装置上的启动键**（或执行页「开始」）→ 双手离开 → 全自动分拣 → 大屏停在最终结果 → 再按一次启动键开始第二次运行（盒子映射和计数自动清零）。

`start_competition.sh` 会：关闭网页调试台和调试工作台（占相机/串口，且比赛中不能有遥控入口）；守护运行，「一键重启」或崩溃后自动拉起回到待机。

## 2. 执行页 http://<X5_IP>:8088

状态（待机/分拣中/已完成/异常）、计时、件数、自检清单、最近一次识别画面、**大屏预览**、本轮每件结果、各盒内容与告警、流程/错误日志。
按钮：**开始**（仅待机）、**停止本轮**、**急停**、**一键重启**、**刷新自检**。没有任何手动控制机械臂或改参数的功能。

## 3. 一键启动（start_trigger.py）

任一来源触发即开始，只认待机之后的第一次按键，运行中再按无效：

| 来源 | 配置 |
|---|---|
| USB 单键按钮 / 小键盘 | 插上即可（读 /dev/input，容器 --privileged 已透传）。`START_KEY_CODES=28,57,96` 只认回车/空格/小键盘回车 |
| X5 40pin GPIO 按键 | `START_GPIO_PIN=37`（BOARD 编号，按下接地，内部上拉，50ms 消抖） |
| 执行页「开始」 | 默认开启 |
| 终端回车 | 有终端时（调试） |
| `START_SOURCES=auto` | 不等待（仿真联调） |

## 4. 主循环（run_competition.py）

```
扫描 -> 选件 (置信度 + 间隙 - 重叠) -> 风险评估 -> 分盒 -> 下位机闭环抓放 -> 大屏 (推送完成才继续)
```
| 赛规 | 处理 |
|---|---|
| 15s 无动作或无显示判负 | 动作看门狗：距上次动作 > `IDLE_GUARD_S`(9s) 先抖料；单件超时 12s |
| 形状+颜色+信息一致才同盒 | category 永久映射到盒子 |
| 每盒 ≤4 件、共 6 盒 | 超出时执行页告警（多半识别错了），第 7 类并入最相近的盒 |
| 分错盒比慢更亏 | 置信度 < `COMMIT_CONF` 或与别的货物框重叠 → 先抖料重扫（最多 2 次）再抓 |
| 完成后才能分拣下一件 | `ui.update_status` 后 `ui.flush(1s)` |
| 用时短优先 | `PREFETCH_STAGE=4/5`：机械臂去放置位时就扫描下一件（先确认那时机械臂已离开相机视野） |
| 分错盒比慢更亏 | 储物盘转位无 ACK：补发一次（`GOTO_SLOT` 是绝对槽位，重发无副作用），仍无 ACK 就**放弃本件不抓**（`SortResult.slot_fail`），不计入该位置的失败次数 |
| 不能无限循环 | 同一位置失败 `MAX_FAILS_PER_SPOT` 次后本轮跳过；剩下的全被跳过时抖料最多 `MAX_BLOCKED_SHAKES` 次后结束（结果「剩余货物无法抓取」）；连续失败 `MAX_CONSEC_FAILS` 或累计 `MAX_ROUND_FAILS` 次结束（结果「失败过多」，多半是下位机/标定/夹爪问题） |

## 5. 组件

| 文件 | 内容 |
|---|---|
| `run_competition.py` | CompetitionApp：待机/运行/执行页接口 |
| `planner.py` | 类别→盒子映射、选件（`skip` 暂避 / `blocked` 永久跳过）、风险评估、容量告警、`display_name()`；映射读写加锁（执行页线程并发读） |
| `controller.py` | 单件闭环抓放、抖料、`last_motion`、阶段回调 |
| `display_ui.py` | 大屏推送 + 本地兜底 + `render_screen()`（执行页/调试台预览共用） |
| `start_trigger.py` | 一键启动 |
| `run_page.py` | 执行页 |
| `run_report.py` | `records/runs/<时间>/report.json` + 截图 + 每次识别的原图/标注图/结果 |
| `precheck.py` | 赛前自检清单（`./is.sh precheck`） |

## 6. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `EXPECTED_TOTAL` | 0 | 已知总数，达到即结束 |
| `START_SOURCES` | web,key,gpio,enter | 启动来源 |
| `START_GPIO_PIN` / `START_KEY_DEVICE` / `START_KEY_CODES` | — | 启动键 |
| `COMMIT_CONF` | 0.45 | 低于此置信度先重扫 |
| `MAX_UNCERTAIN_RETRY` | 2 | 不确定时最多重扫次数 |
| `PREFETCH_STAGE` | 0 | 提前扫描的下位机阶段（0=关） |
| `IDLE_GUARD_S` | 9 | 动作看门狗 |
| `MAX_FAILS_PER_SPOT` | 3 | 同一位置（25px 内）失败几次后本轮跳过 |
| `MAX_BLOCKED_SHAKES` | 2 | 只剩被跳过的货物时最多抖料几次 |
| `MAX_CONSEC_FAILS` | 8 | 连续失败几次结束本轮 |
| `MAX_ROUND_FAILS` | 20 | 本轮累计失败几次结束本轮 |
| `SORT_TIMEOUT_S` | 12 | 单件超时 |
| `SCAN_FRAMES` | 配置 | 每次扫描融合帧数 |
| `ONESHOT` | 0 | 跑完一轮退出 |
| `IS_RUN_PAGE_PORT` | 8088 | 执行页端口（0 关闭） |
| `ALLOW_MOCK` / `USE_SYNTHETIC_CAM` | 0 | 仅联调 |
| `X5_SCREEN_ENDPOINT` | http://127.0.0.1:8756/api/screen | 大屏服务 |

## 7. 复盘
每轮一个目录 `records/runs/<时间>/`（保留 30 轮），网页调试台「复盘」页浏览；日志 `/var/log/intelligentstoring/competition.log`。

`run_report.py` 同时把本轮状态、每件结果、流程事件旁路发布到消息总线 `race/*`，信息处理中心 `RaceLedger` 按赛规复核（同类同盒、一盒一类、≤4 件/盒、≤6 类、序号连续、显示信息齐全、15s 无动作），结论写进 `report.json` 的 `ledger`，严重问题同时进 `warnings`。总线异常不影响比赛流程。
