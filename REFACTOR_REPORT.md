# 智能分拣上位机（RDK X5）工程重构报告 / PR 说明

## 0. 结论速览

- 4 个阶段全部完成：阶段0 修坐标自检 → 阶段1 去嵌套 → 阶段2 统一网页 → 阶段3 删旧，按阶段独立提交。
- **行为不变**：`communication/` 帧格式 / SEQ-ACK / `is_pick_ready`(只认 `RES_FLAGS & 0x04`) / 串口自动发现，以及 `UI/run_competition.py` 的状态机 / 看门狗 / 失败上限 / 抖料 / 赛规告警，均**只移动或改 import，未改逻辑**。
- 全量测试：**65 个单元测试全过** + **仿真端到端 3 件全成功（15.7s）** + `bash -n` 全过 + 残留引用 grep 为 NONE。
- 1 项待用户确认：是否删除 `vision/models/best_1027_640x480.onnx`（12MB，ONNX 回退用）。
- 端口未改：调试台 **8090**、比赛页 **8088**、大屏推送 **8756**（大屏服务不在本仓库）。

## 1. 阶段与提交

| 阶段 | 分支 | commit | 内容 |
|---|---|---|---|
| 阶段0 | `main` | bae6c2e, 1d28cf0, c28cb67, 48aad72, c50f4bc, d7e007b | 修坐标自检、平面位姿取低倾角、标定配置按现场更新、文档同步 |
| 阶段1 | `main` | 2fa1ed7 | 去除无意义嵌套（删空转发/重复入口/示例） |
| 阶段2 | `main` | 9a4fc9b | 统一网页调试台(8090 四节) + 比赛页(8088) + 共享样式 |
| 阶段3 | `main` | eaf7168 | 删旧（Tk 调试器/备份目录/货物照），部署与文档同步 |

> 全部阶段提交都在 `main` 上、线性顺序，`git log --oneline` 即可逐条查看；没有额外的重构分支。

## 2. 阶段 0：修自检的坐标问题

现场 `vision/calib_config.json` 的根因是**配置缺失**（`tray.ref_points=[]`、`tray.origin_mm=null`、无 `camera_matrix/dist_coeffs`），叠加 `estimate_pose` 未收敛仍返回 `ok=True` 的 bug。

改动：
1. **`self_check()` 提示可操作化**：无参考点/无 origin 时明确提示「去调试台 → 托盘页 → 加参考点（≥2 个），或填 origin_mm」；「未做畸变校正」单独成一条并给出「一键单张棋盘格校正」入口（`tools/calib_intrinsics.py --one` / 网页畸变页，棋盘格 `tools/calib_board_A4.pdf`）；「四角一致性」给出量化指标与可能原因（畸变/不同高度/WH 填错）。
2. **`estimate_pose` 收敛判定**：二分未收敛，或反算镜头高与填写 `cam_height_mm` 相差 >15%，返回 `ok=False` 并给出明确提示；此时 `pixel_to_tray / pixel_to_robot` 退回无高度修正的平面单应（径向公式），自检报出。
3. **补单元测试**覆盖「未收敛/高差异常」场景。
4. **清理旧字段**：托盘模式不再写/读顶层 `src_pixels / dst_robot_mm / reproj_error_mm`（文档说明两套坐标优先级）。
5. **`cam_height_mm` 语义**统一为「镜头光心到托盘底面的垂直高度(mm)」，网页保存时与反算差得多会提示复核。
6. 按现场确认更新 `vision/calib_config.json`（focal_px、fixed_z_mm、origin_mm[0]、cam_height 148、corners 重采样等），并同步坐标系/axes 文档。

验收：用现场配置跑自检，问题列表准确、可操作、无误报；新增单元测试通过；合成数据斜视投影误差仍 <0.1mm（单元测试断言）。

## 3. 阶段 1：去除无意义嵌套（不破解耦）

- 删除 `vision/camera.py`（459B，仅转发到 `vision/cameras/`，确认空转发）→ 统一从 `vision.cameras` / `vision.cameras.uvc` 导入。
- 删除 `tools/convert.sh`（仅转调根目录 `convert.sh` 的重复入口）。
- 删除 `communication/example_usage.py`（示例代码，内容并入 `communication/GUIDE.md`）。
- 收敛各 `__init__.py` 大段 re-export，只保留上层真正用到的符号。
- **`vision/detector.py` 与 `vision/yolo_detector.py` 不合并**：二者职责不重叠（`YoloWorkpieceDetector` 继承 HSV `WorkpieceDetector`，共用基类而非重复实现），合并会破坏解耦，故保留（理由已写进 MODULES.md 阶段1 记录）。
- `interfaces.py` 抽象基类与后端注册表**保留**（解耦用）。
- 验证：`main.py --help`、`./is.sh` 各子命令正常；59 测试通过（阶段2前基数）。

## 4. 阶段 2：网页界面统一

新增公共网页包 **`webui/`**（纯标准库 `http.server` + `ThreadingHTTPServer`，无 Node/npm、无 CDN）：
- `webui/base.py`：口令校验、静态资源读取（防目录穿越）、JSON 错误格式、MIME。
- `webui/pages.py`：顶部统一导航 + 三个附加章节页面（`/comm` `/flow` `/run`）。
- `webui/comm_debug.py`：串口发现/连接/mock/收发命令（clear/estop/cancel/permit/grip/z/tray/storage/poll/move），收发日志 hook。
- `webui/flow_debug.py`：单步驱动比赛流程（scan/plan/exec 一件/shake/reset/整轮起停），复用 `SorterPlanner` + `CompetitionController`。
- `webui/screen.py`：全屏只读大屏（`/screen`，无任何调试控件）。
- `webui/static/app.css` + `common.js`：一份 CSS、一份公共 JS（请求封装/轮询/toast/确认弹窗/图片流/转义）。
- `webui/test_webui.py`：6 个 API 用例（token、静态资源、页面、8090 server、comm mock、8088 run_page）。

**8090 统一调试台（一个服务、一个口令、四节）**：在 `tools/vision_tool.py cmd_web` 基础上加顶部导航与附加章节：
1. 视觉调试（原 `vision_tool web` 全部功能原样保留：相机/畸变/托盘四角+参考点/ROI/颜色/识别/点哪去哪/复盘回归）。
2. 比赛流程（UI）调试：单步 scan/plan/exec/抖料/转位，看 planner、料盒、RaceLedger 复核。
3. 上下位机通讯调试：串口选择/自动发现/连接、HostStatus 实时字段、各命令发送（含 GOTO_SLOT/STEP/夹取）、收发日志、ACK、`MockSTM32` 模拟。
4. 完整过程调试：一键跑整轮（真机或 mock+合成相机），相机画面/检测框/每步状态/通讯日志/总线事件/监控告警，可单步/暂停/急停。

**8088 比赛页**：`UI/run_page.py` 换成共享 `app.css`，`<body class="is-competition">` 红/橙顶栏 + 「比赛模式」徽标与调试台醒目区分；保留自检/开始/停止本轮/急停/一键重启/状态进度/错误/相机小窗/大屏预览；新增 `/screen` 全屏只读大屏（复用 `webui.screen`）。

约束照旧：`/tmp/is_competition.lock` 存在时（比赛运行中）调试台默认拒绝打开相机/串口，`--force` 才强开。适配 1366×768；危险操作二次确认（急停一键直达）。

验收：8090 四节可操作、8088 能完成一整轮、样式一致、离线可用、两个服务均有 API 自动化测试。

## 5. 阶段 3：删除清单（逐条 + 原因）

