#!/bin/bash
# ============================================================================
# is_boot_guard.sh —— 开机就绪等待 + 分拣工作环境准备（幂等）
#
# 由 intelligentstoring.service 的 ExecStartPre 调用，目的：
#   1. 等 Docker daemon 真正可用（After=docker.service 不够，socket 就绪有延迟）
#   2. 等物理屏 X 会话 :0 真正可用（lightdm 起来后 Xorg 才是 ready 状态）
#   3. 通过 sunrise 用户的 Xauthority 放行本机连 :0 的客户端
#      —— 注意：不要用 `xhost +local:`，它会让 **容器内 root** 也能直连 :0
#         并投出调试窗口，与比赛大屏抢屏。这里只加 "root@<hostname>" 精确条目。
#   4. 物理屏 framebuffer 锁死原生 800x480 + 禁用 DPMS/屏保（沿用既有脚本）
#
# 退出码 0/1：两个就绪条件都满足时返回 0；超时返回 1（服务会被判为启动失败，
#             Restart=on-failure 会按 RestartSec 拉起重试）。
#
# 可单独运行做诊断: bash is_boot_guard.sh
# ============================================================================
set -uo pipefail

PHYS_DISPLAY=${PHYS_DISPLAY:-:0}
XUSER=${XUSER:-sunrise}
XUSER_HOME=${XUSER_HOME:-/home/$XUSER}
WAIT_DOCKER=${WAIT_DOCKER:-120}   # 秒
WAIT_X=${WAIT_X:-180}             # 秒

log() { echo "[boot-guard] $*"; }

# ------------------------------------------------------- 1. 等 Docker ----
if ! command -v docker >/dev/null 2>&1; then
    log "错误: 找不到 docker 命令"
    exit 1
fi
deadline=$(( $(date +%s) + WAIT_DOCKER ))
until docker info >/dev/null 2>&1; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        log "错误: docker daemon ${WAIT_DOCKER}s 内未就绪"
        exit 1
    fi
    sleep 2
done
log "docker daemon 就绪"

# --------------------------------------------------- 2. 等物理屏 :0  ----
# 以 sunrise 身份探测：既验证 X server 起来了，也验证该用户的 Xauthority 可用。
as_xuser() {
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "$XUSER" -- env DISPLAY="$PHYS_DISPLAY" HOME="$XUSER_HOME" "$@"
    else
        su -s /bin/sh "$XUSER" -c "$(printf '%q ' "$@")"
    fi
}

deadline=$(( $(date +%s) + WAIT_X ))
delay=1
until as_xuser xdpyinfo >/dev/null 2>&1; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        log "错误: 物理屏 $PHYS_DISPLAY 在 ${WAIT_X}s 内不可用（lightdm/Xorg 未起？）"
        exit 1
    fi
    sleep "$delay"
    # 渐进退避，最多 5s（避免开机阶段刷日志）
    delay=$(( delay < 5 ? delay + 1 : 5 ))
done
log "物理屏 $PHYS_DISPLAY 就绪"

# ------------------------------------- 3. 精确放行 root 连接 :0  --------
# 只放行 "root@<hostname>"（也就是容器里的 root 看到的身份），
# 不用 "xhost +local:" —— 那会把 :0 对容器内所有进程完全敞开，
# 调试界面执行时就能把窗口投到物理屏上，与比赛大屏抢屏。
# 注意：容器用 --network host，容器内 hostname 与宿主相同，所以身份串就是 root@$(hostname)。
HOSTN=$(hostname)
if as_xuser xdpyinfo >/dev/null 2>&1 && DISPLAY=$PHYS_DISPLAY xdpyinfo >/dev/null 2>&1; then
    log "root@$HOSTN 已可连接 $PHYS_DISPLAY（无需 xhost 变更）"
else
    as_xuser xhost + "root@$HOSTN" >/dev/null 2>&1 \
        && log "已放行 root@$HOSTN 连接 $PHYS_DISPLAY" \
        || log "警告: xhost 放行失败（调试界面不需要 :0，可忽略）"
fi

# --------------------------- 4. 物理屏分辨率 / DPMS 设置（幂等） -------
if [ -x /root/rkx5_remote/x5_display_setup.sh ]; then
    as_xuser bash /root/rkx5_remote/x5_display_setup.sh "$PHYS_DISPLAY" 800x480 2>&1 \
        | sed 's/^/[boot-guard][display] /'
else
    log "跳过: /root/rkx5_remote/x5_display_setup.sh 不存在"
fi

log "完成"
exit 0
