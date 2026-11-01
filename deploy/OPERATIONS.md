# STM32 智能分拣上位机 — 运维手册（RDK X5）

> 适用对象：赛场操作员 / 值班同学。所有命令都需要 `sudo`（本机以 root 登录则直接执行）。

---

## 0. 一页速查

| 我要做什么 | 命令 |
| --- | --- |
| 看整体健康状态 | `is_health.sh` |
| 比赛模式（只开物理屏大屏） | `systemctl start x5_bigscreen` |
| 开调试工作台 | `is_debug.sh on` |
| 开指定调试器 | `is_debug.sh on --comm`（`--vision` / `--msg` / `--monitor`） |
| 关调试工作台 | `is_debug.sh off` |
| 看调试界面 | 浏览器打开 `http://<X5-IP>:8756/ui` |
| 容器丢了 / 依赖没了 | `provision_intelligentstoring.sh` |
| 看日志 | `is_debug.sh log` 或 `journalctl -u intelligentstoring -f` |
| 全屏黑屏 / 自动黑屏 | `is_boot_guard.sh`（重新锁 framebuffer + 关 DPMS） |
| **比赛程序（待机→启动键）** | `./start_competition.sh`；开机自启 `./is.sh race on` / 关 `./is.sh race off` |
| **执行页** | 浏览器打开 `http://<X5-IP>:8088` |
| **赛前自检** | `./is.sh precheck` |
| **换模型** | `.pt` 放进 `vision/models/` 后 `./convert.sh` |
| **BPU / 容器诊断** | `sudo deploy/bpu_doctor.sh`（输出整段发给维护者） |
| **迁移到 BPU 容器** | `sudo ./is.sh migrate`（见第 0.5 节） |

---

## 0.5 容器方案（2026-10 起）：ubuntu:22.04 + 宿主 BPU 运行库

| | 旧：`ultralytics/ultralytics:latest-arm64` | 新：`ubuntu:22.04` |
|---|---|---|
| Python | 3.11+（与宿主不同） | **3.10，与 RDK X5 宿主一致** |
| BPU | 加载不了宿主为 3.10 编译的 `hobot_dnn`/`hbm_runtime` → 只能 CPU，每帧几百 ms | 只读挂载宿主运行库，**BPU 每帧十几 ms** |
| 依赖来源 | 镜像自带 torch | provision 脚本在容器里 apt/pip 安装（幂等） |
| 需要 docker build | — | **不需要**（本机 BuildKit 已坏、legacy builder 会 OOM） |

**迁移（一次，约 10~20 分钟，需联网）**
```bash
cd <工程目录>
sudo deploy/bpu_doctor.sh                 # 先看宿主 BPU 运行库情况 (可选)
sudo ./is.sh migrate                      # = bpu_bridge.sh 生成挂载参数 + provision --recreate
```
- 旧容器不删除，改名为 `intelligentstoring_old_<时间>` 并停用；确认新容器正常后 `docker rm` 它。
  回退：`docker rm -f intelligentstoring && docker rename intelligentstoring_old_<时间> intelligentstoring && docker start intelligentstoring`
- `deploy/bpu_bridge.sh` 在宿主上找 `hobot_dnn` / `hbm_runtime` / `Hobot.GPIO` 及其依赖库，生成 `deploy/.bpu_bridge.args`；
  容器内挂到 `/opt/hostbpu/{py,lib}`，并整体只读挂载 `/usr/hobot`。宿主系统升级后重新执行 `./is.sh migrate`。
- provision 会安装：Python3.10、tkinter、opencv 4.11 + numpy 1.26（numpy 必须 <2，宿主 BPU 库按 1.x 编译）、onnxruntime、
  CPU 版 torch + ultralytics + onnx（板上一键转换导出 ONNX 用，`--no-export` 可跳过）、openssh-client（连笔记本编译）。
- 板子不能联网时：在笔记本 WSL 里 `docker buildx build --platform linux/arm64 -t is-x5:22.04 .`（用工程根目录 `Dockerfile`），
  `docker save is-x5:22.04 | gzip > is.tgz`，拷到板子 `docker load < is.tgz`，再 `IMAGE=is-x5:22.04 ./is.sh migrate`。