| 删除项 | 原因 |
|---|---|
| `patch_backup_tray_fixed_1101_144419/` | 补丁备份，无任何引用 |
| `patch_backup_vision1102_1101_154036/` | 补丁备份，无任何引用 |
| `debug/` 整个目录（含 `*_debug/`、`common/`、`*.bat`） | Tk 桌面调试器，已被阶段2 网页替代 |
| `main.py` | Tk 调试启动入口，已被 `./is.sh web` 替代 |
| `deploy/is_debug.sh`、`deploy/intelligentstoring-debug@.service` | Tk 调试台部署单元，随 main.py 一起废弃 |
| `UI/display_ui.py` 本地 Tk 兜底窗口代码 | 被网页大屏 `/screen` 替代；保留 `render_screen` 与 ScreenClient 主链路 |
| `tools/calib_images/`（60 张货物照，非棋盘格） | 先 `cp -a` 到 `dataset/calib_images_20261029/`（`.gitignore` 内）再从仓库移除 |

**未删（需用户决定）**：`vision/models/best_1027_640x480.onnx`（12MB ONNX 回退）。按规格「删模型文件先问用户」。
保留：`best_1027_640x480.bin` + `.json`（板上在用 + 类别名）、`best_1027.pt`（转换源）。

**澄清（规格提示有误）**：`communication/client.py` 的 `_waiting_ack_seq / _ack_event` 经核查在 `_wait_and_ack` 与 `_handle_status` 中**真实读写，是活代码，不是死代码**，故保留、不改逻辑。

文档同步：删除所有指向已删文件（`main.py`/`debug/`/`is_debug`/`debug@`）的段落，涉及 `README.md`、`MODULES.md`、各 `GUIDE.md`、`deploy/OPERATIONS.md`、`start_competition.sh`、`deploy/is_health.sh`、`MessegeCenter/race.py`。

## 6. 目录树前后对比

**重构前（main）**
```
pc_test_stm/
├── main.py                     # Tk 调试总入口
├── debug/                      # Tk 调试器整树
│   ├── communication_debug/  integrated_debug/  message_center_debug/
│   ├── monitor_debug/  vision_debug/  common/  (*.bat)
├── patch_backup_tray_fixed_1101_144419/
├── patch_backup_vision1102_1101_154036/
├── vision/ (camera.py 空转发, detector.py, yolo_detector.py, models/…onnx)
├── tools/ (convert.sh 重复入口, calib_images/ 60 张货物照)
├── communication/ (example_usage.py)
├── deploy/ (is_debug.sh, intelligentstoring-debug@.service, …)
├── UI/ (display_ui.py 含本地 Tk 兜底)
└── MessegeCenter/  moniter/  README.md MODULES.md …
```

**重构后（main）**
```
pc_test_stm/
├── webui/                      # 新增：统一网页包 (base/pages/comm_debug/flow_debug/screen/static/test_webui)
├── is.sh                       # 统一入口：web/start→8090 调试台, race→8088 比赛
├── start_competition.sh        # 比赛一键启动(8088)
├── tools/vision_tool.py web    # 8090 视觉调试台(+四节导航/API)
├── UI/run_page.py              # 8088 比赛页(共享样式 + /screen)
├── vision/ (cameras/, detector.py, yolo_detector.py, models/)
├── communication/  deploy/  MessegeCenter/  moniter/
├── dataset/calib_images_20261029/   # 移入(gitignore)
└── README.md MODULES.md WEB_CONSOLE.md VISION_HANDOVER.md …
```

## 7. 测试结果（第六节 + 补充）

```
python3 -m unittest vision.test_vision MessegeCenter.test_bus moniter.test_monitor \
    communication.test_comm UI.test_ui webui.test_webui
→ Ran 65 tests ... OK   (vision31 + bus4 + monitor3 + comm12 + ui9 + webui6)

# 仿真端到端（合成相机 + MockSTM32）
ALLOW_MOCK=1 USE_SYNTHETIC_CAM=1 START_SOURCES=auto ONESHOT=1 EXPECTED_TOTAL=3 \
    python3 UI/run_competition.py
→ EXIT=0  === 分拣结束 (完成): 成功 3 件, 用时 15.7s ===   (1 号盒 蓝色四面体 x3)

# 网页 smoke(容器内, 合成相机)
tools/vision_tool.py web --camera synthetic --no-auth --port 18090
→ / /static/app.css /static/common.js /comm /flow /run
  /api/comm/state /api/comm/ports /api/flow/state /api/run/state /api/status  全 200

# 语法
bash -n is.sh start_competition.sh convert.sh deploy/*.sh  → 全 ok
grep -rn 'is_debug|intelligentstoring-debug@|python main.py' → NONE
```

## 8. 已知限制与待现场验证

- 网页四节接**真机**、通讯页接**真 STM32**、自检提示是否贴合现场、参考点标定后「点哪去哪」误差、整轮比赛真实节拍——**均只在仿真验证过**，详见 `FIELD_VERIFICATION_CHECKLIST.md`。
- `best_1027_640x480.onnx` 是否删除待用户确认。
- 大屏推送服务（8756，`x5_remote_server.py`）不在本仓库，未改动其接口。

## 视觉优化 A2：发送前越界拦截（限位）

**提交**：`视觉A2:`（commit `76d00c8`；文件：`vision/limits.py`(新) / `vision/gates.py`(新) / `vision/calibration.py` /
`vision/calib_config.json` / `vision/service.py` / `webui/comm_debug.py` / `vision/test_vision_gates.py`(新)）
> `vision/gates.py` 随 A2 一起提交：`calibration.py` / `service.py` 顶层 import 它，缺它 A2 自身 import 会失败。

**机制**　唯一检查函数 `vision/limits.py::check_point(x_mm, y_mm, limits, tray_margin_mm=None,
tray_margin_action="warn", tray_xy=None)`，返回 `LimitVerdict(ok, trigger, axes, reason, warnings, detail)`；
`detail` 恒带三套坐标（像素 `pixel_uv` / 托盘 `tray_xy` / 机械臂 `robot_xy`）与越界轴、阈值、来源。
边界**闭区间** `x_min <= x <= x_max`。三个入口共用它：

| 入口 | 调用点 | 越界行为 |
| --- | --- | --- |
| scan 返回前过滤 | `vision/service.py::_filter_limits` | 目标不进 `scan()` 返回值/总线/记录，`last_scan_rejects` 留档，调试图上红框 `LIMIT` |
| 网页"点哪去哪" | `tools/vision_tool.py::goto()`（A6 handover 后接入） | 拒绝发送 + 页面提示（`{"ok":false,"error":"机械臂限位拦截 (指令未下发): ..."}`） |
| 调试台 `/comm` MOVE | `webui/comm_debug.py::send()` | 拒绝发送 + 返回 `{"ok":false,"error":...}`，协议不变 |

配置：`calib_config.json` 只**增键**（现场值原样保留）`robot_limits_mm`（默认 `null` = 不检查，**唯一硬拦截项**）、
`tray_margin_mm`（默认 `null` = 关闭）、`tray_margin_action`（`warn`/`drop`，默认 `warn`）；
环境变量覆盖 `IS_ROBOT_LIMITS="x_min,x_max,y_min,y_max"`、`IS_TRAY_MARGIN_MM`、`IS_TRAY_MARGIN_ACTION`。
`robot_limits_mm` 写错/缺键时按"未配置"放行并 `logger.warning`（**不静默拦死**：配置打错一个字就抓不到货，
比漏拦一次更糟；未配置由 A3 门禁 `robot_limits` 提示）。`communication/**`、`UI/**` 零改动。

### A2.8 证据：越界帧有没有进发送序列

**正向复现**（人为设一个极小限位，让"正常货物"必然越界）：

```
IS_CALIB_CONFIG=/tmp/a2_calib.json IS_ROBOT_LIMITS="-9999,-9998,-9999,-9998" \
IS_COMPETITION_LOCK=/tmp/a2_e2e_nolock ALLOW_MOCK=1 USE_SYNTHETIC_CAM=1 START_SOURCES=auto \
ONESHOT=1 EXPECTED_TOTAL=3 timeout 150 python3 UI/run_competition.py
```

时间轴（run `records/runs/20261103_140430`，`RUN_EXIT=0`）：

