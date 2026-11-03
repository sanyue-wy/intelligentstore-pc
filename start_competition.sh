#!/bin/bash
# ============================================================================
# 比赛一键启动 (RDK X5)。宿主上执行时自动进入 intelligentstoring 容器运行
# (容器 = ubuntu:22.04 + 宿主 BPU 运行库挂载, 见 deploy/provision_intelligentstoring.sh)。
#
#   ./start_competition.sh                      # 启动 -> 自检 -> 待机, 按启动键开始
#   EXPECTED_TOTAL=12 ./start_competition.sh    # 已知货物数时达到即结束
#   START_GPIO_PIN=37 ./start_competition.sh    # 用 40pin GPIO 按键启动
#
# 做了什么:
#   1. 关闭网页调试台 / 调试工作台 (它们占用相机和串口, 且比赛中不能有遥控入口)
#   2. 视觉门禁 (A3): 比赛模式 (存在锁文件) 下门禁不过就打印原因并退出非零, 不进守护循环
#   3. 守护运行 UI/run_competition.py: 执行页「一键重启」(退出码 75) 或程序崩溃时自动拉起,
#      回到待机; 正常退出 (Ctrl+C) 不再拉起
#   4. 执行页: http://<X5_IP>:8088
# ============================================================================
cd "$(dirname "$0")" || exit 1
CONTAINER=${CONTAINER:-intelligentstoring}

if [ ! -f /.dockerenv ] && command -v docker >/dev/null 2>&1 \
   && [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]; then
    # 宿主: 停调试工作台, 然后进容器跑本脚本
    # 网页调试台由容器内的 pkill 关闭 (见下方「比赛模式」段)
    TTY=""; [ -t 0 ] && TTY="-it"
    ENVS=()
    for v in $(env | grep -E '^(EXPECTED_TOTAL|START_|STM_PORT|ALLOW_MOCK|USE_SYNTHETIC_CAM|SCAN_FRAMES|IS_|COMMIT_CONF|PREFETCH_STAGE|IDLE_GUARD_S|ONESHOT|SORT_TIMEOUT_S|MAX_)' | cut -d= -f1); do
        ENVS+=("-e" "$v=${!v}")
    done
    exec docker exec $TTY "${ENVS[@]}" "$CONTAINER" bash -lc "cd /app && ./start_competition.sh"
fi

export PYTHONUNBUFFERED=1
export IS_VISION_BACKEND=${IS_VISION_BACKEND:-yolo}
export DISPLAY=${DISPLAY:-:0}
LOG=${COMPETITION_LOG:-/var/log/intelligentstoring/competition.log}
mkdir -p "$(dirname "$LOG")" 2>/dev/null || LOG=/tmp/competition.log

# 1) 比赛模式: 关闭网页调试台和调试工作台
pkill -f "[v]ision_tool.py web" 2>/dev/null && echo "[比赛模式] 已关闭网页调试台"
pkill -f "[m]ain\.py" 2>/dev/null && echo "[比赛模式] 已关闭调试工作台"
sleep 0.5

python3 -c "from vision.yolo_engine import is_bpu_available as f; print('BPU 运行库:', '可用' if f() else '不可用 (将退回 CPU, 宿主执行 deploy/bpu_doctor.sh 排查)')"
echo "执行页: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${IS_RUN_PAGE_PORT:-8088}"

# 2) 视觉门禁 (A3): 比赛模式 (存在锁文件) 下门禁不过 -> 打印原因并退出非零, 不进守护循环。
#    为什么不丢进 while 里重试: 门禁不过 = 标定/成像/限位没配好, 2 秒一次无限重启只会刷屏,
#    还会把「现场真正要做的动作」(参考点盖章 / 锁曝光增益白平衡 / 填限位) 淹没掉。
#    修好后重新执行本脚本即可; 门禁自身异常时按锁文件判据放行, 不因门禁故障挡住比赛。
LOCK=${IS_COMPETITION_LOCK:-/tmp/is_competition.lock}
python3 -m vision.gates --check
gate_code=$?
if [ "$gate_code" != 0 ]; then
    if [ -f "$LOCK" ]; then
        echo "[比赛模式] 视觉门禁不通过 (退出码 $gate_code), 上面列出的 FAIL 项必须先处理 -> 不启动"
        echo "           现场: ./is.sh gates        看门禁 11 项"
        echo "                 ./is.sh gates --seal 给当前参考点盖章"
        exit "$gate_code"
    fi
    echo "[警告] 视觉门禁检查异常 (退出码 $gate_code; 未发现锁文件 $LOCK, 按调试模式放行)"
fi

# 3) 守护运行
while true; do
    python3 UI/run_competition.py "$@" 2>&1 | tee -a "$LOG"
    code=${PIPESTATUS[0]}
    if [ "$code" = 0 ] || [ "$code" = 130 ]; then
        echo "[比赛模式] 程序正常退出"
        break
    fi
    if [ "$code" = 75 ]; then
        echo "[比赛模式] 一键重启 ..."
    else
        echo "[比赛模式] 程序异常退出 (code=$code), 2 秒后自动重启回到待机"
        sleep 2
    fi
done
