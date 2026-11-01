# 智能分拣装置 · 上位机（RDK X5）总指导

2027 工创「智能+」赛道 **智能分拣** 赛项。装置把托盘里的货物识别、分类，用机械臂放进 6 个储物盒，高亮屏逐件显示序号、货物名称、图片、分拣成功总数量。本工程是 RDK X5 上的上位机：视觉（YOLOv8 + BPU）、坐标标定、比赛流程、下位机通讯、信息处理中心、监控、部署和工具链。

## 1. 三分钟上手

```bash
# 板子上 (宿主, 工程目录内)
sudo deploy/bpu_doctor.sh            # 1. 诊断 BPU / 容器 (第一次或系统升级后)
sudo ./is.sh migrate                 # 2. 建 ubuntu:22.04 容器并挂载宿主 BPU 库 (一次性)
./is.sh web --arm                    # 3. 浏览器 http://<板子IP>:8090 : 托盘 / 畸变 / 颜色 / 相机 / 标定 全部在这里调
./convert.sh --setup-remote 用户@笔记本IP   # 4. 一次性: 笔记本 WSL 作为 BPU 模型编译机
cp best.pt vision/models/ && ./convert.sh  # 5. 换模型: 一条命令 .pt -> ONNX -> BPU .bin
./is.sh precheck                     # 6. 赛前自检
./start_competition.sh               # 7. 比赛: 待机 -> 按启动键 -> 双手离开; 执行页 http://<板子IP>:8088
```
`./is.sh` 不带参数会列出全部命令。把视觉调试交给别人：照 `VISION_HANDOVER.md` 一步步做（传文件 → 替换工程 → 环境 → 调参 → 验收 → 交回）。调试台第一次打开要输入口令（启动日志里的 6 位数字，存在 `tools/.web_token`）。

> 目录名 `MessegeCenter`（Message）、`moniter`（monitor）是已知拼写错误，贯穿全部 import，历史原因保留，不要改名。

## 2. 一次比赛怎么跑

| 赛规（现场初赛） | 本工程 |
|---|---|
| 调试剩 30s 停止调试、开电源进入待机 | 开机即启动、自检、模型预热，停在待机（`./is.sh race on` 开机自启） |
| 裁判下令后一键启动，只有一次机会 | USB 单键 / GPIO 键 / 执行页「开始」，只认待机后的第一次按键 |
| 不能触碰笔记本、不能遥控 | 比赛程序启动时关闭调试台；执行页只有 开始/停止/急停/一键重启，没有手动控制 |
| 形状、颜色、信息完全一致才放同一盒，6 个盒，每盒 ≤4 件 | category = 形状\|颜色\|信息 永久映射到盒；超限告警 |
| 每件放好后显示 序号、名称、图片、成功总数，显示完才能分拣下一件 | 大屏推送并确认后才扫描下一件 |
| 15s 没有动作或没有显示 → 本轮结束 | 主循环 9s 动作看门狗先抖料；单件超时 12s；信息处理中心 12s 预警、15s 记违规 |
| 分错盒扣分，用时短优先 | 低置信度 / 重叠先抖料重扫；可选提前扫描下一件 |
| 两次运行取最好 | 再按一次启动键开始第二次运行，盒子和计数清零；每轮复盘报告 `records/runs/` |

## 3. 模块

```
             UI/ 比赛流程: 待机 → 启动键 → 扫描 → 选件 → 分盒 → 抓放 → 大屏 → 下一件
                │ scan()                 │ sort_one()             │ update_status()
          ┌─────▼──────┐          ┌──────▼──────┐          ┌──────▼──────┐
          │  vision/   │          │communication│          │   高亮屏     │
          │ YOLO+BPU   │          │ USB CDC 32B │          │ :8756 服务  │
          │ 托盘坐标系  │          └──────┬──────┘          └─────────────┘
          └─────┬──────┘                 │
                │ 心跳/目标               │ 状态 (调试工作台经 router)
          ┌─────▼────────────────────────▼─────┐   race/*   ┌──────────────┐
          │ MessegeCenter/ 信息处理中心           │◀──────────│ UI/run_report │
          │ 总线 + RaceLedger 赛规复核            │──告警────▶│ moniter/ 监控 │
          └─────────────────────────────────────┘           └──────────────┘
   tools/ : 一键模型转换、浏览器调参台、内参标定、训练      deploy/ : 容器、BPU 挂载、systemd、运维
   debug/ : Tk 调试工作台 (通讯/消息/监控; 视觉调参转浏览器)
```

| 模块 | 读这个 | 一句话 |
|---|---|---|
| 视觉 | `vision/GUIDE.md` | YOLOv8（BPU > ONNX > .pt）+ HSV 颜色 + 精定位 + 多帧融合；白色托盘自动定位，像素→托盘坐标→机械臂 |
| 比赛流程 | `UI/GUIDE.md` | 待机/启动键/执行页/看门狗/风险评估/复盘报告（已调试完成） |
| 通讯 | `communication/GUIDE.md` | 32 字节定长帧，ACK 重发，任务闭环（已调试完成） |
| 信息处理中心 | `MessegeCenter/GUIDE.md` | 消息总线 + 按赛规复核每轮分拣信息 |
| 监控 | `moniter/GUIDE.md` | 心跳看门狗、资源、告警 |
| 工具 | `tools/GUIDE.md` | `./convert.sh` 一键转模型、浏览器调参台、标定、训练 |
| 部署运维 | `deploy/OPERATIONS.md` | 容器、BPU 挂载、systemd、日志、故障速查 |
| 调试界面 | `debug/README.md` | Tk 工作台 |
| 接口索引 | `MODULES.md` | 每个模块的契约文件和约定 |