```
14:04:31,072 WARNING vision.service: 视觉限位拦截: 目标#1 蓝色四面体 像素(462.0,225.0) 托盘(128.1,127.3) 机械臂(138.6,72.3) 触发=robot_limits 轴=x,y 原因=x=138.6 超出 [-9999.0, -9998.0]; y=72.3 超出 [-9999.0, -9998.0]
14:04:31,074 WARNING vision.service: 视觉限位拦截: 本次 1 件目标全部越界, 未返回任何目标 (触发=robot_limits) —— 不是空盘, 是限位/标定问题
14:04:32,520 WARNING vision.service: 视觉限位拦截: 目标#1 蓝色四面体 像素(472.0,208.0) 托盘(135.0,121.2) 机械臂(133.3,66.5) 触发=robot_limits 轴=x,y ...
14:04:32,523 WARNING vision.service: 视觉限位拦截: 目标#2 黄色四面体 像素(161.8,262.1) 托盘(24.5,106.4) 机械臂(121.6,161.6) 触发=robot_limits 轴=x,y ...
14:04:32,524 WARNING vision.service: 视觉限位拦截: 本次 2 件目标全部越界, 未返回任何目标 (触发=robot_limits) —— 不是空盘, 是限位/标定问题
14:04:32,527 INFO vision.service: scan: 5 帧 -> 0 件 (取图 167 ms, 识别 156 ms)
14:04:32,541 INFO competition: 托盘无可识别货物, 抖料后重扫 (2/2)
14:04:33,945 WARNING vision.service: 视觉限位拦截: 目标#1 蓝色四面体 像素(483.0,189.0) 托盘(143.0,114.1) 机械臂(127.1,59.6) 触发=robot_limits 轴=x,y ...
14:04:33,963 INFO competition: === 分拣结束 (完成): 成功 0 件, 用时 3.2s ===
```

关键计数（同一份原始日志）：`视觉限位拦截` = **7** 行、`不是空盘` = **3** 行、
`scan: 5 帧 -> 0 件` = **3** 次、UI 空盘行 `托盘无可识别货物, 抖料后重扫` = 2 行、
**`competition: 抓取`（取件命令）= 0 行**、`PERMIT`/`move` = 0 行。
`records/runs/20261103_140430/scan_001..003.json` 全是 `[]`（越界目标没有进返回值 → 没有进记录）；
`report.json`：`total_done=0`、`items=[]`，`events[]` 里带 t=0.31/1.76/3.18 的拦截原文。

**对照基线**（不设限位，同一份现场配置，run `records/runs/20261103_140442`，`RUN_EXIT=0`）：
`视觉限位拦截` = 0；`scan: 5 帧 -> 1 件` = 3；取件命令 3 行：

```
14:04:43,135 INFO competition: 抓取 蓝色四面体 conf=0.60 mm=(138.6,72.3) -> 1 号盒
14:04:50,350 INFO competition: 抓取 蓝色四面体 conf=0.59 mm=(100.5,52.4) -> 1 号盒
14:04:56,232 INFO competition: 抓取 蓝色四面体 conf=0.66 mm=(107.2,70.8) -> 1 号盒
14:04:56,232 ... === 分拣结束 (完成): 成功 3 件, 用时 19.5s ===
```

→ **未配置限位时行为与基线一致**（该发的一件不少发）。保留目标的
`diagnostics.limit_checked/limit_ok/limit_configured` 落进 scan 记录：

```
scan_001.json -> diagnostics={"model_color":"blue","hsv_color":"blue",...,"limit_checked":true,"limit_ok":true,"limit_configured":false,"limit_trigger":"","limit_axes":[]}
```

**单测**（`vision/test_vision_gates.py`，28 tests OK）内含"越界时 `client.send_move` 根本没被调用"
（`client.moves == []`）与"`scan()` 返回不含越界目标且 `last_scan_rejects` 有记录"两条硬断言 ——
不满足只断言"调用了检查函数"。

#### 另外两个发送入口（点哪去哪 / `/comm` MOVE）的端到端证据：上位机到底往串口写了几个字节
本机没有下位机硬件，而 `tools/vision_tool.py --arm` 走 `communication/client.py::connect()`（只扫
`/dev/ttyACM*`/`/dev/ttyUSB*`，**没有** ALLOW_MOCK 回退），所以第一轮测试里 `POST /api/goto`
直接返回 `{"ok":false,"error":"未连接下位机 (启动时加 --arm)"}` —— **拦截函数根本没被走到**。第二轮改用
**PTY 假串口**：`/tmp/a3_pty_bridge.py` 用 `pty.openpty()` + `tty.setraw(slave)` 把 slave 软链成
`/dev/ttyACM9`，master 侧挂仓库自带的进程内 `MockSTM32`（`communication.mock_device`，**不改
`communication/**`**），于是工具真的"连上了下位机"，而上位机写出的每一个字节都被落盘
（`/tmp/a3_pty_<tag>_c2d.bin`），再用 `/tmp/a3_pty_analyze.py` 按 `protocol.py` 的 32B 帧格式
（X/Y 为微米）解出命令帧。**越界时字节数应为 0**：

| 用例 | 入口 | 限位 | 上位机发出的原始字节 | 命令帧 / MOTION | HTTP 响应 |
| --- | --- | --- | --- | --- | --- |
| `goto_reject` | `POST /api/goto (462,225)` | `-9999,-9998,-9999,-9998` | **0 B** | **0 / 0** | `{"ok":false,"target":[126.4,63.4],"error":"机械臂限位拦截 (指令未下发): x=126.4 超出 [-9999.0, -9998.0]; y=63.4 超出 … \| 触发=robot_limits 轴=x/y \| 像素(462.0,225.0) 托盘(?) 机械臂(126.4,63.4)"}` |
| `goto_pass`（对照） | 同上 | `-9999,9999,-9999,9999` | 96 B | 3 / **1** | `{"ok":true,"target":[126.4,63.4],"reached":[126.4,63.4],"err_mm":0.04}` |
| `comm_reject` | `POST /api/comm/send move x=500 y=500` | `-9999,-9998,-9999,-9998` | **0 B** | **0 / 0** | `{"ok":false,"cmd":"move","error":"坐标越界, 已拒绝发送 (x=500.0, y=500.0): …"}` |
| `comm_pass`（对照） | 同上 `x=126.4 y=63.4` | `-9999,9999,-9999,9999` | 32 B | 1 / **1** | `{"ok":true,"cmd":"move"}` |

两个对照用例解出的运动帧原文（`/tmp/a3_goto_analyze.txt`）：

```
== goto_pass     原始字节 96 B;  命令帧 3 个
   [CLEAR   ] off=0      seq=11926 task_id=0
   [MOTION] off=32     task_id=201  目标=(126.440, 63.400)mm timeout=10000ms
   [CANCEL  ] off=64     seq=11928 task_id=201
== comm_pass     原始字节 32 B;  命令帧 1 个
   [MOTION] off=0      task_id=0    目标=(126.400, 63.400)mm timeout=10000ms
== goto_reject   原始字节 0 B;  命令帧 0 个   --> 上位机一个运动指令都没发出去
== comm_reject   原始字节 0 B;  命令帧 0 个   --> 上位机一个运动指令都没发出去
```

即：越界时**上位机连 CLEAR/POLL 都没写出去，`send_move` 一行字节都不存在**；而同一入口在范围内时
确实会下发 MOTION（`task_id=201` 是点哪去哪的任务号、`task_id=0` 是 `/comm` 手动点动），证明拦截的
是"发送"而不是"把入口整个弄坏"。原始输出：`/tmp/a3_goto_evidence.txt`（HTTP 响应）、
`/tmp/a3_goto_analyze.txt`（帧解析）、`/tmp/a2_goto_evidence.txt`（合并版）、
`/tmp/a3_tool_pty_{goto,comm}_{reject,pass}.log`（工具日志）。

### A2 现场历史记录（11-02 01:06 "参数超限"）的检索结论

