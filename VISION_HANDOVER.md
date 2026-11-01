# 视觉调试交接清单（从电脑传文件到 RDK X5，到视觉调完）

> 给接手视觉调试的同学。按顺序做，每一步做完在 □ 里打勾。
> 约定：「电脑」= 你的 Windows 笔记本；「板子」= RDK X5 小电脑；`<板子IP>` 换成实际 IP；`$P` = 板子上的工程目录。
> 你只负责**视觉**：`UI/`（比赛流程）和 `communication/`（通讯）已经调好，不要改代码。
>
> **相机参数与斜视投影校正**（focal 444px、倾角 44.15°、`fixed_z_mm` 的来历、高度投影验收）另见
> `HANDOVER_CAMERA_PROJECTION.md`。

---

## 0. 开工前准备（10 分钟）

| □ | 要准备的东西 | 说明 |
|---|---|---|
| □ | 工程压缩包 `InteillgentStoring_pc_v3_review_fix.zip` | 交接人给你 |
| □ | 板子 IP、SSH 账号密码 | RDK 系统常见默认账号 `sunrise/sunrise` 或 `root/root`，以交接人告知为准 |
| □ | 电脑和板子在同一网络（同一路由器 / 网线直连） | 电脑上 `ping <板子IP>` 能通 |
| □ | 托盘**内框**尺寸 W×H（mm）、白色外沿宽度、托盘壁高、镜头到托盘面高度 | 卷尺量。W = 相机画面里**左右**方向，H = **上下**方向，别填反 |
| □ | 卷尺 / 钢尺、比赛用全部货物、比赛灯光 | 颜色标定和识别测试要用 |
| □ | 棋盘格（可选，推荐） | A4 打印贴硬板，记下**内角点**数（如 9×6）和格子边长 mm |
| □ | 机械臂、下位机、储物盘通电，USB 连好 | 「点哪去哪」验标定要用 |
| □ | Chrome / Edge 浏览器 | 所有调参都在浏览器里 |

---

## 1. 把文件从电脑传到板子

**电脑上打开 PowerShell**（Win 键 → 输入 powershell），Windows 10/11 自带 `ssh` 和 `scp`：

```powershell
ssh root@<板子IP>                 # 第一次会问 yes/no，输入 yes，再输入密码；能登上说明网络和账号没问题，然后 exit
scp D:\下载\InteillgentStoring_pc_v3_review_fix.zip root@<板子IP>:/root/
```
（路径换成 zip 实际所在位置。不习惯命令行可以用 WinSCP / MobaXterm 拖进 `/root/`。）

| □ | 检查 |
|---|---|
| □ | 板子上 `ls -lh /root/*.zip` 能看到文件，大小和电脑上一致 |

> **只传 zip，不要在 Windows 上解压后再传文件夹**：Windows 会把脚本换行改成 CRLF、丢掉可执行权限，`.sh` 会跑不起来。

---

## 2. 在板子上替换工程（保留现场数据）

以下都在板子上执行（`ssh root@<板子IP>` 登录后）。

### 2.1 找到现在的工程目录
```bash
docker inspect intelligentstoring --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}' | grep /app
```
输出 `XXXX -> /app` 里的 `XXXX` 就是工程目录：
```bash
P=XXXX            # 例如 P=/root/InteillgentStoring_pc
```
没有容器（第一次部署）：直接 `P=/root/InteillgentStoring_pc`，跳过 2.2 的备份，做完 2.3 后看第 3 节。

### 2.2 停程序、备份旧工程
```bash
cd $P && ./is.sh race off 2>/dev/null; ./is.sh stop 2>/dev/null
B=${P}_bak_$(date +%m%d_%H%M)
mv $P $B && echo "旧工程备份在 $B"
```

### 2.3 解压新工程到**原来的路径**（容器挂载路径不变，就不用重建容器）
```bash
cd /tmp && rm -rf InteillgentStoring_pc && unzip -q /root/InteillgentStoring_pc_v3_review_fix.zip
mv /tmp/InteillgentStoring_pc $P
chmod +x $P/*.sh $P/deploy/*.sh
```

