# vision/ 使用指导（视觉模块）

> 对外契约：`interfaces.py`（上层只用这里的东西）。识别参数：`vision_config.json`；坐标标定：`calib_config.json`（例子见 `calib_config.example.json`）。
> **所有需要实时画面的调参都在浏览器里做**：`./is.sh web` → 笔记本打开 `http://<板子IP>:8090`，板子不需要显示器。

## 1. 边界

对外接口不变（UI/、debug/、MessegeCenter 不用改）：`create_vision_service(bus, backend, **kw)`、`VisionServiceBase` 全部方法、`scan()`、`get_fresh_frames()`、`detector.color_ranges / get_color_mask / update_color_range`、`VisionTarget` 字段、心跳 `module_name="vision"` 1Hz、`TOPIC_VISION_TARGET`、`from vision.camera import PhysicalCamera, SyntheticCamera`。

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
  → 高度视差         p' = c + (p - c)·(1 - h/H_cam)，h = 货物高度 × parallax_factor
  → 托盘单应         每次 scan 自动检测托盘四角，按内框尺寸映射成托盘坐标 (mm)
  → 刚体变换         [xr,yr] = R·[xt,yt] + t：1 个参考点 + axes，或 ≥2 个参考点自动求 R
  → 残差多项式       ≥5 个参考点一阶、≥9 个二阶，补镜头残余畸变 / 装配倾斜 / 机械臂误差
  → 机械臂 (x_mm, y_mm)
```

**托盘检测**（`tray.py`）：HSV 低饱和高亮分割（亮度阈值自动）→ 大核闭运算填掉货物留下的洞 → 最大外轮廓 → 去畸变 → 凸包粗四边形 → 每条边只取贴着凸包的轮廓点（被货物挡住凹进去的点自动排除）做 Huber 直线拟合 → 相邻边求交得到亚像素角点 → 长宽比与配置比对（默认容差 8%）。
**跟踪**：多帧中值；单帧跳变（手伸进画面）直接忽略；连续 3 帧一致的新位置才接受（相机被碰后自动重新锁定，并在自检/执行页告警）。检测失败时沿用上次结果，启动时先用保存的角点。
**自动 ROI**：没有手动设 `detect.roi` 时，用托盘内框外扩 6mm 作 ROI，托盘外的误检直接丢弃。

### 现场标定步骤（全部在浏览器）
1. **托盘页**：勾选启用，填内框 W（画面左右方向）× H（上下方向）、外沿 rim（白色外边界到内框）、壁高、镜头高 → 保存并应用。画面出现绿框（白色外边界）、青框（内框）、红/绿箭头（托盘 +x/+y）。长宽比报错就检查尺寸或相机是否太斜。
2. **参考点**：
   - 尺子量：固定点到「画面左上内角」两条内边的距离 = 托盘坐标，再填它的机械臂坐标 → 加参考点；只有 1 个点时选好「机械臂 +X / +Y 对应托盘哪个方向」。
   - 或「标定/ROI」页 → 标定模式 → 点击画面上的标记 → 输入（`--arm` 时自动读取）机械臂坐标 → 求解。托盘模式下点击的点自动换算成托盘坐标存为参考点，**相机再动也有效**。
   - 2 个点以上会给出旋转角、镜像、比例（偏离 1 超过 3% 说明尺寸或坐标填错）和逐点误差。
3. **畸变页**（推荐做一次）：棋盘格贴硬板，在托盘区域各位置、各倾角「拍一张」，覆盖画面四角，15~25 张后「计算内参」，RMS < 0.5px 为好；勾选「显示去畸变结果」看直线是否变直。没有棋盘格时在托盘页「用托盘直边估计畸变」（只估 k1，对桶形畸变够用）。**改内参后托盘会自动重新锁定，参考点不用重做。**
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
| `scan.frames / min_hit_ratio / match_px` | 融合帧数 / 出现比例下限 / 匹配距离 |
| `record.enabled / dir / keep` | 每次 scan 存原图+标注图+json（`records/scans`），也是量化校准图来源 |

环境变量临时覆盖：`IS_CAM_TYPE IS_CAM_DEVICE IS_CAM_SERIAL IS_CAM_REPLAY IS_CAM_W/H/FPS IS_CAM_EXPOSURE IS_CAM_GAIN IS_CAM_WB IS_YOLO_MODEL IS_YOLO_CONF IS_YOLO_IOU IS_PARALLAX_FACTOR IS_SCAN_FRAMES IS_VISION_RECORD IS_VISION_CONFIG IS_CAM_HEIGHT_MM`。`IS_YOLO_CONF` 超出 0.05~0.95、`IS_YOLO_IOU` 超出 0.10~0.95 或不是数字时退回默认值，并写进自检问题（不会再静默检测全空）。颜色投票全落在 `hue_bins` 空隙时判 `unknown`，不再误判成 black。

## 6. calib_config.json（`tray` 段）
| 键 | 说明 |
|---|---|
| `enabled, inner_w_mm, inner_h_mm` | 托盘模式开关与内框尺寸（必填） |
| `rim_mm, rim_height_mm` | 白色外边界到内框距离、壁顶高度 |
| `axes` | 机械臂 +X/+Y 对应 `tray_x / -tray_x / tray_y / -tray_y` |
| `ref_points` | `[{"tray_mm":[x,y], "robot_mm":[x,y]}]` |
| `residual` | `auto`（默认）/ `none` |
| `white_s_max, white_v_min, aspect_tol, jump_px, confirm` | 检测与跟踪参数，一般不用动 |
| `auto_roi, roi_margin_mm` | 托盘内框自动作 ROI |
顶层还有 `cam_height_mm`、`camera_matrix`、`dist_coeffs`、`image_size`（换分辨率后自检报警）。

## 7. classes.json
- `classes` 顺序 = 模型类别顺序，`./convert.sh` 自动按模型重排；新类别要补 `shape_cn`（中文名）和 `height_mm`。
- `color: null` = 颜色由 HSV 判（推荐：决赛换颜色不用重训）。
- `decode_qr: true`：解码二维码，内容参与分类。
- 分类键 `category = 形状|颜色|信息`，相同才进同一盒；`label` 是大屏显示名（可用 `name` 覆盖）。
- `thin_wall: true`：薄壁/空心件，颜色只取壁上像素，抓取点取外轮廓几何中心。
- `hsv`：颜色区间，浏览器「颜色」页生成。

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