**结论：无法确认当时被下位机拒绝的是 XY 坐标越界还是速度参数越界。** 依据：
1. `records/runs/` **没有 01:06 那次 run**（现有：20261101_162657/163122/203540/210733、
   20261102_003832/024631/024718/024845、20261103_…）；任务书提到的时间轴没有对应 run 目录。
2. 11-02 的 26 个 `scan_NNN.json` 键集合 = `['bbox','category','conf','label','u','v']`，**没有任何 mm 坐标**。
3. `/var/log/intelligentstoring/competition.log`：`grep '2026-11-02'` = 0 行、`grep '01:06:1'` = 0 行；
   该日志里 `参数超限` 全部在 **11-01**：16:27:20 任务3 / 16:27:22 任务4 / 16:27:32 任务6 /
   16:27:43 任务8 / 16:31:37 任务2 / 16:31:52 任务4（另有 16:27:10、16:31:36 `任务 1 失败: 前往取件点超时`）。
4. 上表正向复现只证明"新拦截在越界时确实生效"，**不能**反推当时也能拦住。

**边界声明（本项不做的事）**：本项只保证**上位机不把越界目标放进发送序列**；
**不声称**修复下位机驱动器故障、固件 `参数超限`（坐标/速度）判定、`ACM0 -> ACM1` 重枚举这三类现场问题。

**现场待验证**：`[现场待验证]` 真实托盘坐标系下的 `robot_limits_mm` 究竟该填多少（本机从未在实机上跑过 A2 的
拦截路径，PTY 假串口只证明了协议层"越界不发帧"）；`tray_margin_mm` 取多少合适；真机串口上
`/comm` MOVE 与"点哪去哪"的越界提示在浏览器里的实际观感。

---

## 视觉优化 A3：自检 + precheck 门禁

**提交**：`视觉A3:`（文件：`is.sh`、`start_competition.sh`、A6 handover 后的 `tools/vision_tool.py`，
以及把本节追加到 `REFACTOR_REPORT.md` 末尾）
> `vision/gates.py` 与 `service.py` 的门禁 hunk 已随 A2 提交（无法在文件内拆分）。

**11 条门禁**（`vision/gates.py::evaluate() -> GateReport`，每条有稳定 id / 级别 / 详情含数值与来源路径）：

| id | 判据 | 调试缺省 | 比赛缺省 |
| --- | --- | --- | --- |
| ref_points | 参考点 <3；或**缺 `ref_points_fp` 指纹=按未标定**；或指纹≠当前=已过期 | warn | fail |
| ref_collinear | 三点共线（三角面积比 / 最小内角，阈值 0.02 / 5°） | warn | fail |
| fit_quality | 拟合比例出 0.97–1.03、平均误差 >2mm、单点最大 >4mm | fail | fail |
| residual_poly | 残差多项式反而变大 → **自动不用残差** + warn（已真的生效：标定侧不再 apply） | warn | warn |
| class_height | ≥80% 的类 `height_mm` 相同（占位值） | warn | warn |
| imaging_locked | `camera.exposure_us`/`gain_db`/`wb_kelvin` 有 null（详情列出哪几个） | warn | fail |
| intrinsics | 无 `camera_matrix`/`dist_coeffs` | warn | warn |
| color_imaging | `color.source=="hsv"` 但缺 `hsv.imaging_fp` 或与当前成像指纹不一致 | warn | fail |
| robot_limits | `robot_limits_mm` 未配置 | warn | warn |
| pose | `estimate_pose()` 返回 `ok=False` | fail | fail |
| cam_self | 焦距是托盘四角自估（`source=="self"`） | warn | warn |

配置：`calib_config.json` 顶层 `gates: {"<id>":"off|warn|fail"}`（缺省 = 上表，按 `race_mode()` 选列；
单条可覆盖；`IS_GATE_<ID>` 再覆盖；非法值 warning 后回落缺省）。阈值在顶层 `gate_thresholds`
（`IS_GATE_TH_<KEY>` 可覆盖）。

**指纹（D1：只比指纹，不比时间）**：`ref_points_fp` = sha1 前 12 位，输入 = 托盘四角 + 内参
（`camera_matrix`/`dist_coeffs`）+ 分辨率 `image_size` + `tray.fixed_z_mm` + `tray.axes` + `tray.origin_mm`
（后三项同样改变像素→机械臂映射）。写者是 `vision/calibration.py`：`set_tray_ref_points()` 存参考点时自动盖章，
现场也可 `python3 -m vision.gates --seal` / `./is.sh gates --seal`。`ref_points_verified_at` 只显示，不参与判定
（板上可能无 RTC）。

**成像指纹 `hsv.imaging_fp` 的写入者（A6 handover 后接入）**：`color_imaging` 门禁判"颜色区间是不是在
当前成像下调出来的"，所以写入者与判定必须**同一个函数**。`tools/vision_tool.py::color_solve()`（颜色页采样
成功、写 `classes.json` 之前）新增：

```python
from vision import gates as _gates
cam = dict(getattr(svc, "_cam_cfg", None) or getattr(svc.cfg, "camera", {}) or {})
new["imaging_fp"] = _gates.imaging_fp(cam)      # = sha1(exposure_us|gain_db|wb_kelvin|W,H|device|fourcc|swap_rb)[:12]
```

即 `vision/gates.py::imaging_fp()` 同时用于写与读（`_g.check_color_imaging` 比对
`gates.imaging_fp(vcfg.camera)`），口径一致；采样当时是什么曝光/增益/白平衡就记什么。几何那套
（四角+内参+分辨率+`fixed_z_mm`+`axes`+`origin_mm`）是另一个指纹 `ref_points_fp`，两者不混。
工具自带 `except Exception` 兜底：指纹算不出来只 warning，颜色区间照旧保存（不让辅助信息毁掉主流程）。
`classes.json` 现在的**仓库现场版本仍然没有 `imaging_fp`**（所以验收 (a) 才会报"颜色未标定"）；
只有现场在颜色页采一次样之后它才会出现 —— 这正是该门禁要捕捉的状态。

### A3 验收实测（原始输出 `/tmp/a3_evidence.txt` 24.5KB）

**(a) 用当前仓库 `calib_config.json` 跑自检** → `ok=False`，**problems 11 条**（不是"无"），要求的 6 项全命中：
`参考点`（有 3 点但缺 `ref_points_fp`，当前应为 `622ef169ba5f`）/`颜色标定`（缺 `imaging_fp`）/
`类别高度`（23/23=100% 都是 30.0mm）/`成像锁定`（三项 null）/`相机内参`（无 `camera_matrix`）/`限位未配置`。
另 5 条为：`相机无图像`（自检时没开相机）、`标定重投影误差 2.8mm 偏大`、`拟合质量`（平均 2.83mm>2.0、单点最大 4.23mm>4.0）、
`残差多项式`（2.834mm→2.834mm 无收益，已自动停用）、`托盘边距未配置`。
门禁命令：调试模式 `通过 3 / 提醒 7 / 拦截 1`（唯一 FAIL=`fit_quality`）exit=1；比赛模式 4 条 FAIL
（ref_points/fit_quality/imaging_locked/color_imaging）exit=1。
> 口径说明：现场 `ref_points` 其实已有 3 个 manual 点，但 `calib_config.json` 里**没有 `ref_points_fp`** →
> 按"指纹缺失 = 未标定/过期"处理（否则四角换过、参考点没重标也会被当成"已标定"）。
> `fit_quality` 的 2.83mm 是现场四点与三点参考点几何不自洽造成的（单点最大 4.23mm），如实按 fail 报出。

**(b) 比赛模式拒绝启动**（我自己的锁 `/tmp/a3_race.lock`，绝不碰共享锁）：
真实入口 `python3 UI/run_competition.py` → **exit=1**，traceback：
`UI/run_competition.py:282 boot → self.vision.start()` → `vision/service.py:170 start → self._enforce_gates()`
→ `service.py:437 raise RuntimeError(msg)`，消息里逐条列出 4 条 FAIL。服务层同锁下
`gate_report().race=True`、`start()` 抛 `RuntimeError`、`is_running=False`、`_grabber=None`（**拒绝时不去开相机**）。
`start_competition.sh`（容器内实测）：建锁 → `[比赛模式] 视觉门禁不通过 (退出码 1), 上面列出的 FAIL 项必须先处理 -> 不启动`，
**`SCRIPT_EXIT=1` 且没有进入 while 重启循环**（日志里没有"2 秒后自动重启"）。
**删锁 → **`exit=0`，正常跑完 3 件 19.7s。

