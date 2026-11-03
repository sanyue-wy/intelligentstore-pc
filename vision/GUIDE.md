# vision/ 使用指导（视觉模块）

> 对外契约：`interfaces.py`（上层只用这里的东西）。识别参数：`vision_config.json`；坐标标定：`calib_config.json`（例子见 `calib_config.example.json`）。
> **所有需要实时画面的调参都在浏览器里做**：`./is.sh web` → 笔记本打开 `http://<板子IP>:8090`，板子不需要显示器。

## 1. 边界

对外接口不变（UI/、MessegeCenter 不用改）：`create_vision_service(bus, backend, **kw)`、`VisionServiceBase` 全部方法、`scan()`、`get_fresh_frames()`、`detector.color_ranges / get_color_mask / update_color_range`、`VisionTarget` 字段、心跳 `module_name="vision"` 1Hz、`TOPIC_VISION_TARGET`、相机实现 `from vision.cameras import SyntheticCamera` / `from vision.cameras.uvc import PhysicalCamera`。

## 2. 结构
```
VisionService (service.py)                scan 永不抛异常; 推理锁; 启动自检; 每次 scan 存图复盘
 ├─ FrameGrabber (cameras/)               独立取图线程, 帧序号, 掉线重连; uvc | hikrobot | replay | synthetic
 ├─ CoordinateCalibrator (calibration.py) 像素 -> 机械臂 mm
 │    └─ tray.py                          白色托盘定位 + 托盘坐标系 + 畸变估计
 └─ detector
      ├─ YoloWorkpieceDetector (yolo_detector.py)  后端 "yolo" (比赛默认): YOLO + HSV 颜色 + GrabCut 精定位 + 多帧融合 + 二维码
      │    └─ YoloEngine (yolo_engine.py)          BPU (hbm_runtime / hobot_dnn) > ONNX > .pt, 按最新 .pt 选模型
      └─ WorkpieceDetector (detector.py)           后端 "hsv_color": 无模型时兜底
color_calib.py  现场 HSV 采样 -> 颜色区间        config.py  配置加载与环境变量覆盖
```

## 3. 坐标：像素 → 机械臂（推荐托盘模式）

硬件前提：托盘固定不动、外围贴黑边、夹爪垂直托盘（纯平面 XY）。

```
像素 (u,v)
  → 去畸变           camera_matrix / dist_coeffs (棋盘格或托盘直边估计)
  → 托盘单应+位姿    托盘四角 (自动检测或固定四角) -> 托盘坐标 mm; 由单应+焦距求相机位姿
  → 高度视差         按位姿把高度 h 处的点投回托盘 (h = 货物高度 × parallax_factor); 求不出位姿时退回 p' = c + (p - c)·(1 - h/H_cam)
  → 刚体变换         [xr,yr] = R·[xt,yt] + t：1 个参考点 + axes，或 ≥2 个参考点自动求 R
  → 残差多项式       ≥5 个参考点一阶、≥9 个二阶，补镜头残余畸变 / 装配倾斜 / 机械臂误差
  → 机械臂 (x_mm, y_mm)
```

**托盘检测**（`tray.py`）：HSV 低饱和高亮分割（亮度阈值自动）→ 大核闭运算填掉货物留下的洞 → 最大外轮廓 → 去畸变 → 凸包粗四边形 → 每条边只取贴着凸包的轮廓点（被货物挡住凹进去的点自动排除）做 Huber 直线拟合 → 相邻边求交得到亚像素角点 → 长宽比与配置比对（默认容差 8%）。
**跟踪**：多帧中值；单帧跳变（手伸进画面）直接忽略；连续 3 帧一致的新位置才接受（相机被碰后自动重新锁定，并在自检/执行页告警）。检测失败时沿用上次结果，启动时先用保存的角点。
**镜头畸变（一键单张）**：`tools/calib_intrinsics.py` 的 `calibrate_views`——平面棋盘格在无畸变相机里应满足同一个单应，求让格点最符合单应的径向畸变 k1（覆盖够大再加 k2，畸变明显时放开主点），焦距取经验值（去畸变像素只依赖 k/f²）。所以单张、打印缩放不准都可以。网页「畸变」页「一键校正」，旧内参备份在 `calib_config.json` 的 `prev_intrinsics`，可撤销。