## 4. 现场调参顺序（全部在浏览器 :8090）

1. **相机页**：锁曝光、增益、白平衡 → 应用并保存。
2. **畸变页**：棋盘格 15~25 张 → 计算内参（RMS < 0.5px）。没有棋盘格就在托盘页用托盘直边估计。
3. **托盘页**：填内框 W×H、外沿、壁高、镜头高 → 看绿框/青框/坐标轴是否贴合；加参考点（尺子量的固定点，或点击画面 + 机械臂坐标），1 个点时选好轴向，建议 ≥2 个，9 个最好。
4. **颜色页**：每种颜色点 2~5 个货物 → 生成。
5. **状态页**：点哪去哪验 9 个位置（平均 ≤2mm）；Scan 看识别结果。
6. 关掉调试台，`./is.sh precheck`，`./start_competition.sh`。

## 5. 换模型（训练 → 板上一条命令）

1. 现场采图：调试台 Snap 或 `python3 tools/vision_tool.py capture --count 60`；比赛时每次扫描也会自动存 `records/scans/raw`。
2. 电脑上训练（`tools/train.py`），只训形状和污渍/文字/图案变体，颜色交给 HSV。
3. `best.pt` 放进 `vision/models/` → `./convert.sh`：导出 ONNX 并对拍 → 同步类别 → ONNX 立即可用 → 笔记本 WSL 编译 BPU `.bin` → 板上 BPU 与 ONNX 一致才启用 → 旧模型归档。
4. `vision/classes.json` 给新类别填 `shape_cn`、`height_mm`，薄壁件加 `thin_wall: true`。
5. 执行页「一键重启」生效。

## 6. 目录

```
convert.sh  is.sh  start_competition.sh  main.py  Dockerfile
vision/         视觉: service / yolo_engine / yolo_detector / calibration / tray / color_calib / cameras/
                配置: vision_config.json  classes.json  calib_config.json(现场生成, 例子 calib_config.example.json)  models/
UI/             比赛流程: run_competition / planner / controller / display_ui / start_trigger / run_page / run_report / precheck
communication/  下位机通讯
MessegeCenter/  信息处理中心: bus / topics / race / router
moniter/        监控
tools/          convert_model / vision_tool / calib_intrinsics / train
deploy/         provision / bpu_bridge / bpu_doctor / is_debug / is_health / systemd / OPERATIONS.md
requirements.txt / requirements-export.txt   依赖版本 (与 Dockerfile、provision 一致; 脱离容器部署时用)
debug/          Tk 调试工作台
records/        运行时生成: scans/(每次扫描) runs/(每轮报告)
```

## 7. 测试

```bash
for m in vision.test_vision MessegeCenter.test_bus moniter.test_monitor communication.test_comm UI.test_ui; do python3 -m unittest $m; done
ALLOW_MOCK=1 USE_SYNTHETIC_CAM=1 START_SOURCES=auto ONESHOT=1 EXPECTED_TOTAL=3 python3 UI/run_competition.py   # 无硬件全流程
```

## 8. 已知遗留（赛后和固件一起改）
- 通讯协议：32 字节帧没有 CRC（USB CDC 链路层有校验，风险低）；重发时换新 SEQ，下位机按 SEQ 去重失效，ACK 丢失时命令可能执行两次（比赛路径用绝对槽位 `GOTO_SLOT` 和任务闭环，影响有限；`STEP_CW/CCW` 只在调试里用）；ACK 用 `ack>s && ack-s<500` 模糊匹配。计划：同 SEQ 重发 + 下位机去重时重新回 ACK + RESERVED 字节放 CRC8 + 精确匹配。`client.py` 里 `_waiting_ack_seq/_ack_event` 是没用到的旧 ACK 逻辑。
- 消息总线默认同步分发，与 `MessegeCenter/interfaces.py` 契约不符（见 `MessegeCenter/GUIDE.md`）。
- 监控预置了 communication / message_center / coordinator 三个模块，但只有视觉发心跳，比赛程序也没启动 MonitorService：目前只是没覆盖，不会误报。
- 上层用到的契约外属性：`client.latest_status`、`vision.detector.engine`、`detect_multi/detect_fast`，换后端时要一并实现。

## 9. 还需要在真机上确认的事
- 宿主 `hbm_runtime` / `hobot_dnn` 挂进容器后能导入，`.bin` 推理结果与 ONNX 一致（`./convert.sh` 会自动比对）。
- Windows OpenSSH → WSL `hb_mapper` 编译链路。
- 海康相机、灯光、白色托盘检测（外围黑边）、GPIO/USB 启动键、物理大屏、机械臂完整闭环。
- 托盘参考点和点哪去哪误差。