**(c) 全达标配置必须放行**：`/tmp/fullpass/{calib_config.json,vision_config.json,classes.json}`（现场配置副本，
只增改门禁要判的项：内参、`robot_limits_mm={0,240,0,240}`、与 axes/origin 几何自洽的三点参考点+盖章、
`exposure_us/gain_db/wb_kelvin`、`color.imaging_fp`、逐类不同 `height_mm`），用
`IS_VISION_CONFIG`/`IS_VISION_CLASSES`/`IS_CALIB_CONFIG` 指向它们（仓库现场文件零改动）→
`python3 -m vision.gates --check` **exit=0**（比赛模式 `通过 10 / 提醒 1 / 拦截 0`，唯一 warn 是
`residual_poly`："只有 3 个参考点、残差本就没启用"），真实 `UI/run_competition.py` **exit=0**、成功 3 件。
`VisionService` 新增 `IS_VISION_CLASSES` 支持（构造 detector 时透传 `meta_path`，不改 `yolo_detector.py`）。

**(c2) 现场盖章流程**：`--seal` 前 ref_points=warn（缺指纹）→ `--seal` 写入 `ref_points_fp=622ef169ba5f` →
之后 ref_points=ok（"参考点 3 个, 指纹 622ef169ba5f 与当前配置一致(确认于 …)"）。现场就是 `./is.sh gates --seal`。

**(d) 每条门禁可单独 `off|warn|fail`**（隔离实测：其余门禁全部 `off`，只留被测那条；现场配置 + 比赛模式）：

| 门禁 | off | warn | fail |
| --- | --- | --- | --- |
| ref_points | off / exit 0 | warn / exit 0 | fail / **exit 1** |
| fit_quality | off / exit 0 | warn / exit 0 | fail / **exit 1** |
| residual_poly | off / exit 0 | warn / exit 0 | fail / **exit 1** |
| class_height | off / exit 0 | warn / exit 0 | fail / **exit 1** |
| imaging_locked | off / exit 0 | warn / exit 0 | fail / **exit 1** |
| intrinsics | off / exit 0 | warn / exit 0 | fail / **exit 1** |
| color_imaging | off / exit 0 | warn / exit 0 | fail / **exit 1** |
| robot_limits | off / exit 0 | warn / exit 0 | fail / **exit 1** |
| ref_collinear / pose / cam_self（本身达标） | off / exit 0 | ok / exit 0 | ok / exit 0 |

即：**拦截只发生在"条件不达标 且 级别=fail"**；`off` 一律不参与，`warn` 只提示不改退出码。

**(e) 指纹过期**：改 `corners` / `camera_matrix` / `dist_coeffs` / `image_size` / `fixed_z_mm` / `axes` /
`origin_mm` 各一次（副本上）→ **7/7 全部判"参考点已过期"**，指纹从 `9dab9f6bfb4b` 变为 7 个互不相同的新值。

### A3 启动脚本门禁路径（容器内真机实测原文）

`start_competition.sh` 的门禁块在 `while true` **之前**（第 46–61 行），容器内实测（原始输出 `/tmp/a3_script_evidence.txt`）：

```
$ docker exec intelligentstoring bash -lc 'cd /app && touch /tmp/a3_race.lock && IS_COMPETITION_LOCK=/tmp/a3_race.lock ./start_competition.sh'
BPU 运行库: 可用
执行页: http://10.68.246.162:8088
视觉门禁 (比赛模式, 共 11 项): 通过 3 / 提醒 4 / 拦截 4
[FAIL] ref_points       参考点未标定: 有 3 个参考点但 calib_config.json 缺 ref_points_fp 指纹, ... 当前应为 622ef169ba5f ...
[FAIL] fit_quality      平均重投影误差 2.83mm > 2.0mm; 单点最大误差 4.23mm > 4.0mm
[FAIL] imaging_locked   成像未锁定: exposure_us, gain_db, wb_kelvin 为 null ...
[FAIL] color_imaging    颜色未标定: 有 hsv 区间但缺 imaging_fp ...
比赛模式 (锁文件存在): 有拦截项就必须停下来, 不要带着 4 项 fail 开赛
[比赛模式] 视觉门禁不通过 (退出码 1), 上面列出的 FAIL 项必须先处理 -> 不启动
           现场: ./is.sh gates        看门禁 11 项
                 ./is.sh gates --seal 给当前参考点盖章
SCRIPT_EXIT=1                      # 且日志里没有任何 "2 秒后自动重启" -> 确认没有进 while 循环
```

删锁后同一脚本（`ONESHOT=1 EXPECTED_TOTAL=3 ALLOW_MOCK=1 USE_SYNTHETIC_CAM=1`）放行：

```
2026-11-03 14:12:26,050 INFO competition: ✔ 第 3 件 蓝色四面体 完成, 用时 6.2s
2026-11-03 14:12:26,054 INFO competition: === 分拣结束 (完成): 成功 3 件, 用时 19.7s ===
[比赛模式] 程序正常退出
SCRIPT_EXIT2=0
```

`./is.sh gates`（宿主入口，实际进容器跑 `python3 -m vision.gates --check`）与 `./is.sh gates --seal`
（在容器内副本上盖章 `ref_points_fp=622ef169ba5f` 后该门禁转 `ok`）同样实测通过；现场
`vision/calib_config.json` 的 `ref_points_fp` 仍**不存在**（我的所有跑测都走 `/tmp` 副本）。

### 取舍与说明

- **`start_competition.sh` 门禁失败不丢进 while 重启循环**：门禁不过 = 标定/成像/限位没配好，
  2 秒一次无限重启只会刷屏，并把"现场真正要做的动作"（参考点盖章 / 锁曝光增益白平衡 / 填限位）淹没。
  现在直接打印失败项并 `exit 1`，修好后重跑脚本即可。门禁自身异常（非零但不是配置问题）时按锁文件判据：无锁放行、有锁拦。
- **不改 `UI/**`**：拒绝启动通过 `VisionService.start()` 抛异常实现，`run_competition.py` 不动；
  `UI/precheck.py` 的退出码只由 `level=="error"` 决定，门禁项文本含"标定/相机"等关键词 → 走它既有的分组，
  其余兜底进"视觉配置"(warn)，**不会**产生新的 error。现场看门禁用 `./is.sh gates`（11 项全列）。
- **`python3 -m vision.gates` 有一行 `RuntimeWarning: 'vision.gates' found in sys.modules ...`**：
  `vision/__init__.py` 会导入 calibration → gates，属无害噪音，不影响退出码（已实测 exit 0/1 正确）。

**现场待验证**：`[现场待验证]` 实机 `ref_points_fp` 盖章后的数值、以及曝光/增益/白平衡锁定量；
`class_height` 逐类高度真值只能回实验室量；颜色页采样后写出的 `hsv.imaging_fp` 在实机照明下
是否一次就与 `vision_config.json` 的 camera 段一致（本机是用合成相机验证的读写同源）。

---

## 视觉优化（A1–A8）总述

本节按《视觉优化执行计划（4）》(v5, 2026-11-03) 的 A 阶段逐项收口。基线 `17d8c98`，全部改动在
`视觉A*:` 前缀的 commit 里，**未改 `communication/**`，`UI/**` 只动了 `UI/run_report.py`（只加字段）**，
`vision/interfaces.py` 与 `VisionTarget` 原 16 字段名/语义一字未动（只在末尾新增
`diagnostics: dict = field(default_factory=dict)`）。