### 2.4 把现场数据拷回来（第一次部署跳过）
```bash
for f in vision/calib_config.json vision/models deploy/.bpu_bridge.args tools/convert_config.json \
         tools/.web_token tools/calib_images tools/chess_images records dataset; do
  [ -e "$B/$f" ] && cp -a "$B/$f" "$P/$(dirname $f)/" && echo "已保留 $f"
done
# 相机参数和颜色标定: 旧板子上调过就拷回旧的, 没调过就用新包的。先看差异:
diff $B/vision/vision_config.json $P/vision/vision_config.json; diff $B/vision/classes.json $P/vision/classes.json
# 确认旧的是调过的再执行:  cp $B/vision/vision_config.json $B/vision/classes.json $P/vision/
```

| □ | 检查 |
|---|---|
| □ | `ls $P` 能看到 `README.md is.sh convert.sh vision UI ...` |
| □ | 有旧模型的话 `ls $P/vision/models` 里还在 |

---

## 3. 容器和 BPU 环境

```bash
cd $P
docker inspect -f '{{.Config.Image}}' intelligentstoring     # 应该是 ubuntu:22.04
```
| 情况 | 做什么 |
|---|---|
| 是 `ubuntu:22.04` | `docker restart intelligentstoring` 即可 |
| 不是（旧 ultralytics 镜像）或没有容器 | `sudo ./is.sh migrate`（需联网，10~20 分钟，旧容器会改名保留） |

然后诊断：
```bash
sudo deploy/bpu_doctor.sh          # BPU / 容器诊断，有 ✘ 就把整段输出发给交接人
./is.sh precheck                   # 赛前自检清单 (相机/模型/BPU/标定/下位机/大屏/启动键)
```
| □ | 检查 |
|---|---|
| □ | `bpu_doctor` 没有 ✘（有模型 `.bin` 时 BPU 应可用） |
| □ | precheck 里「相机」是 ✔；「标定」此时报未标定是正常的，后面会做 |

---

## 4. 打开浏览器调参台

```bash
cd $P
./is.sh race off                   # 比赛模式开着时调参台会拒绝启动
./is.sh web --arm                  # --arm = 连接下位机，标定时自动读机械臂坐标、可用「点哪去哪」
```
启动日志里有一行 **`网页口令: 123456`**。电脑浏览器打开 `http://<板子IP>:8090`，输入这 6 位口令（浏览器记住 30 天；口令也存在板子上 `$P/tools/.web_token`）。

- 这个窗口不要关；调完按 `Ctrl+C` 退出。
- 只想看画面不想动机械臂：去掉 `--arm`。
- 网页打不开：确认 SSH 窗口里调试台还在运行、电脑能 ping 通板子、板子防火墙没拦 8090。

| □ | 检查 |
|---|---|
| □ | 状态页有实时画面，帧率 > 10 |
| □ | 启动日志有「已连接下位机」（用了 `--arm` 时） |

---

## 5. 视觉调试步骤（全部在浏览器里，按顺序）

**光照先定下来**：用比赛时的灯光，关掉会变化的环境光，后面所有步骤都在这个光照下做。光照一变，第 5.1 和 5.4 步要重做。

### 5.1 相机页：锁定曝光、增益、白平衡
1. 托盘里放上全部颜色的货物。
2. 拖曝光：白色托盘不过曝（不发白一片）、深色货物看得清；有反光就降曝光、加增益。
3. 白平衡固定一个色温（别用自动，否则蓝/浅蓝、黄/橙会漂）。
4. 点「应用并保存」（写入 `vision/vision_config.json`）。

| □ | 验收：托盘边缘清晰、每种颜色肉眼可分、画面不闪 |
|---|---|