**固定托盘（可选）**：`calib_config.json` 的 `tray.fixed_px` 填托盘内框在画面里的像素位置（画面左上角为原点，x 右 y 下：`{"corners":[4点]}`，相机正对时也可用 `{"x","y","w","h"}`，加 `image_size`），就不再自动检测。**相机斜看（托盘在画面里是梯形）必须给四角**：网页托盘页「点四角」（可自动吸附到白色边缘 = 壁顶内沿，`fixed_z_mm` 自动取壁高），或 `tools/set_tray_fixed.py --corners`。`tray.origin_mm` 给出托盘原点在机械臂坐标系里的位置（无参考点时生效）。

**托盘坐标 → 机械臂坐标**（`robot = R·tray + t`，这是唯一会丢信息的一步）：

- 视觉只算得出「货物在托盘里哪儿」，算不出「托盘摆在世界哪儿」。`R` 来自 `axes`（或用 ≥2 个参考点自动求），`t` 来自 `origin_mm`（或用 1 个参考点反算）。
- **两个都不给**时 `t` 按 0 处理，所有点整体偏移一个未知向量——相对位置对、绝对位置错，机械臂会抓空。自检会报「没有参考点也没有 origin_mm」。
- `axes` 写法：`[机械臂+X 对应托盘哪个方向, 机械臂+Y 对应托盘哪个方向]`。托盘方向约定为**画面右 = tray_x、画面下 = tray_y**。
  例：托盘左上内角在机械臂 (0,190)、画面向右 Y 减小、画面向下 X 增大 → `axes: ["tray_y", "-tray_x"]`、`origin_mm: [0, 190]`。
  验算：托盘 (100,0) → (0,90)，托盘 (0,50) → (50,190)。
- 参考点 ≥2 个时 `R` 自动求解，还能查出镜像和比例（偏离 1 超过 3% 说明尺寸或坐标填错）；≥5 / ≥9 个再加残差多项式修正。

**镜头高 `cam_height_mm`**：镜头光心到**托盘底面**的垂直高度（mm），量竖直距离，不是到托盘边缘的斜距。它有两个用途：反推焦距求相机位姿；求不出位姿时做径向视差校正。
反推出的镜头高和填写值**相差超过 15%** 时 `estimate_pose` 返回失败，`pixel_to_tray/pixel_to_robot` 退回平面单应，自检会报出原因和「最接近的可达值」。这时优先怀疑：四角没点在同一高度、内框 W/H 填反、或广角镜头没做畸变校正。

> **注意这个值是硬约束，量错会直接改变抓取点**：位姿一旦成立，`parallax_factor × height_mm` 高度上的点会按真实三维几何投回托盘，斜看相机时这个修正量可达几十毫米（本机倾角 44°、抓取高 18mm 时全盘最大 34mm）。所以改完 `focal_px`/`cam_height_mm` 一定要做一次「点哪去哪」验证 —— 但**「点哪去哪」默认按底面算（h=0），验不出高度投影**，要验高度投影得在状态页把「抓取高」填成实际值（如 18）再点货物顶面。
> 最常见的两种量错：① 量成「**桌面**到镜头」而不是「**托盘底面**到镜头」——托盘本身架高了，差的就是那几十毫米；② 量到镜头外壳/遮光罩而不是光心。

**透视 / 斜看相机（`tray.perspective`，默认 auto）**：托盘四角单应 + 焦距 → 相机位姿（倾角、镜头高）。高度 h 处的像点按 `像素 = K[r1 r2 t+h·n][x y 1]ᵀ` 投回托盘坐标，与倾角无关；旧公式 `p' = c + (p-c)(1-h/H)` 只在光轴垂直托盘时成立（倾 25° 时 18mm 高的点偏约 8mm，新方法 <0.1mm，合成数据）。焦距来源：`tray.focal_px` > 棋盘格完整标定 > `cam_height_mm` 反推 > 托盘四角自估（倾角 ≥12°）；都没有时退回旧公式。状态里 `perspective` 字段给出方法、倾角、估计镜头高。自动检测模式下梯形托盘的长宽比校验也会自动放宽。

**自动 ROI**：没有手动设 `detect.roi` 时，用托盘内框外扩 6mm 作 ROI，托盘外的误检直接丢弃。