**原则**：所有阈值/开关都进配置文件并支持 `IS_*` 环境变量覆盖，没有写死在代码里；
现场没实测过的一律标 `[现场待验证]`，报告里不出现编造的实测数字。

### A1 category 第一段取错（形状位置放的是带颜色的模型类名）
- **问题**：`_build_target()` 用 `shape = meta.get("shape", d.name)`，而 `classes.json` 只有 `shape_cn`
  → 取到模型类名。现场证据：`records/runs/20261102_024845/report.json` 的
  `"category": "黄色四面体|blue|"`、`records/runs/20261102_024718/scan_003.json` 的 `"黄色四面体|red|"`。
- **改了什么**：`vision/yolo_detector.py` 改为 `meta["shape_cn"] or meta["shape"] or d.name`（23 类的
  category 第一段现在都等于 `shape_cn`）；新增 `model_color_of()` 与 `color_source` 读取；
  `classes.json` 23 类全部补 `model_color`（现场 HSV 标定值逐字节未动，`color` 仍为 null）；
  `diagnostics` 记 `{model_color, hsv_color, color_source}`；调试台冲突用洋红标注；
  `vision/service.py` 新增 `_apply_color_conflict()`：记忆键 `shape|model_color|hsv_color`（**不用位置**）、
  TTL 60s、比赛模式同一组合首次把置信度压到 `conflict_conf` 触发一次抖料重扫、第二次起采信 HSV。
- **新增配置（默认）**：`color.source="hsv"`、`color.conflict_ttl_s=60`、`color.conflict_conf=0.43`。
- **怎么关**：把 `color.source` 设成 `model`（应急：颜色直接用 `model_color`，换颜色即失效）；
  不想压置信度就把 `color.conflict_conf` 设 `≥ COMMIT_CONF(0.45)`（不做抖料，只告警）；
  整套冲突提示在调试模式（无锁文件）本来就只 WARNING。
- **有意行为变化**：`UI/planner.py:_closest_slot()` 用 `category.split("|")[0]` 做同形状归并，
  修好 shape 后"同形状不同颜色"现在会归到同一槽位（改前不会）。已写进本报告，未改 planner。
- **证据**：`/tmp/A1_evidence.txt`、`/tmp/A1_unittest.txt`（vision 40 OK）、
  `/tmp/A1_e2e.txt`（`新类别 tetrahedron|blue|`、`颜色冲突: 类=黄色四面体 … 键=tetrahedron|yellow|red`）。

### A1.1 冲突率双口径（D4）
- **改了什么**：`UI/run_report.py` 增 `data["color_conflict"]`，两个口径分开算：
  ①`by_detection`＝按"检测到的目标"计（在 `save_scan_image()` 累计）；②`by_piece`＝按"实际抓取的件"计
  （在 `item(result="ok")` 用 `_match_piece_target()` 先按 `|du|+|dv| <= 2.0` 匹配该件被抓走前最后一次
  scan 的目标名册，退化再按 category/label 就近），并附 `note` 说明两个口径的差别。
- **新增配置（默认）**：`color.conflict_ttl_s=60`、`color.conflict_conf=0.43`（`IS_CONFLICT_TTL_S` /
  `IS_CONFLICT_CONF` 可临时改）。
- **怎么关**：同 A1（`conflict_conf ≥ 0.45` 时两份计数都恒为 0 冲突）。
- **证据**：e2e 实测 `by_detection {scans 3, targets 4, conflicts 1, rate 0.25}`、
  `by_piece {pieces 3, conflicts 0, unmatched 0}`（4/25 分之差正是"同一件被多帧重复记"的口径差）。
- 顺带清理：`IS_RACE` 措辞清除（`grep -rn IS_RACE` 排除 `records/` 后零命中），比赛判据统一走
  `vision.config.race_mode()` + 锁文件。

### A4 信息维度 + 二维码加强
- **改了什么**：`classes.json` 每类可选 `info{qr,text,template_set,decode_qr}`（兼容旧的顶层
  `decode_qr`，`info.decode_qr` 优先）；`vision_config.json` 新增顶层 `require_info`；`_decode_qr` 重写：
  删掉 `cv2.wechat_qrcode_WeChatQRCode()`（属 opencv-contrib，板上没有），主路径 `cv2.QRCodeDetectorAruco`
  → 回退 `cv2.QRCodeDetector`，实际解码器如实写 `diagnostics.qr_decoder`(aruco/qrcode/none)；
  流程为 框内裁剪 → {1×,2×,3×} × {原图, OTSU, 自适应阈值, CLAHE} 命中即停；
  `category_core`（`形状|颜色|`）每个目标都有（含 HSV 兜底路径）；
  `UI/run_report.py` 的 `scan_NNN.json` 同时写 `category_core` 与 `category`，`finish()` 汇总
  `category_core_counts`。
- **新增配置（默认）**：`require_info=false`（`IS_REQUIRE_INFO`）、`detect.qr_budget_ms=500`
  （按**整次 scan** 计，`IS_QR_BUDGET_MS`）、`detect.require_info_conf=0.43`（`IS_REQUIRE_INFO_CONF`）。
- **怎么关**：`require_info=false` 时行为与优化前逐字段一致（有回归断言：`info==""`、
  `category=="形状|颜色|"`、无 `info_missing`）；`detect.qr_budget_ms=0` 则本次 scan 完全不解码。
- **未编造**：`classes.json` 本项**没改**（现场是否贴码、码里是什么内容没有实测 → 不填假值），
  `vision/GUIDE.md` §7.2 只写填写格式并标 `[现场待验证]`。
- **证据**：`/tmp/A4_unittest.txt`（vision 54 OK，`TestInfoAndQr` 14/14）、`/tmp/A4_e2e.txt`。

### A4.1 解码顺序按置信度 + HSV 兜底的信息语义
- **改了什么**：新增 `_qr_plan(frame, dets)`——**只把配了 `decode_qr` 的目标当候选**，按 `(-conf, 原序)`
  排序依次解码，预算整次 scan 共享、命中即停；`_build_target(..., qr_override=None)` 新增可选参数，
  返回顺序/`target_id`/`hits` 不变。HSV 兜底路径：`require_info=true` 时 info 为 `unknown`、
  category 仍是三段、置信度 ≤ `require_info_conf`（会触发抖料）；`false` 时与基类逐字段相同。
- **怎么关**：同 A4。
- **为什么**：这两条是计划原文的明确要求（line 172 与 line 99/D4），verifier 在 A4 首次提交时
  独立发现未实现（反例：先解 conf=0.30 的目标、conf=0.95 的反而 `attempted=False`），本项补齐。

### A5 多帧融合匹配半径自适应
- **改了什么**：`detect_multi()` 的固定半径改为 `max(match_px_min, match_ratio × min(框宽, 框高))`；
  匹配保持**类无关**（"同类优先"只作近距离 tie-breaker，不作硬过滤）；`min_hits` 语义不变。
- **新增配置（默认）**：`scan.match_ratio=0.35`、`scan.match_px_min=12`（`IS_MATCH_RATIO` /
  `IS_MATCH_PX_MIN`）；`match_ratio <= 0` 时退回旧的固定 `match_px`（默认 25）。
- **怎么关**：`scan.match_ratio: 0`（完全回到旧行为）。
- **证据**：`/tmp/A5_unittest.txt`、`/tmp/A5_e2e.txt`；verifier 独立复现 80px 框→28.0px、300px→105.0px、
  20px→12.0px、ratio=0→25.0px；中心距 60px/框 80px 保持 2 目标；15px 抖动合并为 1；
  5 帧中 2 帧 A / 3 帧 B → 取多数 B。

### A6 / A6.1 托盘白基准颜色归一化（默认关）
- **改了什么**：每次 scan 从托盘内框取「低饱和 `S <= hsv.white_s_max` + 高亮度 `V >= hsv.white_v_min`
  + 不在任何检测框内」的像素，自实现白斑法（约 30 行，**不引入 scipy/新依赖**）算三通道增益，
  限幅后**只乘在颜色投票前的裁剪图上**；送模型的图与 GrabCut 精定位用的原图逐像素不动（有断言）。
  增益 = 白斑三通道均值 / 各通道均值，限幅 `wb_gain_min~wb_gain_max`，白斑像素不足则沿用上次增益
  （`reason=too_few_pixels`），托盘找不到就跳过（`no_tray`，正常出目标）。