## 0.6 比赛模式

- `./start_competition.sh`：关闭网页调试台和调试工作台 → 守护运行比赛程序 → 自检、预热后待机。
- 启动方式：装置上的 USB 单键按钮（插上即用）或 GPIO 按键（`START_GPIO_PIN=37`），执行页「开始」也可以。按下后双手离开。
- 执行页 `:8088`：状态/流程/错误/自检/相机/大屏预览 + 开始 / 停止本轮 / 急停 / 一键重启。
- `./is.sh race on`：安装 `deploy/intelligentstoring-competition.service` 后开机自动进入待机（与调试工作台互斥）。

---

## 1. 五个 systemd 单元，各管什么

| 单元 | 开机自启 | 作用 | 出问题时的现象 |
| --- | --- | --- | --- |
| `x5_bigscreen.service` | ✅ enabled | **比赛大屏**：物理屏 `:0` 上显示「智能分拣 · 分拣信息」成功页 | 物理屏黑/无窗口 |
| `x5_remote.service` | ✅ enabled | 远程服务 `:8756`，同时负责启动虚拟屏 `Xvfb :99`（1280x820） | 浏览器打不开 / 调试器起不来 |
| `intelligentstoring.service` | ✅ enabled | **容器保活 + 依赖自愈**：等 docker 和 `:0` 就绪，容器没了就重建，依赖缺了就装 | 容器不存在 / `import cv2` 失败 |
| `intelligentstoring-debug@.service` | ❌ **不自启（设计如此）** | **调试工作台**：手动启动、崩溃 5 秒自动重拉 | — |
| `logrotate.timer` | ✅ enabled | 每天轮转 `/var/log/intelligentstoring/*.log`（单个超 5 MB 也轮转） | 磁盘被日志写满 |

**关键设计：物理屏只显示比赛大屏。** 调试界面一律跑在虚拟屏 `:99`，通过浏览器看，所以
「比赛大屏还在跑」和「我在调试」可以同时成立，不需要停大屏。

---

## 2. 比赛时怎么用

```bash
# 1) 确认状态（10 项全绿就放心上场）
is_health.sh

# 2) 如果物理屏没显示大屏
systemctl start x5_bigscreen
is_health.sh          # 等到「比赛大屏 ✓ … 物理屏窗口 id=…」

# 3) 确保调试工作台是关的（比赛时不该有调试窗口占 CPU）
is_debug.sh off
```

比赛期间**不要**执行 `is_debug.sh on`（会额外吃 CPU/内存）；确实要临时排查，
用完立刻 `is_debug.sh off`。

---

## 3. 调试时怎么用

```bash
is_debug.sh on                 # 综合工作台（默认）
is_debug.sh on --comm          # 通讯调试器
is_debug.sh on --vision        # 视觉调参 -> 实际转到浏览器调试台 :8090 (见第 10 节)
is_debug.sh on --msg           # 消息中心
is_debug.sh on --monitor       # 监控看板

is_debug.sh status             # 看哪个实例在跑、:99 上有哪些窗口、访问地址
is_debug.sh log                # 跟随日志（Ctrl-C 退出）
is_debug.sh off                # 全部关闭
```

然后在**笔记本浏览器**打开 `http://<X5-IP>:8756/ui`（`is_debug.sh open` 会直接打印地址）。

**崩溃自愈**：程序异常退出（段错误、`kill -9`、Tk 报错）后，systemd 会在 5 秒内自动
重新拉起，不需要人工干预。连续崩溃时会指数退避（最长约 60 秒一次），不会把 CPU 打满。

> 想验证自愈：
> ```bash
> is_debug.sh on
> docker exec intelligentstoring pkill -9 -f '[m]ain\.py'
> sleep 12
> systemctl show -p NRestarts -p ActiveState intelligentstoring-debug@workbench.service
> ```
> `NRestarts` 会 +1，`ActiveState` 回到 `active`。

---

## 4. 容器没了怎么恢复（一条命令）

```bash
provision_intelligentstoring.sh
```

这条命令是**幂等**的，跑多少遍结果都一样：