### 现场标定步骤（全部在浏览器）
1. **托盘页**：勾选启用，填内框 W（画面左右方向）× H（上下方向）、外沿 rim（白色外边界到内框）、壁高、镜头高 → 保存并应用。画面出现绿框（白色外边界）、青框（内框）、红/绿箭头（托盘 +x/+y）。长宽比报错就检查尺寸或相机是否太斜。
2. **参考点**：
   - 尺子量：固定点到「画面左上内角」两条内边的距离 = 托盘坐标，再填它的机械臂坐标 → 加参考点；只有 1 个点时选好「机械臂 +X / +Y 对应托盘哪个方向」。
   - 或「标定/ROI」页 → 标定模式 → 点击画面上的标记 → 输入（`--arm` 时自动读取）机械臂坐标 → 求解。托盘模式下点击的点自动换算成托盘坐标存为参考点，**相机再动也有效**。
   - 2 个点以上会给出旋转角、镜像、比例（偏离 1 超过 3% 说明尺寸或坐标填错）和逐点误差。
3. **畸变页**（推荐做一次）：打印 A4 棋盘格（`tools/calib_board_A4.pdf`，缩放无所谓）平铺在托盘上 →「一键校正」，格线拟合误差 ≤0.3px 合格；可挪到角落「再拍一张叠加」，不满意「撤销」。没有打印机时在托盘页「用托盘直边估计畸变」（只估 k1）。**改内参后托盘会自动重新锁定，参考点不用重做。**
4. **验收**：状态页勾选「点哪去哪」（`--arm`），点托盘里 9 个位置，夹爪对准误差平均 ≤2mm、最大 ≤4mm 算合格。

### 点位模式（兼容旧方法）
`tray.enabled=false` 时使用：标定模式点 ≥4 个点（建议 9 点）→ 单应矩阵（≥8 点 RANSAC）。相机一动就要重标。

## 4. 浏览器调试台页面（`tools/vision_tool.py web`）

| 页 | 调什么 |
|---|---|
| 状态 | 帧率、模型、标定/托盘状态、自检问题；点击画面看机械臂坐标和 HSV；点哪去哪 |
| 托盘 | 托盘尺寸 / 参考点 / 轴向；重新检测；托盘直边估计畸变 |
| 畸变 | 棋盘格拍摄与内参计算；去畸变画面对比 |
| 标定/ROI | 点击加标定点或参考点；手动 ROI |
| 相机 | 曝光 / 增益 / 白平衡滑块，写回 `vision_config.json` |
| 颜色 | 点选货物采样 → 自动生成 `classes.json` 的 HSV 区间 |
| 大屏 / 复盘 | 大屏预览；每轮报告、识别画面、归档回归集 |

画面下方：Scan（与比赛相同的多帧融合）、Snap（存原图作训练/校准数据）、置信度滑块。比赛程序运行时调试台拒绝启动。

## 5. vision_config.json
| 键 | 说明 |
|---|---|
| `camera.type` | uvc / hikrobot / replay / synthetic |
| `camera.exposure_us / gain_db / wb_kelvin` | 比赛建议全部锁定（自动白平衡会让 蓝↔浅蓝、黄↔橙 漂移） |
| `model.conf / iou / class_conf` | 全局 / 按类置信度阈值 |
| `detect.roi` | 手动 ROI；托盘模式下留空即自动 |
| `detect.min_area_px / max_area_px / edge_margin_px` | 过滤误检、贴边截断目标 |
| `detect.parallax_factor` | 抓取高度 = 物体高度 × 该值（0.6 ≈ 上表面附近） |
| `require_info` | **顶层**开关（默认 **false** = 旧行为）：true 时 `category` 第三段必须是解出的信息，解不出写 `unknown` 并压置信度触发抖料（A4，见 §7.2） |
| `detect.qr_budget_ms` | 二维码解码时间预算（默认 500），按**整次 scan** 计；0 = 本次 scan 不解码（A4，见 §7.2） |
| `scan.frames / min_hit_ratio` | 融合帧数 / 出现比例下限（`min_hits = ceil(frames × min_hit_ratio)`，语义未变） |
| `scan.match_ratio / match_px_min` | 多帧匹配半径 = `max(match_px_min, match_ratio × min(框宽, 框高))`（默认 0.35 / 12px，**按件大小自适应**，A5 见 §7.3）；`match_ratio <= 0` 时退回旧的固定 `match_px`（默认 25） |
| `color.source` | `hsv`（默认：HSV 投票为权威，`classes.json` 的 `model_color` 只做冲突检测）/ `model`（应急：颜色直接用 `model_color`，**换颜色后就失效**，只用于板上 HSV 不可用时） |
| `color.conflict_ttl_s / conflict_conf` | 冲突记忆 TTL（默认 60s）/ 比赛模式首次冲突把置信度压到的值（默认 0.43，须 < `COMMIT_CONF` 0.45，压下去才会触发一次抖料重扫） |
| `color.tray_white_balance / wb_gain_min / wb_gain_max / wb_min_pixels` | 托盘白基准归一化（默认 **false**）；增益限幅 0.7~1.4；可用像素不足 200 时沿用上次增益。**只作用于颜色投票前的裁剪图，不改送模型的图**。`[现场待验证]` |
| `record.enabled / dir / keep` | 每次 scan 存原图+标注图+json（`records/scans`），也是量化校准图来源 |

