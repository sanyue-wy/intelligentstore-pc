#!/bin/bash
# ============================================================================
# is_health.sh —— 运行时健康检查（只读，不改动任何状态）
#
# 检查项（任一失败退出码 1，全部通过退出码 0）：
#   1. 容器 intelligentstoring 存在且在运行
#   2. 容器内依赖齐备 (tkinter / pyserial / opencv / PIL / psutil / 字体 / xdpyinfo)
#   3. 比赛大屏服务 x5_bigscreen 为 active，且物理屏上确有「分拣信息」窗口
#   4. 物理屏 :0 可用，framebuffer = 800x480，DPMS 已禁用（不会自动黑屏）
#   5. 远程服务 8756 /healthz 有响应（调试界面镜像通道）
#   6. 若调试工作台在运行：:99 可用且确有可见窗口
#   7. 磁盘/内存余量（磁盘已损坏，低于阈值要提前告警）
#   8. 最近 10 分钟内服务是否有意外重启（NRestarts 增长）
#
# 用法:
#   is_health.sh            # 人类可读
#   is_health.sh --json     # 机器可读（便于定时任务/看板）
#   is_health.sh --quiet    # 只输出失败项
# ============================================================================
set -uo pipefail

CONTAINER=${CONTAINER:-intelligentstoring}
PROVISION=/usr/local/sbin/provision_intelligentstoring.sh
PORT=${PORT:-8756}
LOG_DIR=${LOG_DIR:-/var/log/intelligentstoring}

MODE=${1:-}
FAILS=0
declare -a RESULT=()

add() {  # add <status> <name> <detail>
    local st=$1 name=$2 detail=$3
    [ "$st" = "fail" ] && FAILS=$((FAILS + 1))
    RESULT+=("$st|$name|$detail")
    if [ "$MODE" != "--json" ]; then
        if [ "$st" = "ok" ]; then
            [ "$MODE" = "--quiet" ] || printf '  \033[32m✓\033[0m %-28s %s\n' "$name" "$detail"
        elif [ "$st" = "warn" ]; then
            printf '  \033[33m!\033[0m %-28s %s\n' "$name" "$detail"
        else
            printf '  \033[31m✗\033[0m %-28s %s\n' "$name" "$detail"
        fi
    fi
}

[ "$MODE" != "--json" ] && [ "$MODE" != "--quiet" ] && echo "===== intelligentstoring 健康检查 ($(date '+%F %T')) ====="

# ---- 1. 容器 ----
if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]; then
    up=$(docker inspect -f '{{.State.StartedAt}}' "$CONTAINER" 2>/dev/null)
    add ok "容器运行中" "启动于 $up"
    HCONTAINER=1
else
    ex=$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null)
    add fail "容器未运行" "状态=${ex:-不存在}"
    HCONTAINER=0
fi

# ---- 2. 依赖 ----
if [ "$HCONTAINER" = 1 ] && [ -x "$PROVISION" ]; then
    if out=$("$PROVISION" --check-only --quiet 2>&1); then
        add ok "容器依赖齐备" "tkinter/pyserial/opencv/PIL/psutil/字体"
    else
        add fail "容器依赖缺失" "$(echo "$out" | head -1)"
    fi
elif [ "$HCONTAINER" = 1 ]; then
    add warn "依赖检查脚本缺失" "$PROVISION 不存在"
else
    add fail "容器依赖不可检" "容器未运行"
fi

# ---- 3. 比赛大屏 ----
# 注意：不能用 `xdotool search --name '分拣信息'` 判断中文窗口名。
# xdotool 3.20160805 只匹配 WM_NAME(STRING)，而 Tk 把 WM_NAME 写成双重编码的
# 乱码字节，真正的 UTF-8 标题在 _NET_WM_NAME 里，因此中文子串永远匹配不到
# （实测「智能分拣 · 分拣信息」窗口的 WM_NAME 是 mojibake）。
# 这里改为：枚举可见窗口 -> 用 xprop 读 _NET_WM_NAME -> 再做子串比较。
bigscreen_win() {
    local w name
    for w in $(sudo -n -u sunrise env DISPLAY=:0 xdotool search --onlyvisible --name '.' 2>/dev/null); do
        name=$(DISPLAY=:0 xprop -id "$w" _NET_WM_NAME 2>/dev/null | sed -n 's/^_NET_WM_NAME(UTF8_STRING) = "\(.*\)"$/\1/p')
        [ -z "$name" ] && name=$(DISPLAY=:0 xprop -id "$w" WM_NAME 2>/dev/null | sed -n 's/^WM_NAME(STRING) = "\(.*\)"$/\1/p')
        case "$name" in *分拣信息*) echo "$w"; return 0 ;; esac
    done
    return 1
}

