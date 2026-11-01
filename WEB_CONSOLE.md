# 统一网页调试台（阶段 2）

阶段 2 把原来分散的 Tk 调试器收敛成**一个网页调试台 :8090**（同一进程、同一口令、同一视觉服务），
比赛执行页保持 **:8088**。全部为标准库实现，不依赖 Node/npm/CDN。

## 启动

```bash
./is.sh web            # 只调试台（不连下位机）
./is.sh web --arm      # 调试台 + 连接下位机（标定可点哪去哪 / 流程可真实执行）
# 浏览器 http://<板子IP>:8090
```

首次打开要输入口令：启动日志里的 6 位数字，或环境变量 `IS_WEB_TOKEN`，或 `tools/.web_token` 文件。

> 比赛程序运行时会写 `/tmp/is_competition.lock`，此时调试台默认**拒绝启动**（防止误操作机械臂）；
> 确需并存用 `--force`。

## 四节（顶栏导航）

1. **视觉调试**（`/`）：原有全部视觉调参能力——扫描/snap、状态自检、托盘内外框与参考点、畸变一键校正、
   颜色样本生成、相机曝光/增益/白平衡、像素↔机械臂坐标、标定板 PDF、回放记录。逻辑与阶段 1 完全一致，
   只换成统一样式。
2. **流程调试**（`/flow`）：扫描 → 规划（`SorterPlanner.pick_next`/`slot_for`）→ **单步执行一件**
   （`CompetitionController.sort_one`）→ 抖料 → 复位。无硬件时用模拟通讯 + 合成相机，可逐件验证分盒/抓放时序。
3. **通讯调试**（`/comm`）：列出串口 / 连接 / 断开 / 连接模拟下位机；发送 `clear/estop/cancel/permit/
   grip_open/grip_close/z_up/z_down/tray_shake/tray_home/storage_step/storage_slot/poll/move`；
   实时显示收发热日志与 `HostStatus`（状态/坐标/夹爪/结果位/是否可抓等）。
4. **完整调试**（`/run`）：一键跑完整流程（与比赛同一状态机/看门狗逻辑，mock + 合成相机），
   观察赛规动作、抖料、失败上限、大屏推送。

另有 **大屏预览**：`/screen`（全屏只读，随大屏刷新）与 `/screen.png`。

## 比赛执行页 :8088

`./start_competition.sh` 后浏览器打开 `http://<板子IP>:8088`：只有 `开始 / 停止本轮 / 急停 / 一键重启 /
刷新自检`，无手动控制（符合赛规）。新增 `/screen` 全屏只读大屏页。

## 主要 JSON 接口

- 视觉：见 `tools/vision_tool.py` `cmd_web`（`/api/status`、`/api/scan`、`/api/tray`、`/api/calib/*`、
  `/api/color/*`、`/api/camera`、`/api/pixel`、`/api/goto` 等，阶段 1 已存在）。
- 通讯：`GET /api/comm/state`、`GET /api/comm/ports`；`POST /api/comm/connect|mock|disconnect|send`。
- 流程：`GET /api/flow/state`；`POST /api/flow/scan|plan|exec|shake|reset`。
- 完整：`GET /api/run/state`；`POST /api/run/start|stop|estop`。

## 测试

```bash
python3 -m unittest webui.test_webui -v
```
