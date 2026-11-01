# tools/ 使用指导

| 文件 | 用途 | 在哪跑 |
|---|---|---|
| `../convert.sh` → `convert_model.py` | 一键 .pt → ONNX → BPU .bin，同步类别、对拍、上板验证 | **板上**（编译自动交给笔记本）或 x86 电脑 |
| `convert_config.json` | 远程编译机（笔记本）配置，`--setup-remote` 生成 | — |
| `vision_tool.py` | 网页调试台 / 自检 / 采集 / 测速 / 回归集 | 板上 |
| `train.py` / `data.yaml` | 训练（data.yaml 的 names 由转换脚本自动同步） | 电脑 |
| `calib_intrinsics.py` | 棋盘格内参标定（浏览器「畸变」页调用同一函数；也可命令行） | 板上/电脑 |

## 1. 一键转换（SSH 进板子，换 .pt，一条命令）

```bash
# 把训练好的 best.pt 覆盖到 vision/models/（文件名随意，取最新的 .pt）
scp runs/detect/train/weights/best.pt root@<X5_IP>:<工程目录>/vision/models/
ssh root@<X5_IP>
cd <工程目录> && ./convert.sh
```

它会依次：导出 6 头 ONNX → 与 .pt 逐框对拍 → 同步 `vision/classes.json` / `data.yaml` → **ONNX 立即可用**（CPU 推理）→ 用 `records/scans/raw`（比赛/调试时自动存的原图）生成量化校准数据 → 发给笔记本编译 → 取回 `.bin` → **板上与 ONNX 对比，一致才切换到 BPU** → 旧模型移到 `models/old/`。最后在执行页点「一键重启」生效。

- **为什么要笔记本**：地瓜编译器 `hb_mapper` 只有 x86_64 版本，ARM 的 X5 上无法编译。
- 笔记本不在线：停在 ONNX（结果正确，只是慢），开机联网后再 `./convert.sh` 自动补编译。
- 同一个 .pt 再跑会提示"已是最新"；`--force` 重新转换。
- 校准图不足 20 张：`python3 tools/vision_tool.py capture --count 60`（换货物摆放/数量）后重跑。
- 程序选模型的规则：以 `models/` 里最新的 .pt 为准，用它转出的 `.bin` > `.onnx`；还没转换时临时直接用 .pt 并在自检里提示。

## 2. 笔记本编译机（一次性配置，Windows + WSL）

**笔记本（Windows，管理员 PowerShell）**
```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0     # 开启 Windows 自带 SSH 服务
Start-Service sshd; Set-Service sshd -StartupType Automatic
wsl -l -v                                                         # 确认有 Ubuntu-22.04 (没有: wsl --install -d Ubuntu-22.04)
```
**WSL（Ubuntu 22.04，自带 Python 3.10）**
```bash
sudo apt update && sudo apt install -y python3-pip
python3 -m pip install --user rdkx5-yolo-mapper     # 地瓜官方 x86 编译工具链 (pip 版)
hb_mapper --version
```
**板子上**
```bash
./convert.sh --setup-remote 你的Windows用户名@笔记本IP              # 默认 Windows 模式
#   默认 WSL 不是 22.04 时: --wsl-distro Ubuntu-22.04
#   工具链装在 conda 环境里: --remote-init "source ~/miniconda3/bin/activate rdk"
#   笔记本是 Linux 或 WSL 里自己开了 sshd: --remote-type linux
#   还没装 hb_mapper: 加 --install-remote 自动装
```
它会生成板子的 SSH 公钥并打印添加方法（不配免密也能用，每次转换输一次 Windows 密码），然后测试连接和 `hb_mapper`。

原理：板子 `ssh 用户@笔记本 wsl -e bash -lc "..."`，把 ONNX + 校准数据打包传过去，在 WSL 里跑 `hb_mapper checker / makertbin`，再把 `.bin` 和日志取回。笔记本上不需要放工程代码。

## 3. 电脑上直接转换（备选）