- **新增配置（默认）**：`color.tray_white_balance=false`、`wb_gain_min=0.7`、`wb_gain_max=1.4`、
  `wb_min_pixels=200`（`IS_TRAY_WB` / `IS_TRAY_WB_GAIN_MIN|MAX|MIN_PIXELS`，A6.1 补的后三个）。
- **怎么关 / 零影响**：默认就是关；关掉时不取样、不写任何 `wb_*` 诊断键，`scan()` 输出与 A5 树
  逐字段一致（单测 JSON diff 断言，剔除 timestamp）。
- **⚠ 限幅可达性（实测口径，必须一起看）**：默认 `hsv.white_s_max=45` 只允许白斑三通道差落在约 ±17.6% 内，
  真实增益天然只有约 `[0.94, 1.14]`；`0.7~1.4` 的极端值只在人为造偏色时出现（离场实测 `[1.105,1.0,0.913]`、
  极端造偏色被夹到 `[0.7,1.367,1.4]`、白斑像素 14862）。**要让限幅真正起作用必须同时放宽
  `hsv.white_s_max`**（代价：有色件会被当白斑），否则"限幅 0.7~1.4"是空承诺。已写进
  `vision/GUIDE.md` §7.4 与 `FIELD_VERIFICATION_CHECKLIST.md` 补充项 3。
- **证据**：`/tmp/A6_unittest.txt`、`/tmp/A6_e2e.txt`、`/tmp/A6_1_evidence.txt`；verifier 在
  `f033dcc`/`ed15b93` 树上独立复现全部断言。
- **`[现场待验证]`**：0.7~1.4 限幅、`wb_min_pixels=200`、"白斑三通道均值"这个目标亮度都是经验值；
  现场光照稳定后更推荐直接锁 `camera.exposure_us/gain_db/wb_kelvin`（门禁 `imaging_locked`）而不是依赖本功能。

### A7 / A7.1 模型转换部署门禁
- **改了什么**：部署判定抽成纯函数 `decide(cfg, metrics) -> DeployDecision`；阈值优先级
  内置默认 < `tools/convert_config.json` < 环境变量 < 命令行。**修掉了真正的风险**：原 `main()` 的
  step4/step5 会**立刻** `sync_classes()` + `retire_old_models()` + 覆盖 `onnx_dst`
  （640x480 相机下恰好与在用的 `best_1027_640x480.onnx` 同名），判定不通过时会留下
  "旧模型已移走、新模型没上线"的空档；现在所有写入推迟到判定通过之后，候选 `.bin` 先落
  `tools/build/<prefix>/new.bin`。校准图上限 50→200，有 `dataset/hard_cases` 时按类轮询优先抽取、
  最少 20 张保留；余弦 <0.99 或 BPU/ONNX 一致率 <0.85 默认**不部署**。
- **新增配置（默认）**：`tools/convert_config.json` 的 `calib_max_images=200`、`calib_min_images=20`、
  `cosine_min=0.99`、`consistency_min=0.85`、`hard_cases_dir=dataset/hard_cases`、
  `regression_dir=dataset/regression`（`IS_CALIB_MAX` / `IS_CALIB_MIN` / `IS_COSINE_MIN` /
  `IS_CONSISTENCY_MIN`）。
- **怎么关**：只对"不部署"这一条来说，`--force` 绕过余弦/一致率（但**不绕过** `.bin` 装载失败）；
  `--dry-run` 全程只读。
- **不部署时的可见行为**：退出码 **3**、打印 `仍在使用上一版模型: <path>`、
  `vision/models/` 一个文件都不动、不产生 `models/old/`。
- **证据**：`/tmp/a7_evidence.txt`（179 行）：`best_1027_640x480.bin`(sha256 `7b2f4c80…`, mtime
  2026-10-29 16:34:31, 3412884 B) / `.onnx`(`d8c91377…`, 16:19:38, 12091246 B) / `.json`(`8c66238d…`)
  在判定前后逐字节相同，`cv2.dnn.readNetFromONNX` 仍成功（229 层 23 类）；
  6 组 dry-run（余弦 0.98→不部署、0.995+一致率 0.90→部署、0.10+`--force`→部署、一致率 0.80→不部署、
  `IS_COSINE_MIN=0.5`→部署、`=0.999`→不部署）。测试 `tools.test_convert_model` 39 例。
- **`[现场待验证]`**：`--assume-cosine/--assume-consistency` 是模拟值（板上无 ultralytics/hb_mapper，
  真值要等一次真编译）；`dataset/hard_cases`、`dataset/regression` 板上**不存在**
  （`dataset/` 只有 `calib_images_20261029/` 60 张），两条分支已实现+单测覆盖"目录不存在也照常工作"
  但无真跑；回归项还依赖 onnxruntime（板上缺，走优雅降级）。
- 已知小瑕疵（保留现状）：`load_remote_cfg()` 会把 `convert_config.json` 的所有非 `_` 键读进远端配置
  dict 再全量写回，新门禁键被一并搬进远程配置（值不变、无害）。

### A8 抓取高度语义
- **改了什么**：明确 `_build_target()` 传给 `pixel_to_robot(u,v,h)` 的 h 是**投影平面高度**
  （不是下潜深度/夹持高度）。优先级：`classes.json` 的 `proj_h_mm`（填了直接用） >
  `thin_wall ? parallax_factor_thin` > `height_mm × parallax_factor`；`diagnostics` 记实际用的
  `proj_h_mm` 与实际 `parallax_factor`。`vision/GUIDE.md` 新增 §7.5 写清语义（老经验值 0.6 标 `[现场待验证]`）。
- **新增配置（默认）**：`detect.parallax_factor_thin=null`（`IS_PARALLAX_FACTOR_THIN`）；
  `classes.json` 可选 `proj_h_mm`。
- **怎么关**：不填 `proj_h_mm`、`parallax_factor_thin` 留 null 时，行为与优化前一致（`height_mm × parallax_factor`）。
- **未编造**：`classes.json` 未改（现场没有实测高度 → 不填假值，23 类仍都是占位 30mm，门禁 `class_height` 会报）。
- **证据**：`/tmp/A8_unittest.txt`（vision 84 OK，含 6 例 `TestProjectionHeight`）、`/tmp/A8_e2e.txt`。

### 视觉优化新增配置总览（默认值 / 怎么关闭）

| 配置文件 | 键 | 默认 | 关闭 / 退回旧行为 |
|---|---|---|---|
| `vision_config.json` | `color.source` | `"hsv"` | 设 `"model"`（应急） |
| | `color.conflict_ttl_s` / `color.conflict_conf` | `60` / `0.43` | `conflict_conf ≥ 0.45` → 不压置信度 |
| | `color.tray_white_balance` / `wb_gain_min` / `wb_gain_max` / `wb_min_pixels` | `false` / `0.7` / `1.4` / `200` | 默认就是关（关掉零影响） |
| | `require_info` | `false` | 默认就是关 |
| | `detect.qr_budget_ms` / `detect.require_info_conf` | `500` / `0.43` | `0` = 本次 scan 不解码 |
| | `detect.parallax_factor_thin` | `null` | 留 null = 与旧行为一致 |
| | `scan.match_ratio` / `scan.match_px_min` | `0.35` / `12` | `match_ratio: 0` → 用固定 `match_px` |
| `calib_config.json` | `robot_limits_mm` | `null`（不检查） | 不填 = 不拦截（调试够用，比赛门禁会提醒） |
| | `tray_margin_mm` / `tray_margin_action` | `null` / `"warn"` | 不填 = 不做托盘外拦截 |
| | `gates.<id>` | 未写＝调试 warn / 比赛 fail | 每项可单独 `off`/`warn`/`fail` |
| | `gate_thresholds` | 计划默认（2mm/4mm/0.97~1.03…） | 现场超差又暂时重标不了时显式放宽 |
| | `ref_points_fp` | 无（= 未标定） | `./is.sh gates --seal` 盖章写入 |
| `classes.json` | `model_color` | 已按现有 23 类填 | 仅冲突检测用，不决定 `category` |
| | `info{...}` / `proj_h_mm` | 未填 | 不填＝不启用信息维度 / 用 `height_mm × factor` |
| `tools/convert_config.json` | `calib_max_images` / `calib_min_images` / `cosine_min` / `consistency_min` | `200` / `20` / `0.99` / `0.85` | `--force` 绕过余弦/一致率 |