### 5.2 畸变页：一键校正镜头畸变（约 2 分钟）
1. 「畸变」页点「下载打印纸」，A4 打印（**缩放比例无所谓**，不用量格子）。也可用 `tools/calib_board_A4.pdf`。
2. 纸平铺在托盘上，尽量盖满托盘，压平、别反光、别拱起。
3. 点「一键校正」：自动拍 5 帧平均 → 认格子（画面出现橙点）→ 计算 → 写入 → 自动切到去畸变画面。托盘模式会自动重新定位托盘。
4. 想更准：把纸挪到画面另一侧或角落，点「再拍一张叠加」，2~3 张就够。
5. 不满意点「撤销」，恢复上一次的内参。

看结果：`格线拟合误差 2.3px → 0.04px` 这类数字，箭头后面越小越好。提示「畸变很小，不需要校正」说明镜头本身很直，跳过这一步即可。

| □ | 验收：校正后格线拟合误差 ≤ 0.3 px；去畸变画面里托盘边、格子线是直的 |
|---|---|

- 已有照片也可离线算：`python3 tools/calib_intrinsics.py --one 照片1.png 照片2.png`
- 完整多角度棋盘格标定还在页面「完整棋盘格标定」折叠里，一般不需要。
- 改了畸变后，如果用的是固定托盘（5.3B），像素坐标不用重量，程序会自动换算；参考点和「点哪去哪」建议重新验一遍。

### 5.3 托盘页：托盘坐标系
1. 勾「启用托盘模式」，填：内框 W（画面左右）、内框 H（画面上下）、外沿 rim、壁高、镜头高 → 保存并应用。
2. 画面应出现：**绿框**贴白色托盘外边界、**青框**贴内框、红箭头 = 托盘 +x（向右）、绿箭头 = 托盘 +y（向下），原点在画面左上的内框角。
3. 长宽比报错 → 检查 W/H 是否填反、尺寸是否量错、相机是否太斜；托盘周围贴黑边能明显提高稳定性。
4. 手伸进画面再拿开，框应保持不跳。

| □ | 验收：绿框/青框与实物贴合（误差 ≤ 2 像素），状态页「问题」里没有托盘相关提示 |
|---|---|

### 5.3B（可选）固定托盘位置：直接给四个角，不自动检测
托盘和相机都固定不动，或者自动检测不稳定（反光、没贴黑边）时用。坐标系：**画面左上角 = (0,0)，x 向右，y 向下，单位像素**。

> **相机是斜着看托盘的**（托盘在画面里是上窄下宽的梯形）：一定要给**四个角**，不能用矩形 x,y,w,h。程序用四个角求透视映射，并算出相机倾角和位置，货物有高度时按真实三维几何换算（斜看时旧的“绕画面中心缩放”视差公式会偏好几毫米）。

**推荐做法（网页，一分钟）**：
1. 托盘页先填内框 W、H、**壁高**、**镜头高**（镜头到托盘底面，卷尺量，±2cm 即可）→ 保存并应用。
2. 勾「自动吸附到白色边缘」→ 点「点四角」→ 在画面上依次点托盘内框的 4 个角（顺序随意，点个大概就行）。
3. 点满 4 个自动保存。吸附后的角是**壁顶内沿**（远处能看到内壁、近处被壁顶挡住，白色区域的边界都在壁顶高度），程序按壁高处理，所以壁高要填对。
4. 状态栏「perspective」显示：相机倾角、估计镜头高、焦距来源。估计镜头高和实际差很多 → W/H 填反或点错角。
5. 取消固定：「取消固定 (恢复自动检测)」。

**命令行（备用）**：
```bash
# 四个角 (左上 右上 右下 左下)；--mm 内框实际尺寸 (左右 上下)；--cam-height 镜头到底面高度
python3 tools/set_tray_fixed.py --corners 102,64 538,66 537,392 101,390 --mm 240 180 --cam-height 380
# 点的是壁顶内沿而不是底面内角时加 --z-mm 壁高
python3 tools/set_tray_fixed.py --corners ... --mm 240 180 --z-mm 15
# 相机正对托盘 (画面里是长方形) 才能用矩形
python3 tools/set_tray_fixed.py --x 102 --y 64 --w 436 --h 327 --mm 240 180
```
- （可选）不用参考点，直接给托盘原点（内框左上角）在机械臂坐标系里的位置：`--origin-mm X0 Y0 --axes tray_x tray_y --clear-refs`。**有参考点时以参考点为准**，origin_mm 不生效。
- 重启调试台 / 比赛程序生效。查看：`--show`；恢复自动检测：`--off`。量取时的分辨率记在配置里，换分辨率自动换算；相机或托盘一旦移动就要重新点。

