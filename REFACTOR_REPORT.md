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