* 容器不存在 → 按标准参数（`--privileged --network host` + 4 个关键挂载）重建
* 容器存在但停了 → `docker start`
* 容器在跑 → 只做检查，什么都不动
* 依赖（tkinter / pyserial / opencv / pillow / psutil / 中文字体 / X 工具）缺哪个补哪个
  * apt 源和 pypi 清华镜像都需要**联网**
* 最后自检一遍，全过才返回 0

只检查不改动：

```bash
provision_intelligentstoring.sh --check-only   # 全过 exit 0，缺东西 exit 1
```

网络不好时可以强制重装 apt 依赖：`provision_intelligentstoring.sh --force-apt`

**重建会耗时 1~3 分钟**（apt + pip 下载）。比赛前请提前跑一次，
不要等到上场前才第一次恢复。

---

## 5. 日志在哪、怎么轮转

| 位置 | 内容 |
| --- | --- |
| `journalctl -u intelligentstoring` | 容器保活/依赖补齐的每一步 |
| `journalctl -u 'intelligentstoring-debug@*'` | 调试工作台的标准输出与退出码 |
| `/var/log/intelligentstoring/` | 程序自己的日志（**已挂载进容器**，容器删了日志还在） |

轮转策略 `/etc/logrotate.d/intelligentstoring`：

```
size 5M      # 单个文件超 5 MB 就轮转
rotate 2     # 最多留 2 份历史
compress     # gzip 压缩（实测 7.1 MB → 583 KB）
```

由 `logrotate.timer` 每天驱动一次；单个日志涨得快时也能在当天被下一轮轮转到。
**总量上限约 11 MB**，不会把这块已损坏的 SD 卡写满。

手动强制执行一次（排查时用）：

```bash
logrotate -vf /etc/logrotate.d/intelligentstoring
```

> ⚠️ 本机 `/dev/root` 的 EXT4 文件系统已有损坏（`FS Error count` 持续增长，
> 属 SD 卡介质问题）。本手册的所有日志策略都是按「必须控量」设计的；
> 但**介质本身需要更换 SD 卡才能根治，这不是软件能修的**。

---

## 6. 屏幕：800x480 物理屏 + 1280x820 虚拟屏

| 显示 | 分辨率 | 谁在用 |
| --- | --- | --- |
| `:0`（HDMI-1） | 800x480 原生 | 比赛大屏 `x5_bigscreen` |
| `:99`（Xvfb） | 1280x820 | 调试工作台（浏览器经 `:8756/ui` 查看） |

虚拟分辨率 1280x820 的持久化由 `x5_remote.service` 负责（它启动 Xvfb 时直接带这个尺寸），
不依赖临时的 `xrandr --fb` 命令，**重启后不会丢**。

`is_boot_guard.sh` 每次开机还会：把 `:0` 的 framebuffer 锁回原生 800x480、
关闭屏保、禁用 DPMS（防止自动黑屏）。手动补跑：

```bash
is_boot_guard.sh
```

---

## 7. 出事怎么查（故障速查）

### 7.1 物理屏没显示比赛大屏
```bash
systemctl status x5_bigscreen --no-pager -l
is_boot_guard.sh                  # 重新锁屏 + 关 DPMS
systemctl restart x5_bigscreen
is_health.sh                      # 确认「物理屏窗口 id=…」出现
```

### 7.2 调试工作台起不来
```bash
is_debug.sh status
systemctl status 'intelligentstoring-debug@*' --no-pager -l
journalctl -u 'intelligentstoring-debug@*' -n 50 --no-pager
# 常见原因：容器没跑（is_health.sh 看）、:99 没起来（systemctl start x5_remote）
```

### 7.3 容器里 `import cv2` / `import serial` 报错
```bash
provision_intelligentstoring.sh      # 自动补装
```

### 7.4 浏览器打不开 8756
```bash
systemctl status x5_remote --no-pager
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8756/healthz   # 期望 200
```

### 7.5 磁盘快满
```bash
is_health.sh                          # 看「磁盘余量」
du -sh /var/log/intelligentstoring/*  # 日志占用
logrotate -vf /etc/logrotate.d/intelligentstoring
journalctl --vacuum-size=200M         # 收缩 journal（本来就占 1 GB）
```