```bash
python3 tools/convert_model.py best.pt --calib 现场图片目录 --deploy root@<X5_IP>:<工程目录>
python3 tools/convert_model.py best.pt --onnx-only
```
编译器自动探测：本机 `hb_mapper`（pip 版）> OE Docker `openexplorer/ai_toolchain_ubuntu_20_x5_cpu:v1.2.8`。
`--deploy` 后记得把同一个 .pt 也拷到板子 `vision/models/`（板子按最新 .pt 选模型）。

### 常用参数
| 参数 | 作用 |
|---|---|
| `--force` | 同一个 .pt 重新转换 |
| `--calib DIR` / `--calib-max 50` | 校准图片目录 / 张数 |
| `--cal-type max\|kl\|mix` | 余弦相似度 < 0.99 时换校准方法 |
| `--int16` | 全 int16 量化（更准更慢） |
| `--imgsz H W` | 输入尺寸（默认按相机比例 640x480 → 480 640） |
| `--compiler auto\|local\|docker\|remote\|none` | 编译方式 |

## 4. 浏览器调试台（所有实时画面调参都在这里；比赛程序运行时会拒绝启动）

```bash
./is.sh web [--arm]                            # = python3 tools/vision_tool.py web, 浏览器打开 http://<X5_IP>:8090
python3 tools/vision_tool.py check             # 自检 + 实拍 3 次
python3 tools/vision_tool.py capture --count 60
python3 tools/vision_tool.py bench
python3 tools/vision_tool.py regress           # 回归集对比
```
**口令**：调试台能动机械臂、改标定，所以要口令。第一次启动生成 6 位数字存在 `tools/.web_token`（不进 git，重启不变），启动日志会打印；浏览器第一次打开输入一次，记住 30 天，也可以直接收藏 `http://<X5_IP>:8090/?t=口令`。综合工作台拉起调试台时把口令显示在视觉页。
- 固定口令：`IS_WEB_TOKEN=xxxx ./is.sh web`；关掉口令：`--no-auth`（不建议）。
- `--host 127.0.0.1` 只允许板上本机访问（笔记本就打不开了，默认 `0.0.0.0`）。
- 机械臂运动加了锁，同时只允许一次「点哪去哪」；坏请求回 400/500 并写日志，不影响调试台继续用。

| 页 | 功能 |
|---|---|
| 状态 | 相机/引擎/标定/问题；点击画面看机械臂坐标与 HSV；**点哪去哪**（`--arm`）：机械臂开到点击处上方停 0.8s 再取消，不下潜不夹取，肉眼验标定 |
| 托盘 | 白色托盘自动定位：填内框尺寸/外沿/壁高/镜头高、参考点、机械臂轴向；画面叠加外边界/内框/坐标轴；托盘直边估计畸变 |
| 畸变 | 棋盘格逐张拍（检测到角点才保存）→ 计算内参写入 `calib_config.json`；去畸变画面对比 |
| 标定/ROI | 点击画面 + 机械臂坐标（`--arm` 自动读）：托盘模式下作为参考点，点位模式下做 9 点单应；手动 ROI |
| 相机 | 曝光/增益/白平衡滑块实时生效，一键写回 `vision_config.json`（比赛锁定） |
| 颜色 | 选颜色→点货物采样（每色 2~5 点）→「生成」自动写 `classes.json` 的 HSV 区间。换场地灯光 1 分钟重标 |
| 大屏 | 按最近一次 Scan 渲染比赛大屏，与装置屏一致 |
| 复盘 | 每轮报告：每件结果/截图/识别画面/流程。识别错的画面「归档」→ `dataset/regression/`（回归集）+ `dataset/hard_cases/`（下次训练要标注的难例） |

## 要点
- 训练只认**形状**（+ 污渍/文字/图案变体），颜色交给 HSV：决赛换颜色不用重训，换灯光只需颜色重标。
- 同一物体站立/倒伏的不同姿态标同一类；带污渍、文字、二维码而要分开装的，单独成类。
- 薄壁/空心件在 `classes.json` 标 `"thin_wall": true`。
- 坐标标定的完整步骤见 `vision/GUIDE.md` 第 3 节。
