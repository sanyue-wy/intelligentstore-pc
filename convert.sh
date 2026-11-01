#!/bin/bash
# ============================================================================
# 一键模型转换 (SSH 进 RDK X5 后在工程根目录执行)
#   1. 把训练好的 .pt 覆盖到 vision/models/ (例: vision/models/best.pt)
#   2. ./convert.sh
# 其它:
#   ./convert.sh --setup-remote 用户名@笔记本IP      一次性配置笔记本编译机 (Windows+WSL)
#   ./convert.sh --force                            同一个 .pt 重新转换
#   ./convert.sh 路径/xxx.pt --calib 图片目录
# 在宿主上执行时自动进入 intelligentstoring 容器 (BPU 验证需要容器里的运行库挂载)。
# ============================================================================
cd "$(dirname "$0")" || exit 1
CONTAINER=${CONTAINER:-intelligentstoring}
if [ ! -f /.dockerenv ] && command -v docker >/dev/null 2>&1 \
   && [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]; then
    TTY=""; [ -t 0 ] && TTY="-it"
    ARGS=$(printf '%q ' "$@")
    exec docker exec $TTY "$CONTAINER" bash -lc "cd /app && python3 tools/convert_model.py $ARGS"
fi
exec python3 tools/convert_model.py "$@"