---

## 8. 已知问题（**未修复，需用户决策**）

### 8.1 `x5_remote.service` 空载时仍持续占用约 6.5% 单核 CPU

| 项目 | 内容 |
| --- | --- |
| **现象** | 无任何浏览器客户端连接时，`x5_remote.service` 仍持续消耗约 **6.5% 单核**（实测 10s 内 65 ticks；单元累计 CPU 已达 7min28s）。RDK X5 为四核，相当于常驻 ~1.6% 总 CPU。 |
| **根因** | `/root/rkx5_remote/x5_remote_server.py` 第 **862–870** 行 `screen_render_loop()`：`self._dirty.wait(timeout=1.0)` 每秒超时后**无条件**执行 `render_bigscreen(...)` + `broadcast_screen_frame(...)`。`broadcast_screen_frame` 内部虽判断 `if not jpeg: return`，但 jpeg 是刚渲染出来的、必然非空，因此**无人观看时照样每秒重绘一次 800x480 大屏**。（对比：`/ui` 的编码器有「无客户端即暂停」保护，大屏渲染循环没有。） |
| **影响** | 长期运行持续发热、增加 SD 卡磨损放大。本机 EXT4 已因 SD 卡介质问题损坏（`FS Error count` 持续增长），因此这条值得留意。 |
| **建议修法** | 在 `broadcast_screen_frame` 前加「无客户端且状态未变则跳过重绘」，或仅在 `_dirty` 被置位时才渲染（保留 1s 兜底但跳过无变化的重绘）。改动量约 1–3 行。 |
| **为什么没修** | **超出本次任务范围（容器/服务/开机自启），且属于 `x5_remote` 另一套交付物——它是裁判直接观看的比赛大屏界面，为省 6% CPU 去改动它风险收益不划算。已报队长确认，决定「不动，作为已知问题记录，由用户决定是否另派任务优化」。** |

> 本条**不是已修复项**。若后续决定优化，请单独派单，并在改完后重做一次大屏截图验证。

---

## 9. 已清理：`sorting_ui.service`（**2026-10-25 已执行完成**）

`sorting_ui.service` 原本是 `/etc/init.d/sorting_ui` 被 `systemd-sysv-generator`
临时生成的单元，**每次开机必然失败**，而且失败被掩盖成 `active (exited)`：

* 生成单元里 `Before=graphical.target` → 比图形界面还早启动 → `could not connect to display :0`
* `Type=forking` + 脚本末尾 `exit 0` → python 后台进程死掉后 systemd 仍报成功
* 它跑的 `/home/root/sorting_ui.py` 是早期 PyQt5 原型（视觉线程全是 `time.sleep` + 假数据），
  功能已被 `x5_bigscreen.service` 完全取代

### 已执行的操作（队长批准，2026-10-25 15:57–15:59）

```bash
# 1) 备份原 init 脚本
mkdir -p /root/backup/sorting_ui_removed_20261025
cp -a /etc/init.d/sorting_ui /root/backup/sorting_ui_removed_20261025/

# 2) 去掉 rc 启动链接（S01sorting_ui 在 rc2-5 中全部消失）
update-rc.d -f sorting_ui remove
systemctl disable sorting_ui.service
systemctl daemon-reload

# 3) 把 init 脚本改名移走（彻底阻止 sysv-generator 再生成单元）
mv /etc/init.d/sorting_ui /root/backup/sorting_ui_removed_20261025/sorting_ui.disabled

# 4) 删除全部残留 rc 链接（rc0-6.d 里的 K01sorting_ui）
for d in /etc/rc0.d /etc/rc1.d /etc/rc2.d /etc/rc3.d /etc/rc4.d /etc/rc5.d /etc/rc6.d; do
    rm -f "$d"/*sorting_ui* 2>/dev/null
done

# 5) 重新生成单元并清 failed
systemctl daemon-reload
systemctl reset-failed sorting_ui.service 2>/dev/null
```

### 验证结果

