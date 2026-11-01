#!/bin/bash
# ============================================================================
# is_debug.sh —— 调试工作台启停控制（一条命令，不需要记 systemd 语法）
#
#   is_debug.sh on [参数]     启动调试工作台（默认综合工作台）
#                             参数: --comm | --vision | --msg | --monitor
#   is_debug.sh off [参数]    停止调试工作台（不带参数=停所有实例）
#   is_debug.sh restart [参数]
#   is_debug.sh status        状态 + 窗口 + 访问地址 + 物理屏大屏状态
#   is_debug.sh log [参数]    实时日志（Ctrl-C 退出）
#   is_debug.sh open          打印笔记本访问地址
#   is_debug.sh takeover      非比赛场景：临时停大屏，把调试界面开到物理屏 :0
#   is_debug.sh giveback      关闭调试界面并恢复比赛大屏
#
# 与比赛大屏的关系（重要）
#   物理屏 :0 = 比赛大屏 x5_bigscreen.service（赛项规定装置屏只显示分拣成功页）。
#   调试工作台跑在虚拟屏 :99（1280x820），经 http://<X5-IP>:8756/ui 在浏览器里
#   查看和操作，**不占物理屏**，因此两者可以同时运行、互不抢屏。
#   若确需直接看物理屏调试（比赛之外）：is_debug.sh takeover / giveback。
#
# 实现说明
#   main.py 的参数形如 "--comm"，不能直接做 systemd 实例名（以 '-' 开头会被
#   当作路径解析），因此使用别名实例：workbench / comm / vision / msg / monitor，
#   对应 unit: intelligentstoring-debug@<别名>.service。
# ============================================================================
set -uo pipefail

CONTAINER=${CONTAINER:-intelligentstoring}
UNIT=intelligentstoring-debug
BIGSCREEN=x5_bigscreen.service
PORT=${PORT:-8756}
UI_DISPLAY=${UI_DISPLAY:-:99}

c_ok()  { printf '\033[32m%s\033[0m\n' "$*"; }
c_warn(){ printf '\033[33m%s\033[0m\n' "$*"; }
c_err() { printf '\033[31m%s\033[0m\n' "$*"; }

# 参数 -> 实例别名
alias_of() {
    case "${1:-}" in
        ""|default|workbench) echo workbench ;;
        --comm|comm)          echo comm ;;
        --vision|vision)      echo vision ;;
        --msg|msg)            echo msg ;;
        --monitor|monitor)    echo monitor ;;
        *) return 1 ;;
    esac
}
unit_of() { echo "$UNIT@$1.service"; }

running_units() {
    systemctl list-units --plain --no-legend "${UNIT}@*" 2>/dev/null \
        | awk '$4=="running"{print $1}'
}

start_debug() {
    local arg=${1:-}
    local alias; alias=$(alias_of "$arg") || {
        c_err "未知参数: $arg（可用: --comm --vision --msg --monitor，或留空）"; exit 2; }
    local unit; unit=$(unit_of "$alias")

    # 1) 容器必须在跑（依赖齐备由服务的 ExecStartPre 再校验一次）
    if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]; then
        echo "容器未运行，先拉起容器 ..."
        systemctl start intelligentstoring.service || {
            c_err "容器启动失败"; systemctl status intelligentstoring --no-pager -l | tail -20; exit 1; }
    fi
    # 2) :99 由 x5_remote.service 提供
    if ! DISPLAY=$UI_DISPLAY xdpyinfo >/dev/null 2>&1; then
        echo "虚拟屏 $UI_DISPLAY 未就绪，启动 x5_remote.service ..."
        systemctl start x5_remote.service
        for _ in $(seq 1 30); do
            DISPLAY=$UI_DISPLAY xdpyinfo >/dev/null 2>&1 && break; sleep 0.5
        done
    fi
    DISPLAY=$UI_DISPLAY xdpyinfo >/dev/null 2>&1 || { c_err "虚拟屏 $UI_DISPLAY 仍不可用"; exit 1; }

    # 3) 已有同类实例在跑就不重复启动（避免两个窗口叠在一起）
    if systemctl is-active --quiet "$unit"; then
        echo "调试工作台已在运行: $unit"
    else
        echo "启动 $unit ($arg) ..."
        systemctl start "$unit" || { c_err "启动失败"; systemctl status "$unit" --no-pager -l | tail -25; exit 1; }
    fi

    # 4) 等窗口出现（最多 30s）
    local n=0
    for _ in $(seq 1 60); do
        n=$(DISPLAY=$UI_DISPLAY xdotool search --onlyvisible --name '.' 2>/dev/null | wc -l)
        [ "$n" -gt 0 ] && break
        sleep 0.5
    done
    if [ "$n" -gt 0 ]; then
        c_ok "就绪 ($n 个可见窗口)"
    else
        c_warn "30s 内未见窗口，最近日志:"
        journalctl -u "$unit" -n 25 --no-pager
        exit 1
    fi
    echo "笔记本浏览器访问:  http://$(hostname -I | awk '{print $1}'):$PORT/ui"
}