if systemctl is-active --quiet x5_bigscreen.service; then
    if win=$(bigscreen_win); then
        add ok "比赛大屏" "x5_bigscreen active，物理屏窗口 id=${win}"
    else
        add warn "比赛大屏窗口缺失" "服务 active 但 :0 上没有「分拣信息」窗口"
    fi
else
    add fail "比赛大屏未运行" "systemctl start x5_bigscreen.service"
fi

# ---- 4. 物理屏 ----
if sudo -u sunrise env DISPLAY=:0 xdpyinfo >/dev/null 2>&1; then
    cur=$(sudo -u sunrise env DISPLAY=:0 xrandr 2>/dev/null | sed -n '1s/.*current \([0-9]\+\) x \([0-9]\+\).*/\1x\2/p')
    if [ "$cur" = "800x480" ]; then
        add ok "物理屏 :0" "framebuffer=$cur"
    else
        add warn "物理屏分辨率异常" "当前 ${cur:-未知}（应为 800x480）"
    fi
    if sudo -u sunrise env DISPLAY=:0 xset q 2>/dev/null | grep -q "DPMS is Disabled"; then
        add ok "DPMS 已禁用" "不会自动黑屏"
    else
        add warn "DPMS 可能启用" "比赛中可能自动黑屏 → bash /root/rkx5_remote/x5_display_setup.sh :0 800x480"
    fi
else
    add fail "物理屏 :0 不可用" "lightdm/Xorg 未就绪？"
fi

# ---- 5. 远程服务 ----
if curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" 2>/dev/null | grep -q '^200$'; then
    add ok "远程服务 :$PORT" "/healthz 正常"
else
    add warn "远程服务 :$PORT 无响应" "调试界面镜像不可用 (systemctl restart x5_remote)"
fi

# ---- 6. 网页调试台（可选, 前台进程） ----
if docker exec "$CONTAINER" pgrep -af "[v]ision_tool.py web" >/dev/null 2>&1; then
    add ok "网页调试台运行中" ":8090 (浏览器访问)"
else
    [ "$MODE" = "--quiet" ] || add ok "网页调试台" "未运行（./is.sh web 启动）"
fi

# ---- 7. 资源余量 ----
if [ -n "${LOG_DIR}" ] && [ -d "$LOG_DIR" ]; then
    used=$(df -P "$LOG_DIR" | awk 'NR==2{gsub("%","",$5); print $5}')
else
    used=$(df -P / | awk 'NR==2{gsub("%","",$5); print $5}')
fi
if [ -n "${used:-}" ]; then
    if [ "$used" -ge 92 ]; then
        add fail "磁盘使用率过高" "${used}%（磁盘已损坏，务必清理日志）"
    elif [ "$used" -ge 85 ]; then
        add warn "磁盘使用率偏高" "${used}%"
    else
        add ok "磁盘余量" "使用率 ${used}%"
    fi
fi
memav=$(awk '/MemAvailable/{printf "%d", $2/1024}' /proc/meminfo)
if [ "${memav:-0}" -lt 300 ]; then
    add warn "可用内存偏低" "${memav}MB（无 swap，opencv 可能被 OOM）"
else
    add ok "可用内存" "${memav}MB"
fi

# ---- 8. 意外重启 ----
for u in intelligentstoring.service; do
    if nr=$(systemctl show "$u" -p NRestarts --value 2>/dev/null); then
        if [ "${nr:-0}" -gt 0 ]; then
            add warn "$u 重启过" "NRestarts=$nr（查 journalctl -u $u）"
        else
            add ok "$u" "NRestarts=0"
        fi
    fi
done

# ---- 输出 ----
if [ "$MODE" = "--json" ]; then
    printf '{"time":"%s","fails":%d,"checks":[' "$(date -Is)" "$FAILS"
    first=1
    for r in "${RESULT[@]}"; do
        IFS='|' read -r st name detail <<< "$r"
        [ $first -eq 1 ] || printf ','
        first=0
        printf '{"status":"%s","name":"%s","detail":"%s"}' "$st" "$name" "${detail//\"/\\\"}"
    done
    printf ']}\n'
elif [ "$MODE" != "--quiet" ]; then
    if [ "$FAILS" -eq 0 ]; then
        echo "----- 结果: 全部通过 -----"
    else
        echo "----- 结果: $FAILS 项失败 -----"
    fi
fi

[ "$FAILS" -eq 0 ]
