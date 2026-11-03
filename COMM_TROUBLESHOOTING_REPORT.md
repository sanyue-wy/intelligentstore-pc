# 下位机通讯故障调查报告（修订版 v2）

> 对象：RDK X5 `/root/Desktop/1/pc_test_stm - 2/pc_test_stm`（重构后 `main`）
> 参照：`C:\Users\wy\Desktop\pc_test_stm_comm_v3 (2)\pc_test_stm`
> 修订时间：2026-11-02

> **修订说明（v1 → v2）**：v1 把"前往取件点超时 / 储物盘无 ACK"直接归因为"下位机固件没实现 0x04/ACK"。复核日志后发现：那次实物运行（11-01 16:31）**根本没有标定文件（`calib_config.json 不存在`，坐标不可信）**，下位机在状态帧里报的是 `参数超限(坐标/速度越界)`——说明**实物串口双向通讯其实是通的**。因此 v1 的"固件不合格"结论证据不足，v2 更正为：**主因是未标定导致坐标非法**；`0x04`/储物盘 ACK 是否合格，需要在标定后做实机验证才能定性。

---

## 〇、结论摘要

1. **通讯源码没有因重构改变**：仓库 `communication/*.py` 与"没问题的" v3 **逐字节一致**（md5 全同），`git log -- communication/client.py` 只有初始提交 `bc0b461`，文件 mtime `2026-10-26` 早于重构（`11-02`）。
2. **"V3 每次指令都对"与"RDKX5 比赛跑不通"不矛盾——两者测的层次不同：**
   - v3 的 `通讯更新说明.txt` **第 28–32 行白纸黑字**：验证是在沙箱里"**虚拟串口(pty)接仿真 STM32**"做的，并注明"**未在你的 Windows + 实物 STM32 上验证，请重点看第二节第 1 条的 RES_FLAGS 0x04**"。也就是说 V3 自测里的"下位机"是 **mock_device 软件仿真**，它天然会把 `RES_FLAGS` 置 `0x04` 并回每条 ACK。
   - 即便你用 V3 的 `--comm` 调试器接**实物 COM 口**看"指令有反应"，它也只做"发指令 / 看 20Hz 状态帧"，**并不执行比赛里的两道闸门**：`等 RES_FLAGS & 0x04 才发 PERMIT`、`GOTO_SLOT 必须 0.5s 内 ACK`。所以"指令有反应"≠"比赛闸门都满足"。
3. **RDKX5 唯一一次实物运行（11-01 16:31，接 /dev/ttyACM0）失败，首要原因是"未标定"：** 日志明确 `未标定 (vision/calib_config.json 不存在), 坐标不可信`；识别出的坐标出现 `(264.4,-179.3)` 这类越界值 → 固件状态帧回 `参数超限(坐标/速度越界)`（`communication/protocol.py:257` 是**下位机上报错误码 PARAM 的映射**，能收到它本身就证明 **RDKX5↔STM32 双向通讯正常**）→ 机械臂进入 FAULT，任务直接判失败；另有在范围内的点则一路"移动中"但始终没有等到 `0x04` → 12s 超时。
4. **重构确实引入过一个通讯 bug（已修复）：** 新 8090 通讯调试页 `/api/comm/state` 在未收到状态帧时返回 `Infinity`（非标准 JSON），浏览器解析失败 → 页面误报"通讯客户端不可用"。已在 `webui/base.py`、`comm_debug.py`、`pages.py`、`tools/vision_tool.py` 修复，18 项测试通过。

---

## 一、证据：通讯模块与 v3 逐字节一致

