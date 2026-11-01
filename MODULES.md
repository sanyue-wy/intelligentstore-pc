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