**焦距从哪来**（一般不用管）：托盘页「焦距」填了就用它 > 棋盘格完整标定 > 用镜头高反推 > 托盘四角自估（倾角 ≥12° 才行）。都没有时退回旧公式。「透视」选 off 可强制旧公式。

| □ | 验收：青框四条边压在托盘内沿上；红/绿箭头从左上内角出发；放一个已知高度的货物，「点哪去哪」夹爪对准货物中心，误差 ≤ 2 mm |
|---|---|

### 5.4 参考点：托盘坐标 → 机械臂坐标（最关键）
推荐方法（需要 `--arm`）：「标定/ROI」页 → 标定模式：
1. 在托盘里放一个小标记（如十字贴纸）。
2. 用下位机/调试工具把夹爪中心对准标记正上方。
3. 在画面上点击标记中心 → 机械臂坐标会自动读入 → 加点。
4. 换位置重复。**至少 2 个点，建议 9 个**（3×3 均匀分布，含四角附近）。点 ≥5 个自动启用一阶残差修正，≥9 个启用二阶。
5. 点「求解并保存」，看结果：旋转角、是否镜像、比例、逐点误差。

备选（没有 `--arm`）：托盘页手动填参考点：用尺子量标记到「画面左上内角」两条内边的距离 = 托盘坐标，再填该点的机械臂坐标。只有 1 个点时一定要选对「机械臂 +X / +Y 对应托盘哪个方向」。

| □ | 验收：比例在 0.97~1.03 之间（偏离大说明尺寸或坐标填错）；逐点误差平均 ≤ 2 mm |
|---|---|

### 5.5 颜色页：HSV 颜色标定
1. 选一种颜色 → 在画面上点该颜色的货物 2~5 次（不同货物、不同位置、亮面和暗面都点）。
2. 每种颜色都做完 → 点「生成颜色区间」（写入 `vision/classes.json`）。
3. 点 Scan 检查颜色是否全部正确。易混的蓝/浅蓝、黄/橙要多采几个点。

| □ | 验收：全部货物颜色识别正确；不会出现 `unknown` |
|---|---|

### 5.6 状态页：点哪去哪验收坐标（需要 `--arm`）
1. 勾「点哪去哪」，依次点击托盘里 9 个位置（四角、四边中点、中心）。
2. 机械臂会开到该点上方停 0.8 秒再退回（**不下潜、不夹取**），页面显示目标和实际到达坐标的误差。
3. 肉眼看夹爪中心是否正对点击位置。

| □ | 验收：9 点平均误差 ≤ 2 mm，最大 ≤ 4 mm |
|---|---|
| 不合格 | 越靠边越偏 → 重做 5.2 畸变、确认镜头高；整体平移 → 5.4 参考点坐标量错，用点击方式重加；某个方向比例不对 → W/H 填反或量错 |

### 5.7 识别测试
1. 按比赛方式随机摆放全部货物（含堆叠、贴边、倒伏），点 **Scan**（和比赛一样多帧融合）。
2. 记录每次：漏检、误检、类别错、颜色错各几件。重复 10 次以上，每次换摆法。
3. 识别错的画面：在「复盘」页点「归档」（进回归集 `dataset/regression/` 和难例 `dataset/hard_cases/`），交给训练的人。
4. 置信度可用画面下方滑块临时试，确定后改 `vision/vision_config.json` 的 `model.conf`。

| □ | 验收：10 次 Scan 类别、颜色全对；同一件货物连续 Scan 坐标抖动 ≤ 1 mm |
|---|---|

### 5.8 （需要时）换模型
训练出新的 `.pt` 后按**第 9 节**操作，换完回到 5.7 再测一遍。

---

## 6. 收尾