stop_debug() {
    local arg=${1:-} scope=()
    if [ -n "$arg" ]; then
        local alias; alias=$(alias_of "$arg") || { c_err "未知参数: $arg"; exit 2; }
        scope=("$(unit_of "$alias")")
    else
        mapfile -t scope < <(running_units)
    fi
    local stopped=0
    for u in "${scope[@]}"; do
        if systemctl is-active --quiet "$u" 2>/dev/null; then
            systemctl stop "$u" && { echo "已停止 $u"; stopped=1; }
        fi
    done
    # 兜底清理容器内残留（服务停止时 ExecStopPost 已处理，双保险）
    docker exec "$CONTAINER" pkill -f '[m]ain\.py' 2>/dev/null && echo "已清理容器内残留进程"
    [ "$stopped" = 0 ] && echo "调试工作台未在运行"
    return 0
}

status_debug() {
    echo "== 服务 =="
    local list; list=$(systemctl list-units --all --plain --no-legend "${UNIT}@*" 2>/dev/null)
    if [ -z "$list" ]; then echo "  (未加载任何调试实例)"; else echo "$list" | sed 's/^/  /'; fi
    echo "== 容器 =="
    docker ps -a --filter name="$CONTAINER" --format '  {{.Names}}  {{.Status}}' 2>/dev/null || echo "  (无)"
    docker exec "$CONTAINER" pgrep -af '[m]ain\.py' 2>/dev/null | sed 's/^/  /' || echo "  程序未运行"
    echo "== 虚拟屏 $UI_DISPLAY 可见窗口 =="
    local w; w=$(DISPLAY=$UI_DISPLAY xdotool search --onlyvisible --name '.' 2>/dev/null)
    if [ -z "$w" ]; then echo "  (无窗口)"; else
        for i in $w; do printf '  %s :: %s\n' "$i" "$(DISPLAY=$UI_DISPLAY xdotool getwindowname "$i" 2>/dev/null)"; done
    fi
    echo "== 访问地址 =="
    echo "  http://$(hostname -I | awk '{print $1}'):$PORT/ui"
    echo "== 物理屏 :0 =="
    if systemctl is-active --quiet "$BIGSCREEN"; then
        echo "  比赛大屏 $BIGSCREEN 运行中（调试界面不会出现在这块屏上）"
    else
        c_warn "  比赛大屏未运行！比赛前请执行: systemctl start $BIGSCREEN"
    fi
}

case "${1:-status}" in
    on|start)   shift; start_debug "${1:-}" ;;
    off|stop)   shift; stop_debug "${1:-}" ;;
    restart)    shift; stop_debug "${1:-}"; sleep 1; start_debug "${1:-}" ;;
    status)     status_debug ;;
    log)
        shift
        if [ -n "${1:-}" ]; then
            alias=$(alias_of "$1"); exec journalctl -u "$(unit_of "$alias")" -f
        else
            exec journalctl -u "${UNIT}@*" -f
        fi ;;
    open)       echo "http://$(hostname -I | awk '{print $1}'):$PORT/ui" ;;
    takeover)
        # 非比赛场景：让物理屏直接显示调试界面（会临时停掉比赛大屏）
        echo "停止比赛大屏，把调试界面开到物理屏 :0 ..."
        systemctl stop "$BIGSCREEN"
        echo "（注意: 调试界面仍按设计开在 $UI_DISPLAY，浏览器查看；"
        echo "  大屏停掉后物理屏会露出桌面。比赛前务必 is_debug.sh giveback）"
        shift; start_debug "${1:-}" ;;
    giveback)
        echo "关闭调试界面并恢复比赛大屏 ..."
        stop_debug
        systemctl start "$BIGSCREEN" && echo "比赛大屏已恢复"
        ;;
    *)
        sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
        exit 2 ;;
esac
