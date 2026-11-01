# 模块接口索引

| 模块 | 契约（上层只依赖这里） | 指导 | 说明 |
|---|---|---|---|
| 视觉 | `vision/interfaces.py`；配置 `vision_config.json`、`calib_config.json`、`classes.json` | `vision/GUIDE.md` | `create_vision_service()` → `scan()`；托盘坐标系在 `calibration.py` + `tray.py` |
| 比赛流程 | `UI/interfaces.py` | `UI/GUIDE.md` | 已调试完成；`run_report.py` 旁路发布 `race/*` |
| 通讯 | `communication/interfaces.py`、`protocol.py` | `communication/GUIDE.md` | 已调试完成，未改动 |
| 信息处理中心 | `MessegeCenter/interfaces.py`、`topics.py`、`race.py` | `MessegeCenter/GUIDE.md` | 总线 + RaceLedger 赛规复核；`MessageRouter` 延迟导入 |
| 监控 | `moniter/interfaces.py` | `moniter/GUIDE.md` | 心跳看门狗、资源、告警（含 race 告警） |
| 工具 | 命令行 | `tools/GUIDE.md` | `./convert.sh`、`vision_tool.py web`、`calib_intrinsics.py`、`train.py` |
| 部署 | `deploy/*.sh`、`*.service` | `deploy/OPERATIONS.md` | ubuntu:22.04 容器 + 宿主 BPU 挂载；比赛模式 service |
| 调试界面 | 依赖以上接口 | `WEB_CONSOLE.md` | 统一网页调试台 :8090 (视觉/流程/通讯/完整) + 比赛执行页 :8088 |

总指导见 `README.md`。

## 约定
- 上层只依赖各模块 `interfaces.py` 的抽象基类，通过 `create_xxx(backend=...)` 拿实例；替换实现 = 实现基类 + `register_xxx_backend()`。
- 数据契约只定义一次：`VisionTarget / ModuleHeartbeat / SystemAlert / RaceState / SortRecord / RaceRule` 在 `MessegeCenter/topics.py`，`HostStatus / HostCmd` 在 `communication/protocol.py`，其它地方只 re-export。
- 所有 import 以工程根目录为根（容器内 `/app`）。
- 参数走配置文件或环境变量（各 GUIDE 末尾有表），代码里不写死路径/端口。
- 需要实时画面的调参只做在浏览器调试台（`tools/vision_tool.py web`），不做本地窗口。
- 改接口时同步 `interfaces.py` 文档串与对应 GUIDE，跑完第 7 节测试再打包。


## 重构记录（阶段 1：去嵌套，不改模块边界）

- `vision/camera.py` 兼容转发层已删除：`SyntheticCamera`/`ReplayCamera`/`FrameGrabber` 走 `vision/cameras`，
  `BaseCamera`/`PhysicalCamera` 直接 `from vision.cameras.uvc import ...`。旧 `from vision.camera import ...`
  写法不再支持（仓库内已全部改到新路径）。
- `tools/convert.sh`（只是转调根目录 `convert.sh` 的重复入口）已删除，统一用根目录 `./convert.sh`。
- `communication/example_usage.py`（示例非运行代码）已删除，用法并进 `communication/GUIDE.md`。
- `vision/detector.py`（HSV `WorkpieceDetector`，368 行）与 `vision/yolo_detector.py`
  （`YoloWorkpieceDetector` 继承它，409 行）职责**不重叠**，不合并：前者是 HSV 检测+几何矩，
  后者在引擎可用时用 YOLO、不可用时回退到前者。二者合并会破坏"引擎不可用自动退回 HSV"的解耦。
- `debug/` 下的 Tk 调试器子目录（`*_debug/`+`common/`+`*.bat`）**本阶段不动**：阶段 2 用统一网页
  调试台替代、阶段 3 整体删除，提前打平只会制造无谓改动。
- `communication/`、`UI/` 的逻辑代码按总原则"行为不变"**不做**微重构（深层嵌套/早期返回等），
  以免破坏已与真机调通的字节级/时序级行为。


## 重构记录（阶段 2：统一网页，逻辑不变）

