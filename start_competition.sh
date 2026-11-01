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
#   2. 守护运行 UI/run_competition.py: 执行页「一键重启」(退出码 75) 或程序崩溃时自动拉起,
#      回到待机; 正常退出 (Ctrl+C) 不再拉起
#   3. 执行页: http://<X5_IP>:8088
# ============================================================================
cd "$(dirname "$0")" || exit 1
CONTAINER=${CONTAINER:-intelligentstoring}

if [ ! -f /.dockerenv ] && command -v docker >/dev/null 2>&1 \
   && [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]; then
    # 宿主: 停调试工作台, 然后进容器跑本脚本
    if command -v systemctl >/dev/null 2>&1; then
        systemctl stop 'intelligentstoring-debug@*' >/dev/null 2>&1 || true
    fi
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

# 2) 守护运行
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