环境变量临时覆盖：`IS_CAM_TYPE IS_CAM_DEVICE IS_CAM_SERIAL IS_CAM_REPLAY IS_CAM_W/H/FPS IS_CAM_EXPOSURE IS_CAM_GAIN IS_CAM_WB IS_YOLO_MODEL IS_YOLO_CONF IS_YOLO_IOU IS_PARALLAX_FACTOR IS_SCAN_FRAMES IS_VISION_RECORD IS_VISION_CONFIG IS_CAM_HEIGHT_MM IS_COLOR_SOURCE IS_TRAY_WB IS_COMPETITION_LOCK IS_REQUIRE_INFO IS_QR_BUDGET_MS IS_REQUIRE_INFO_CONF IS_MATCH_RATIO IS_MATCH_PX_MIN`。`IS_YOLO_CONF` 超出 0.05~0.95、`IS_YOLO_IOU` 超出 0.10~0.95 或不是数字时退回默认值，并写进自检问题（不会再静默检测全空）。颜色投票全落在 `hue_bins` 空隙时判 `unknown`，不再误判成 black。

## 6. calib_config.json（`tray` 段）
| 键 | 说明 |
|---|---|
| `enabled, inner_w_mm, inner_h_mm` | 托盘模式开关与内框尺寸（必填） |
| `rim_mm, rim_height_mm` | 白色外边界到内框距离、壁顶高度 |
| `axes` | 机械臂 +X/+Y 对应 `tray_x / -tray_x / tray_y / -tray_y`（画面右 = tray_x，画面下 = tray_y） |
| `origin_mm` | 托盘原点（画面左上内角）在机械臂坐标系里的位置；无参考点时它就是 `t` |
| `ref_points` | `[{"tray_mm":[x,y], "robot_mm":[x,y]}]`，≥2 个自动求旋转 |
| `residual` | `auto`（默认）/ `none` |
| `focal_px` | 该分辨率下的焦距（像素）。填了它，位姿求解就**不再依赖 `cam_height_mm`**，最稳。算法见下 |
| `fixed_z_mm` | `fixed_px` 那四个角点在托盘底面之上多高（mm）。**点的是壁顶内沿就必须填壁高**（本机 15），填 0 等于把壁顶当底面，抓取点会整体偏 10~30mm。壁顶内沿 = 白色区域边界，是斜看相机时最稳的取点位置 |
| `white_s_max, white_v_min, aspect_tol, jump_px, confirm` | 检测与跟踪参数，一般不用动 |
| `auto_roi, roi_margin_mm` | 托盘内框自动作 ROI |