```bash
# 调试台窗口 Ctrl+C 退出, 然后:
cd $P
./is.sh precheck                   # 全部 ✔ (下位机、大屏、启动键也要通电接好)
./start_competition.sh             # 实跑一轮: 执行页 http://<板子IP>:8088 点「开始」, 看完整分拣
```
| □ | 检查 |
|---|---|
| □ | precheck 全部 ✔（或只剩已知可接受的 ⚠） |
| □ | 实跑一轮：每件放对盒，大屏逐件显示，执行页无红色报错 |
| □ | 复盘：调参台「复盘」页或 `records/runs/` 最新一轮 `report.json` 里 `ledger` 没有违规 |
| □ | 需要开机自动待机时：`./is.sh race on` |

**备份调好的配置**（电脑 PowerShell 执行，交给交接人存档）：
```powershell
scp root@<板子IP>:<工程目录>/vision/calib_config.json root@<板子IP>:<工程目录>/vision/vision_config.json root@<板子IP>:<工程目录>/vision/classes.json D:\备份\
```

---

## 7. 常见问题

| 现象 | 处理 |
|---|---|
| `./is.sh web` 提示比赛程序在运行 | `./is.sh race off`，执行页点「停止」后再开 |
| 网页 401 / 一直要口令 | 口令看启动日志或 `cat $P/tools/.web_token`；也可直接打开 `http://<板子IP>:8090/?t=口令` |
| 画面黑 / 无图 | 相机 USB 重插；`./is.sh precheck` 看相机项；海康相机确认 MVS 驱动和序列号 |
| 托盘未定位 / 长宽比报错 | 外围贴黑边；W/H 别填反；降曝光去反光；相机尽量垂直 |
| 「托盘/相机位置变化」告警 | 相机被碰了，已自动重锁；重做 5.6 验收 |
| 颜色混淆 | 锁白平衡后重做 5.5 |
| BPU 不可用（很慢） | `sudo deploy/bpu_doctor.sh`，必要时 `sudo ./is.sh migrate` |
| 坐标整体偏几毫米 | 5.4 参考点重加 |
| 新包有问题要回退 | `mv $P ${P}_new && mv $B $P && docker restart intelligentstoring` |

更多细节：`vision/GUIDE.md`（视觉原理与参数）、`tools/GUIDE.md`（调参台与模型转换）、`deploy/OPERATIONS.md`（运维）。

---

## 8. 交回给交接人的东西

| □ | 内容 |
|---|---|
| □ | 三个配置文件：`calib_config.json`、`vision_config.json`、`classes.json` |
| □ | 托盘尺寸实测值、镜头高、畸变校正前后格线误差、参考点数和平均误差 |
| □ | 点哪去哪 9 点误差表 |
| □ | 识别测试记录（第几次、漏检/误检/错类/错色），归档的难例 |
| □ | 实跑一轮的结果（件数、用时、错盒数），`records/runs/` 最新目录 |
| □ | 遇到但没解决的问题 |

---

## 9. 更换 .pt 模型（训练出新模型后）

**原理**：程序以 `vision/models/` 里**修改时间最新的 .pt** 为准，自动用它转出来的 `.bin`（BPU，最快）> `.onnx`（CPU）。所以换模型 = 把新 `.pt` 放进去 + 运行一次 `./convert.sh`。
`.bin` 只能在 x86 电脑上编译（地瓜 `hb_mapper` 没有 ARM 版），`./convert.sh` 会自动通过 SSH 交给笔记本的 WSL 编译，再把结果取回板子。