| 文件 | 仓库 md5 | v3 md5 | 结果 |
|---|---|---|---|
| client.py | ff306ef10d5ccc2a5bb2ea56cfd00ec7 | 同左 | ✅ |
| __init__.py | 24cbe7ea29fbef5b23c710c2783e42b2 | 同左 | ✅ |
| interfaces.py | b61097b3aee6d1252f86a9a919612628 | 同左 | ✅ |
| mock_device.py | a9e496ba382bc6add7ea75f37bbe39d6 | 同左 | ✅ |
| protocol.py | dd38df09bd9d8ee0a157a982b4fee3db | 同左 | ✅ |
| serial_discovery.py | 15b7bf6253399cde77b4ef90fd9a9eee | 同左 | ✅ |
| test_comm.py | d1c37073cc6861fd7cb2b0ea60898792 | 同左 | ✅ |

`git diff --stat bc0b461..HEAD -- communication/` 仅 `GUIDE.md`(4 行) + 删除 `example_usage.py`；`git log -- communication/client.py` 仅 `bc0b461 初始提交`。→ **重构没动通讯实现。**

---

## 二、关键事实：11-01 16:31 的实物运行到底发生了什么

日志 `/var/log/intelligentstoring/competition.log`（该次运行，**早于重构**）：

```
16:31:03 WARNING vision.service: 视觉自检 ... 标定=4 点/0.00mm | 问题=[... '未标定 (vision/calib_config.json 不存在), 坐标不可信' ...]
16:31:03 INFO  communication.client: 串口已连接: /dev/ttyACM0 (来源=auto)
16:31:04 INFO  competition: 下位机: /dev/ttyACM0 (USB Serial, 115200bps)
16:31:24 INFO  competition: 抓取 蓝色五棱柱 conf=0.67 mm=(70.9,107.0) -> 1 号盒
16:31:36 WARNING UI.controller: [Controller] 任务 1 失败: 前往取件点超时      ← 等 0x04 满 12s
16:31:37 WARNING UI.controller: [Controller] 任务 2 失败: 机械臂故障: 参数超限(坐标/速度越界)  ← 坐标 (264.4,-179.3) 越界，固件报 PARAM
...
16:32:20 WARNING UI.controller: [Controller] 储物盘转位无 ACK, 补发一次
16:32:20 ERROR   UI.controller: [Controller] 储物盘转位仍无 ACK, 放弃本件
16:32:21 ERROR   competition: 连续失败 8 次 / 本轮累计 8 次, 结束本轮
```

要点：
- **串口连上了 `/dev/ttyACM0`**，而且能收到固件回的错误码 `参数超限` → **RDKX5 与实物 STM32 的双向通讯是通的**，不是"没连上/没反应"。
- **该次运行未标定**（`calib_config.json` 不存在，日志里"坐标不可信"）。坐标越界 → 固件报 `PARAM`（`protocol.py:257`：`"参数超限(坐标/速度越界)"` 是 ErrorCode 映射）→ 机械臂 FAULT → 任务判失败。
- 在范围内的点（如 70.9,107.0）则不报错、一直"移动中"，但固件始终没给出 `0x04` → 上位机等满 `SORT_TIMEOUT_S`(12s) 报"前往取件点超时"。**这可能是固件没置 0x04，也可能是坐标不准导致机械臂没真正到位**——单凭这条日志无法区分，需要实机验证。

> 旁证：`records/runs/` 里 20:35 / 21:07 / 11-02 00:38 三次"全成功"的报告，经核对是**仿真/合成相机 + Mock 下位机**的端到端测试（三件都是同一个"蓝色四面体"、模型耗时 5~46ms、00:38 那次 `elapsed_s=15.7` 与重构验收的仿真 e2e 完全一致），**不是实物运行**，不能用来证明实机 OK。

---

## 三、根因（修正后）

1. **首要原因：未标定 → 坐标非法。** 11-01 16:31 运行时没有 `calib_config.json`，视觉输出的机械臂坐标不可信（出现负值/越界），结果一半任务被固件判 `参数超限`，另一半到不了取件点而超时。**这是"比赛跑不通"的直接原因。**
2. **待实机确认项：固件的 `0x04` 与储物盘 ACK。** 只有在**标定正确、坐标合理**的前提下，机械臂才会真正到达取件点；此时若固件仍不置 `RES_FLAGS` bit2(0x04)、或 `GOTO_SLOT` 不回 ACK（`send_cmd` 的 `ack_timeout_s` 默认 **0.5s**），才会暴露"固件契约不合格"。目前证据不足以下此结论。
3. **V3 "都对"的原因**：它的自动验证跑的是 **仿真下位机**（pty + `mock_device`，`mock_device.py:288` 主动 `res_flags = ResFlags.PICK` 且回 ACK）；`--comm` 调试器也只验证"发得出、收得到"，不验证比赛闸门。所以"V3 没问题"不能外推到实机比赛。