**怎么算 `focal_px`**（知道摄像头型号时比量镜头高可靠）：`f_px = 镜头焦距mm / 像元尺寸mm × (当前画面宽 / 传感器原生宽)`。
例：25H2838135V1（OV2735，3.0µm 像元，3mm 镜头，原生 1920x1080）→ `f@1920 = 3.0/0.003 = 1000px`。
640x480 若由整幅缩小而来（`tools` 里用 SIFT 比对两档画面可测缩放倍率 s），`f@640 = 1000/s`。
该型号实测 s=0.4444（=480/1080，即整幅等比缩小后水平裁到 640），故 `f@640 = 444px`。
DFOV 规格也能反算（`f = 对角像素/2 / tan(DFOV/2)`，100° 时 f@1920=924px），与上面相差约 8%，取哪个对镜头高的影响只有 2mm 左右。
顶层还有 `cam_height_mm`（镜头光心到托盘底面的垂直高度）、`camera_matrix`、`dist_coeffs`、`image_size`（换分辨率后自检报警）。

## 7. classes.json
- `classes` 顺序 = 模型类别顺序，`./convert.sh` 自动按模型重排；新类别要补 `shape_cn`（中文名）和 `height_mm`。
- `color: null` = 颜色由 HSV 判（推荐：决赛换颜色不用重训）。
- `model_color`: 类名里的颜色（如 `红色镂空立方体` → `red`），**只用于与 HSV 结果做冲突检测和界面提示，不参与 `category`**；无法判定填 `null`。
- `shape_cn`: 形状英文键，是 `category` 的**第一段**（取值 `hollow_cube / tri_prism / hemisphere / cone / penta_prism / tetrahedron / cylinder`）。没有它才退回类名 —— 旧版本用类名当形状，`category` 会随模型颜色变化（现场 `蓝色半球|red|` 就是这条 bug）。
- `info`（可选）：信息声明。字符串 = 直接当信息值；对象 = `{"qr": ..., "text": ..., "template_set": ..., "decode_qr": true|false}`（`text` 优先、`qr` 兜底作声明值，`template_set` 原样进 `diagnostics`）。
- `decode_qr`：旧的顶层写法仍兼容 —— `info.decode_qr` 优先，其次回退顶层 `decode_qr`；**两者都没有就不解二维码**（与旧版本一致，不会凭空多花时间）。
- 分类键 `category = 形状|颜色|信息`，相同才进同一盒；`label` 是大屏显示名（可用 `name` 覆盖）。
- `thin_wall: true`：薄壁/空心件，颜色只取壁上像素，抓取点取外轮廓几何中心。
- `hsv`：颜色区间，浏览器「颜色」页生成。

### 7.1 颜色来源与冲突仲裁（A1）
颜色**以 HSV（锁曝光/增益/白平衡后重新标定）为权威**；模型类名里的颜色只当参考，所以决赛换颜色不用重训。

- `category = 形状|颜色|信息`，**第一段永远是 `shape_cn`**，与类名颜色无关 —— 同一件货多次 scan 的 `category` 只随 HSV 变化。
- `model_color` 与 HSV 投票不一致时：vision 打一条 `颜色冲突: 类=… shape=… 类名色=… HSV=… 模式=… 键=…` WARNING，并把 `model_color / hsv_color / color_conflict / color_source` 写进 `VisionTarget.diagnostics`（复盘 `scan_NNN.json` 里可见，调试台画面里该目标用洋红框 + 「冲突」字样）。
- **比赛模式**（`/tmp/is_competition.lock` 存在）：同一冲突组合键 `形状|类名色|HSV色`（**不用位置作键**，抖料会把货挪走）第一次出现 → 把该目标置信度压到 `color.conflict_conf`（0.43 < `COMMIT_CONF` 0.45）触发一次「抖料重扫」；同一键再次出现 → 采信 HSV（颜色/category/label 同步重算）。每个键最多压一次，记忆 TTL `color.conflict_ttl_s`（默认 60s）。
- **调试模式**：只告警 + 写 diagnostics，不压置信度。
- 调试台「颜色」页/Scan 表格给三个按钮：**按 HSV**（把该组合记进冲突记忆，等同第二次自动采信）、**去颜色页重新采样**（跳颜色页并预选该颜色）、**本类强制颜色**（把 HSV 判定的颜色写进 `classes.json` 该类的 `color`；这是唯一写 `color` 的路径，写前备份 `.bak`，之后该类不再走 HSV 投票）。
- 兜底：即使仲裁没生效，`MAX_UNCERTAIN_RETRY` 用尽后仍会抓取，不会结束本轮。


