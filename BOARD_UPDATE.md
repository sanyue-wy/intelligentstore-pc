# 上板更新步骤（RDK X5 宿主）

> 原则：**不要 `docker build`**（板上构建已损坏），镜像不动、容器不动，只更新挂载进容器的工程源码。全程在宿主机的工程目录操作，命令会自动 `docker exec` 进 `intelligentstoring` 容器。

工程目录（宿主）：`/root/Desktop/1/pc_test_stm - 2/pc_test_stm`（容器内 = `/app`）。

## A. 更新前：备份「现场才有、不进 git」的文件

```bash
cd "/root/Desktop/1/pc_test_stm - 2/pc_test_stm"
BK=/root/is_field_backup_$(date +%F_%H%M%S).tgz
tar czf "$BK" \
  vision/calib_config.json \
  vision/classes.json \
  vision/models/ \
  tools/.web_token \
  records/ \
  deploy/.bpu_bridge.args 2>/dev/null
echo "备份: $BK"
```

> 若 `records/`、`.web_token`、`.bpu_bridge.args` 不存在会跳过（`2>/dev/null`），正常。

## B. 拉取新代码

```bash
cd "/root/Desktop/1/pc_test_stm - 2/pc_test_stm"
git status            # 确认干净；若有本地改动先 git stash
git fetch origin

# 情况1：分支已合并到 main
git checkout main && git pull --ff-only

# 情况2：尚未合并，直接用重构分支
git checkout refactor/phase3-cleanup
git log --oneline -5   # 应看到 eaf7168 阶段3 / 9a4fc9b 阶段2 ...
```

## C. 校验（不启动业务，只查语法与单测）

```bash
cd "/root/Desktop/1/pc_test_stm - 2/pc_test_stm"
bash -n is.sh start_competition.sh convert.sh deploy/*.sh && echo "shell OK"

docker exec intelligentstoring bash -lc \
  'cd /app && python3 -m unittest vision.test_vision MessegeCenter.test_bus \
     moniter.test_monitor communication.test_comm UI.test_ui webui.test_webui'
# 期望：Ran 65 tests ... OK
```

## D. 重启（按用途二选一）

**调试（全部网页，统一调试台 :8090）**
```bash
cd "/root/Desktop/1/pc_test_stm - 2/pc_test_stm"
./is.sh stop
./is.sh web            # 需要连下位机做「点哪去哪」时：./is.sh web --arm
# 浏览器打开 http://<板子IP>:8090 ，口令见启动日志「网页口令: xxxxxx」
# 或 cat "$P/tools/.web_token"（$P=工程根）
```

**正式比赛（执行页 :8088）**
```bash
./is.sh stop           # 先关调试台（比赛运行禁止遥控入口）
./start_competition.sh # 待机 → 按启动键开始；浏览器 http://<板子IP>:8088
```

## E. 端口与服务一览（未变更）

| 服务 | 端口 | 启动方式 |
|---|---|---|
| 统一调试台（视觉/流程/通讯/完整四节） | 8090 | `./is.sh web` |
| 比赛执行页 + `/screen` 全屏大屏 | 8088 | `./start_competition.sh` |
| 大屏推送目标（`x5_remote_server.py`，不在本仓库） | 8756 | 系统既有服务 |

## F. 需要保留的现场文件（切勿被 git 覆盖/删除）

- `vision/calib_config.json` —— 现场标定（四角/参考点/origin/镜头高）
- `vision/classes.json` —— 类别名（按模型自动同步）
- `vision/models/` —— `best_1027_640x480.bin` + `.json` + `best_1027.pt`（+ 可选 `.onnx`）
- `tools/.web_token` —— 网页口令（不存在会自动生成新口令）
- `records/` —— 复盘记录
- `deploy/.bpu_bridge.args` —— BPU 桥接参数

> 以上多数已在 `.gitignore`；`git checkout` 不会删除未跟踪文件，但 `git clean -fdx` 会，**不要轻易 clean**。

## G. 回滚到旧版本

```bash
cd "/root/Desktop/1/pc_test_stm - 2/pc_test_stm"
# 旧代码就是 main 的旧提交；若不慎已切到新分支：
git checkout main
# 如 main 也被推进过，回到重构前的旧提交（示例，用实际旧 HEAD）：
# git reset --hard bc0b461     # 初始提交，或重构前的最后一个 main 提交

# 恢复现场文件（若被改动）：
tar xzf /root/is_field_backup_XXXXXXXX.tgz -C "/root/Desktop/1/pc_test_stm - 2/pc_test_stm"

./is.sh stop && ./is.sh web     # 或 ./start_competition.sh 回到比赛
```

## H. 更新后立即自检

1. `./is.sh status` → 应能看到 `vision_tool`（调试台）或 `run_competition`（比赛）进程。
2. 浏览器打开 8090：能登录、四节导航可点、视觉页能出图。
3. 8088（比赛）：自检表正常、相机小窗有画面、`/screen` 全屏只读。
4. 按 `FIELD_VERIFICATION_CHECKLIST.md` 逐项打勾。