### 最终验收（Lead 在最终 HEAD 上实测，原始输出 `/tmp/lead_final_acceptance.txt`、`/tmp/lead_final_acceptance2.txt`）

1. **八个 unittest 模块**：`vision.test_vision` OK / `vision.test_vision_gates` Ran 28 OK /
   `MessegeCenter.test_bus` Ran 4 OK / `moniter.test_monitor` Ran 3 OK / `communication.test_comm` Ran 12 OK /
   `UI.test_ui` Ran 9 OK / `webui.test_webui` Ran 6 OK / `tools.test_convert_model` Ran 39 OK。旧测试一个没删。
2. **现场配置自检**：`ok = False | problems = 11`，包含计划要求的 6 项（参考点未标定缺 `ref_points_fp`、
   颜色未标定缺 `imaging_fp`、类别高度 23/23 都是 30.0mm、成像未锁定 `exposure_us/gain_db/wb_kelvin=null`、
   无 `camera_matrix/dist_coeffs`、机械臂限位未配置），**不是"问题=无"**。
3. **门禁两级**：无锁（调试）`通过 3 / 提醒 7 / 拦截 1`、退出码 1 但只打印不阻止；
   有锁（比赛）`通过 3 / 提醒 4 / 拦截 4`（`ref_points`/`fit_quality`/`imaging_locked`/`color_imaging`）。
4. **有锁拒绝启动**：真实 `python3 UI/run_competition.py` → `LOCKED_EXIT=1`，traceback 落点
   `UI/run_competition.py:282 boot() → vision/service.py:170 start() → _enforce_gates() →
   RuntimeError: 比赛模式 (存在锁文件 /tmp/is_competition.lock) 视觉门禁不通过, 拒绝启动: …`；
   删锁后同一条命令 `PIPE_EXIT=0`，`成功 3 件, 用时 19.5s`，`1 号盒: 蓝色四面体 x3`。
5. **全达标配置放行**（`/tmp/fullpass/{vision_config,calib_config,classes}.json` + 锁）：
   `gates --check` → `通过 10 / 提醒 1 / 拦截 0`、退出码 0；真实入口 `FULLPASS_UI_EXIT=0`、
   `成功 3 件, 用时 19.6s`。
6. **每项门禁可单独 `off|warn|fail`**（现场配置副本 + 锁）：8 条不达标项全设 `off`→exit 0、
   全设 `warn`→exit 0、全设 `fail`→exit 1；**本身达标的 3 项（`ref_collinear`/`pose`/`cam_self`）设 `fail`
   仍 exit 0**（不会误拦）；把唯一 warn 项 `residual_poly` 升级成 `fail` 才正确拦截（exit 1）。
7. **指纹只比内容不比时间**：全达标配置改四角 → `[FAIL] ref_points 参考点已过期: 指纹 9dab9f6bfb4b !=
   当前 ac61489dfb7e (四角/内参/分辨率/fixed_z_mm/axes/origin_mm 改过)`、exit 1；
   只改 `ref_points_verified_at` 时间戳 → 指纹不变、exit 0。
8. **越界拦截端到端**（svc-gate 用 PTY 假串口逼出真实发送路径，`/tmp/a2_goto_evidence.txt` 等）：
   `goto_reject` → 上位机发出 **0 字节 / 0 个 MOTION 帧**，HTTP `{"ok":false,...,"error":"机械臂限位拦截
   (指令未下发): …"}`；`goto_pass` 对照 → 96 B / 1 个 MOTION `[MOTION] task_id=201 目标=(126.440,63.400)mm`；
   `/comm` MOVE 同理（越界 0 字节、放行 32 B）。扫描路径：越界 run 有 `视觉限位拦截: …` 6 行 +
   "不是空盘"提示 + `scan_NNN.json` 全 `[]` + 取件命令 0 行；基线 run 正常 3 件。
9. **`start_competition.sh` 门禁路径**（容器内实测）：门禁不过 → `SCRIPT_EXIT=1` 且**不进 while 重启循环**；
   无锁 → `SCRIPT_EXIT2=0` 并成功 3 件。

### 已知问题与边界（必须和上面的结论一起看）

- **`09b054a`(A5) 单树边界红**：该 commit 的 `vision/test_vision.py:622` 引用了后续 A3 才存在的
  `VisionService._enforce_gates`，单独 checkout 它跑 `vision.test_vision` 是
  `Ran 60 FAILED(errors=8)`（该属性在 `76d00c8` 落地后即闭合，最终 HEAD 全绿）。
  这是**已知边界问题、不是功能缺陷**：规则"每个 commit 前跑全部测试"当时是在含 A3 的工作区上满足的。
  未做历史重写，理由：`test_vision.py` 被 A1/A5/A6/A6.1/A1.1 反复修改，rebase 冲突风险高于收益；
  最终判据取最终 HEAD 的全绿结果（本报告第 1 条）。
- **`76d00c8`(A2) 提交里包含现场未提交的标定状态**：该 commit 的 `vision/calib_config.json` diff 相对
  基线 `17d8c98` 除 A2 新键外还带上现场工作区原有的 3 个参考点、新四角（74.6,87.1 / 403.7,6.9 /
  629.4,243.4 / 65.4,411.9）与 `perspective:"auto"`（基线里是更早的一套四角 163.48,129.54 / 455.22,115.68 /
  591.28,352.19 / 83.26,417.61）。也就是说**现场那套标定数据现在进了 git**（好事：不会再丢），
  代价是这套 3 点拟合 scale=0.9783、平均 2.83mm、最大 4.23mm **超默认阈值**，
  所以比赛模式现在起不来（`fit_quality` 在 debug/race 都是 fail）——现场要么重标，要么显式放宽
  `gate_thresholds`。这与 A3 的验收要求一致，已写进 `FIELD_VERIFICATION_CHECKLIST.md`。
- **越界拦截的边界声明**：只保证**上位机不把越界目标/坐标放进发送序列**；
  **不声称**修复下位机驱动器故障、固件 `参数超限` 判定、`ACM0→ACM1` 重枚举。
- **D3 现场历史（11-02 01:06 "参数超限"）**：被拒的到底是 XY 还是速度参数 → **无法确认**
  （没有对应 run 目录、scan json 里没有 mm 坐标、日志零命中；实测 `参数超限` 全部落在
  11-01 16:27–16:31）。不编结论。
- **`[现场待验证]` 汇总**：见 `FIELD_VERIFICATION_CHECKLIST.md` 末尾"签字前的未闭环项确认"
  （B0.2/B2/B2 采样/B3/B4/B1/B5.1/B5.2/B5.3/B6.1/B6.2/B6.3/B7 全部未闭环）+ 补充 3 条
  （A7 的 `dataset/hard_cases|regression` 不存在、计划中字段的最终落名、A6 限幅可达性）。
- **验证者复核**：`verifier` 对每个 `视觉A*` commit 做了独立只读复核（越界扫描、禁用依赖 grep、
  重跑模块与 e2e、关键断言独立复现），证据 `/tmp/verify_evidence.md`；A1/A7 通过，A4 曾报两处偏差
  （已由 A4.1 补齐）、A5 报的 commit 边界红即上面第 1 条，A2/A6 通过。