---

## 四、重构真正引入的通讯 bug（已修复，与本次故障无关）

- 现象：8090「通讯调试」页一直显示"通讯客户端不可用"，状态帧不刷新（即使已连接/Mock）。
- 原因：`link_age_s` 在未收到状态帧时返回 `inf` → `json.dumps` 输出 `Infinity`（非标准 JSON）→ 浏览器 `JSON.parse` 抛错 → `common.js` 的 `J()` 返回错误 → 页面 `d.available` 为 undefined。
- 修复：`webui/base.py` 新增 `_sanitize()`（nan/inf→null）+ `json_bytes(allow_nan=False)`；`comm_debug.py` 的 `age_s=_finite(...)`；`pages.py` 空值显示 `-`；`tools/vision_tool.py` 的 `_json()` 复用 `webui.base.json_bytes`。
- 验证：`/api/comm/state` 返回 `"age_s": null`，严格 JSON 解析通过；`webui.test_webui` + `communication.test_comm` = **18 tests OK**。

---

## 五、建议的下一步（一锤定音）

1. **先把标定做对**：确认 `vision/calib_config.json` 已生成（当前已有，mtime 2026-11-02 02:01，含 corners/fixed_z=15 等）；在 8090「视觉调试」页确认自检不再报"未标定/坐标不可信"。
2. **接实物做逐条验证**（当前 RDKX5 上 `/dev/ttyACM*` 目前不存在，需把 STM32 插到 RDKX5）：
   - 打开 8090「通讯调试」页 → 连接实物串口 → 点"轮询"，确认 20Hz 状态帧（链路年龄变小、状态有值）。
   - 手动发 `GOTO_SLOT`：看是否**有 ACK**、`储物槽` 是否变化 → 直接判定"储物盘 ACK 是否合格"。
   - 手动发 `MOVE` 到取件点：看 `res_flags` 是否出现 `0x04` → 直接判定"取件位标志是否合格"。
3. **跑一次完整比赛并抓 `/var/log/intelligentstoring/competition.log`**：标定正确后若仍出现"前往取件点超时/储物盘无 ACK"，才是固件问题，按 A 节要求固件方补齐 `0x04` 与 `GOTO_SLOT` ACK；若不再出现，说明此前就是**未标定**所致。
4. 兼容兜底（仅临时、不建议正式用）：可加 `PICK_READY_MODE=stage2` 用 `res_stage==GRIP(2)` 近似判断到达（有提前/滞后风险）。

---

## 六、证据文件清单

| 类型 | 路径 |
|---|---|
| 比赛日志（唯一实物运行，11-01 16:31） | `/var/log/intelligentstoring/competition.log` |
| 仿真成功运行（非实物） | `records/runs/20261101_203540`、`20261101_210733`、`20261102_003832` |
| 旧版备份 | `/root/Desktop/_rdkx5_bak/client.py.orig`（10-25 旧版） |
| v3 通讯说明（含"仿真验证/未做实机验证"原文） | `C:\Users\wy\Desktop\pc_test_stm_comm_v3 (2)\pc_test_stm\通讯更新说明.txt` 第 28–32 行 |
| 关键代码 | `communication/client.py:512,864,874`（send_move / 等 0x04 / 超时）、`communication/protocol.py:257,180-188,383`（ErrorCode.PARAM 映射、ResFlags.PICK=0x04）、`communication/mock_device.py:288`（仿真置 PICK）、`UI/controller.py`（sort_one / 无 ACK 放弃） |