### 7.2 信息维度与二维码（A4）
- 默认 `require_info: false`：信息解不出时 `info` 保持 `""`、`category` 第三段为空 —— 与之前**逐字段一致**（有回归单测）。
- `require_info: true`（顶层，或 `IS_REQUIRE_INFO=1`）：`category = 形状|颜色|信息`；信息解不出写 `"unknown"`（**不许空串、不许猜**），同时把该目标置信度压到 `detect.require_info_conf`（默认 0.43 < `COMMIT_CONF` 0.45）触发一次「抖料重扫」，并标 `diagnostics.info_missing=true`。
- 解码器：主路径 `cv2.QRCodeDetectorAruco`，不存在才回退 `cv2.QRCodeDetector`。**不再用 `cv2.wechat_qrcode_WeChatQRCode()`**（属 opencv-contrib，板上只有 opencv-python 4.11），也**不做静默降级** —— 实际用了哪个如实写在每个目标的 `diagnostics.qr_decoder`（`aruco` / `qrcode` / `none`）。
- 解码流程：框内裁剪（含 pad）→ 尺度 {1×, 2×, 3×} × 预处理 {原图, OTSU 二值, 自适应阈值, CLAHE}，**命中即停**。
- 时间预算 `detect.qr_budget_ms`（默认 500，`IS_QR_BUDGET_MS` 覆盖）按**整次 scan** 计（不是每件）：耗尽后本次 scan 内不再尝试解码，每件写 `diagnostics.qr_ms` 与 `qr_budget_exhausted`；设 0 = 本次 scan 完全不解码。
- `diagnostics.category_core = "形状|颜色|"`（两段 + 尾随 `|`）**每个目标都有**（走 HSV 回退的也有），供复盘按形状+颜色聚合；`records/runs/<时间>/scan_NNN.json` 里 `category_core` 与 `category` 同时给出，`report.json` 结束时多一份 `category_core_counts`。
- **`classes.json` 目前没有填任何 `info` 值**：现场是否有二维码、码里是什么内容都还没验证，不能编造。要用时按上面格式填 `info` 并标 `[现场待验证]`。

### 7.3 多帧融合匹配半径（A5）
- 旧的固定 `match_px=25` 对小件偏大、对大件偏小；现在半径 = `max(scan.match_px_min, scan.match_ratio × min(框宽, 框高))`（默认 12px / 0.35）。例：80px 框 → 28px，300px 框 → 105px。
- 匹配**与类别无关**：同一件跨帧被认成不同类名也照合，类别按加权投票取多数（2 帧 A + 3 帧 B → 判 B）。
- 「同类优先」只作**距离并列**时的 tie-breaker，不做硬过滤。
- `min_hits` 语义不变（`ceil(帧数 × min_hit_ratio)`）；`service.scan()` 仍会传 `match_px`，只在 `scan.match_ratio <= 0` 时作为旧参数兜底生效。
- `IS_MATCH_RATIO` / `IS_MATCH_PX_MIN` 可临时覆盖。

## 8. 换模型 / 换相机 / 复现
- 换模型：`.pt` 放进 `vision/models/` → `./convert.sh`（`tools/GUIDE.md`）。
- 换工业相机：`"camera": {"type": "hikrobot", ...}`；其它品牌实现 `VisionCamera` 并 `register_camera()`。
- 复现现场：`python3 tools/vision_tool.py web --replay records/scans/raw`。

## 9. 常见问题
| 现象 | 处理 |
|---|---|
| 托盘未定位 / 长宽比报错 | 外围贴黑边；尺寸方向（W=画面左右）别填反；相机尽量垂直；强反光时降曝光 |
| 「托盘/相机位置变化」告警 | 相机被碰了，已自动重新锁定；做一次点哪去哪验证。该提示在自检里保留 30 秒后自动消失，自检报告的 `tray.last_event` 里仍可查 |
| 越靠边越抓偏 | 做畸变页；填镜头高（视差）；参考点补到 9 个启用残差修正 |
| 整体平移几毫米 | 参考点的机械臂坐标或托盘坐标量错；用点击方式重加 |
| 蓝/浅蓝、黄/橙混淆 | 锁白平衡；颜色页重新采样 |
| BPU 不可用 | `sudo deploy/bpu_doctor.sh`，必要时 `./is.sh migrate` |
