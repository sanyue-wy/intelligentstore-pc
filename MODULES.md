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
| 调试界面 | 依赖以上接口 | `debug/README.md` | Tk 工作台；视觉调参转浏览器 |

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