```
$ systemctl status sorting_ui.service
Unit sorting_ui.service could not be found.        # 已彻底移除

$ ps aux | grep sorting_ui.py
(无)                                               # 无残留进程

$ DISPLAY=:0 xwininfo -root -children | grep Tk
0x3600015 "智能分拣 · 分拣信息": ("tk" "Tk")  800x480+0+0
                                                   # :0 只剩比赛大屏
```

### ⚠️ 执行时踩到的坑（重要，供后来者参考）

`/etc/init.d/sorting_ui` 这个脚本**没有实现 stop 分支**，内容是无条件的：

```bash
sleep 5
export DISPLAY=:0
python3 /home/root/sorting_ui.py &
exit 0
```

因此 `systemctl stop sorting_ui` 不但不会停止任何东西，反而**会再拉起一个
PyQt5 原型窗口**（实测在 `:0` 上出现 `"智能分拣系统": ("sorting_ui.py" ...)`，
246x150 的小窗口，污染比赛屏）。

**正确做法：先 `mv` 走 init 脚本、删干净 rc 链接，再 `daemon-reload`**，
而不是先 `systemctl stop`。若已误触发了残留进程，用 `pkill -f "[s]orting_ui.py"` 清理。

**回滚方法**（如需恢复）：

```bash
cp /root/backup/sorting_ui_removed_20261025/sorting_ui /etc/init.d/sorting_ui
chmod +x /etc/init.d/sorting_ui
update-rc.d sorting_ui defaults
systemctl daemon-reload
```

---

> 另注：`auto_update_miniboot.service` 当前也是 failed，那是**板子固件层**的问题，
> 与本项目无关，**不要去动**。

---

## 10. 视觉调参（全部在浏览器）与换模型

所有需要实时画面的调参都在浏览器调试台完成，板子不需要显示器：

```bash
./is.sh web            # = docker exec ... python3 tools/vision_tool.py web ; 加 --arm 连接下位机
# 笔记本浏览器打开 http://<X5-IP>:8090
```

| 页 | 做什么 |
|---|---|
| 状态 | 帧率、模型、标定/托盘状态、自检问题；点击画面看机械臂坐标；点哪去哪（--arm） |
| 托盘 | 填内框 W×H、外沿、壁高、镜头高 → 自动找白色托盘；参考点；机械臂轴向 |
| 畸变 | 棋盘格逐张拍 → 计算内参；或托盘直边估计；去畸变画面对比 |
| 标定/ROI | 点击画面 + 机械臂坐标（托盘模式下作为参考点）；手动 ROI |
| 相机 / 颜色 | 曝光增益白平衡；点选货物生成 HSV 区间 |
| 大屏 / 复盘 | 大屏预览；每轮报告与识别画面、归档回归集 |

比赛程序运行时调试台拒绝启动（避免遥控嫌疑和抢相机）。
换模型：把 `.pt` 放进 `vision/models/` 后 `./convert.sh`（详见 `tools/GUIDE.md`）。
每次 scan 原图：`records/scans/raw`；每轮比赛报告：`records/runs/`。

---

## 11. 完整文件清单（便于迁移/复原）

```
/usr/local/sbin/provision_intelligentstoring.sh   幂等容器创建 + 依赖补齐
/usr/local/sbin/is_boot_guard.sh                  开机就绪等待 + 屏幕锁定
/usr/local/sbin/is_debug.sh                       调试工作台启停
/usr/local/sbin/is_health.sh                      10 项健康检查（--json / --quiet）

/etc/systemd/system/intelligentstoring.service
/etc/systemd/system/intelligentstoring-debug@.service
/etc/logrotate.d/intelligentstoring

/var/log/intelligentstoring/                      日志目录（挂载进容器）
/usr/local/share/doc/intelligentstoring/OPERATIONS.md   本文件
```

工程目录里不安装、但会用到的文件：

| 文件 | 用途 |
| --- | --- |
| `Dockerfile` | 断网/批量部署时在 x86 笔记本 WSL 里构建镜像再 `docker load`；平时用 `./is.sh migrate` 运行时安装 |
| `is.sh` | 一个入口管所有常用操作（`./is.sh` 看帮助） |
| `deploy/*.sh / *.service` | 本节上面的安装版的源文件，重装系统后 `install` 回去再 `systemctl daemon-reload` |