### 9.1 一次性准备：笔记本当编译机（只做一次，约 20 分钟）
**笔记本 Windows（管理员 PowerShell）**：
```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd; Set-Service sshd -StartupType Automatic
wsl -l -v                          # 要有 Ubuntu-22.04；没有: wsl --install -d Ubuntu-22.04
ipconfig                           # 记下笔记本 IP (和板子同网段的那个)
```
**笔记本 WSL（开始菜单打开 Ubuntu 22.04）**：
```bash
sudo apt update && sudo apt install -y python3-pip
python3 -m pip install --user rdkx5-yolo-mapper
hb_mapper --version                # 能打印版本号即可
```
**板子上**：
```bash
cd $P
./convert.sh --setup-remote 你的Windows用户名@笔记本IP
#   WSL 默认发行版不是 22.04 时加:   --wsl-distro Ubuntu-22.04
#   笔记本还没装 hb_mapper 时加:      --install-remote
```
| □ | 验收：最后显示连接成功并打印出 `hb_mapper` 版本（不配免密也行，每次转换输一次 Windows 密码） |
|---|---|

### 9.2 把 .pt 传到板子
训练完的模型在训练电脑的 `runs/detect/train*/weights/best.pt`。**先改个带日期的名字**（例如 `best_1027.pt`），方便以后回退，不要覆盖旧的 `.pt`：
```powershell
scp D:\训练\runs\detect\train\weights\best.pt root@<板子IP>:<工程目录>/vision/models/best_1027.pt
```
训练时的类别：形状（以及污渍/文字/图案变体），**颜色不进类别**（颜色由 HSV 判，换颜色不用重训）。

### 9.3 转换（板子上，一条命令，约 5~15 分钟）
```bash
cd $P
./is.sh stop; ./is.sh race off     # 停掉比赛程序 / 调试台，避免同时占用 BPU
./convert.sh                       # 自动取 models/ 里最新的 .pt
```
它依次做：
1. 导出 ONNX，并和 `.pt` 原生预测逐框对拍（不一致会停下报错）；
2. 按模型类别同步 `vision/classes.json`、`tools/data.yaml`；
3. **ONNX 立即可用**（CPU 推理，结果正确但慢）；
4. 用 `records/scans/raw` 里的现场原图生成量化校准数据；
5. SSH 交给笔记本 `hb_mapper` 编译 → 取回 `.bin`；
6. 板上 BPU 结果与 ONNX 对比，**一致才启用 `.bin`**；
7. 旧模型移到 `vision/models/old/`。

| 提示 | 处理 |
|---|---|
| 校准图不足 20 张 | 托盘里换摆法放货物，`docker exec -it intelligentstoring bash -lc "cd /app && python3 tools/vision_tool.py capture --count 60"`，再 `./convert.sh` |
| 笔记本连不上 / 没开机 | 停在 ONNX（能用但慢）；笔记本开机后再跑一次 `./convert.sh`，自动只补编译 |
| 提示「已是最新」 | 这个 .pt 已经转过；要重转加 `--force` |
| BPU 与 ONNX 不一致（余弦相似度 < 0.99） | `./convert.sh --force --cal-type max`（或 `kl`）；仍不行 `--int16` |

### 9.4 补类别信息（有新类别时）
打开 `vision/classes.json`，转换后新类别已按模型顺序加进去，给它补：
- `shape_cn`：大屏显示的中文名（如「圆柱体」）；
- `height_mm`：货物高度（用于视差校正，抓取点更准）；
- 薄壁 / 空心件加 `"thin_wall": true`；
- 带二维码要解码的加 `"decode_qr": true`。

### 9.5 生效与验证
```bash
./is.sh precheck                   # 「模型」显示新模型名，「BPU 加速」✔
./is.sh web --arm                  # 回到 5.7 识别测试；颜色有变化再做 5.5
```
比赛程序已在运行时，执行页点「一键重启」生效。

| □ | 验收：precheck 模型名是新的、BPU ✔；5.7 识别测试 10 次全对；单次 Scan 用时不比旧模型慢 |
|---|---|

### 9.6 回退到旧模型
```bash
cd $P/vision/models
ls . old/                                   # 先看文件名: 同一个模型转出的文件都以 .pt 的名字开头
mkdir -p bad && mv best_1027* bad/           # 新模型的 .pt/.onnx/.bin/.json 全部移走
mv old/<旧模型名>* . && touch <旧模型名>.pt   # 旧的拿回来，touch 让它重新变成"最新"
cd $P && ./is.sh precheck                    # 确认模型名是旧的
```