- 新增 `webui/` 包（纯标准库，无 Node/CDN）：`base.py`（口令校验、静态文件安全读取、MIME）、
  `pages.py`（四节页面骨架）、`comm_debug.py`（串口发现/连接/模拟 + 各命令发送 + 收发日志）、
  `flow_debug.py`（扫描→规划→单步执行一件→抖料→复位，与比赛共用 `SorterPlanner`/`CompetitionController`）、
  `screen.py`（全屏只读大屏页）、`static/app.css` + `static/common.js`（全站统一样式/工具）、`test_webui.py`。
- `tools/vision_tool.py` 的 `cmd_web` 就地升级为**统一调试台 :8090**：保留原有全部视觉调参 API/页面逻辑不动，
  仅在页面顶部加统一导航条（视觉调试/流程调试/通讯调试/完整调试/大屏预览），并挂载新增的
  `/comm`、`/flow`、`/run` 页面与 `/api/comm/*`、`/api/flow/*`、`/api/run/*` 接口；同一进程、同一口令、同一 `svc`。
- 比赛执行页 `UI/run_page.py`（:8088）接入共享样式与红橙「比赛模式」顶栏，新增 `/screen`（全屏只读）
  与 `/static/<name>`；`开始/停止本轮/急停/一键重启/刷新自检` 与 `state_dict`/`frame_jpeg`/`screen_png`/`request`
  逻辑**未改**。详见 `WEB_CONSOLE.md`。
- `debug/` 下 Tk 调试器已在阶段 3 删除；`communication/`、`UI/run_competition.py` 状态机
  与看门狗行为**未改**。


## 重构记录（阶段 3：删除旧的/无用的）

- 删除补丁备份目录 `patch_backup_tray_fixed_1101_144419/`、`patch_backup_vision1102_1101_154036/`。
- 删除整套 Tk 桌面调试器：`debug/` 整目录（含 `*_debug/`、`common/`、`*.bat`）与 `main.py`（Tk 启动入口）。
  调试入口现由 `./is.sh web` → `tools/vision_tool.py web` 统一提供。
- 删除 Tk 相关部署：`deploy/is_debug.sh`、`deploy/intelligentstoring-debug@.service`；同步更新
  `is.sh`（`start`=`web`、`stop/status` 改为 `run_competition|vision_tool`、`race on` 先停调试台）、
  `deploy/intelligentstoring.service` / `-competition.service` 注释与 Conflicts、`deploy/OPERATIONS.md`。
- 删除 `UI/display_ui.py` 的本地 Tk 兜底窗口（`_init_local_window/_refresh_local/_render_local/...`）；
  仅保留远程大屏推送主链路，网页大屏由 `/screen` 提供只读预览。
- `tools/calib_images/`（60 张货物照片，非棋盘格）移到 `dataset/calib_images_20261029/`（`.gitignore` 内），
  不再进仓库。
- 保留 `vision/models/best_1027_640x480.bin` + 同名 `.json`（类别名）、`best_1027.pt`（转换源）。
  `best_1027_640x480.onnx`（12MB，ONNX 回退）**未删**，按规格"删除模型文件先问用户"，待用户确认。
- **未删死代码误报**：`communication/client.py` 的 `_waiting_ack_seq / _ack_event` 经核查**仍在用**
  （`_wait_and_ack` 与 `_handle_status` 中真实读写），不是死代码，故保留、不改逻辑。
- 文档同步：`README.md`、`MODULES.md`、各 `GUIDE.md`、`deploy/OPERATIONS.md` 去除指向已删文件的段落。


## 重构记录(阶段4) — 方案B 上位机 PERMIT 兼容兜底
- 背景：新固件把运动/抓取交给 `User/APP/app.c` 业务层，到达取件位后只停在"等 PERMIT"，**不再置 `RES_FLAGS bit2 (PICK=0x04)`**；旧版上位机 `execute_sorting_task_ex` 死等 0x04 必然 12s 超时。
- 改动：`communication/client.py` 在等待到达取件位的循环中新增兜底——机械臂运动到目标附近（默认5mm）并稳定一段时间（默认1s）即视为到位并下发 `PERMIT`；报警/急停/有 error 锁存时绝不误发。可用 `PERMIT_FALLBACK=0` 关闭。逻辑仅新增、不改原有 PICK 路径（固件修好后仍走原生路径）。
- 测试：`communication/test_permit_fallback.py` 5 用例（兜底生效/关闭超时/故障不误发/目标远不误发/原生PICK回归）；全套 70 用例 OK。
