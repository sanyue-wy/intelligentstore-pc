#!/bin/bash
# ============================================================================
# bpu_bridge.sh —— 把 RDK X5 宿主系统的 BPU 运行库"借"给容器 (在宿主上运行)
#
# 为什么需要
#   地平线 BPU 运行库 (hobot_dnn / hbm_runtime / libdnn / libhbrt_bayes ...) 只装在
#   宿主系统里, 且 Python 扩展是为宿主的 Python 3.10 编译的。
#   容器用同为 Ubuntu 22.04 + Python 3.10 的镜像时, 只读挂载这些文件即可直接用 BPU,
#   不需要在容器里安装任何地平线软件包, 宿主系统升级后重建容器即自动跟随。
#
# 做什么
#   1. 用宿主 python3 定位 BPU 相关 Python 包 (hobot_dnn / hobot_dnn_rdkx5 / hbm_runtime / Hobot.GPIO)
#   2. ldd 这些包里的 .so, 收集非 glibc 依赖库; 再按名字收集常见的 BPU 动态加载库
#      (libdnn* libhb* libcnn_intf* ...: 它们可能被 dlopen, ldd 看不到)
#   3. 生成 docker run 参数文件 (默认 deploy/.bpu_bridge.args, 每行一个参数),
#      provision_intelligentstoring.sh 创建容器时自动读取
#
# 用法
#   sudo deploy/bpu_bridge.sh               # 生成参数文件并打印摘要
#   sudo deploy/bpu_bridge.sh --print       # 只打印, 不写文件
# 输出变量 (容器内)
#   PYTHONPATH=/opt/hostbpu/py      LD_LIBRARY_PATH=/opt/hostbpu/lib:/usr/hobot/lib
# ============================================================================
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${BPU_BRIDGE_ARGS:-"$HERE/.bpu_bridge.args"}
PRINT_ONLY=0
[ "${1:-}" = "--print" ] && PRINT_ONLY=1

PY=${HOST_PYTHON:-python3}
MODS="hobot_dnn hobot_dnn_rdkx5 hbm_runtime Hobot"
# 容器自己有的基础库, 不能用宿主的覆盖
SKIP_RE='/(ld-linux[^/]*|libc\.so[^/]*|libm\.so[^/]*|libpthread\.so[^/]*|libdl\.so[^/]*|librt\.so[^/]*|libstdc\+\+\.so[^/]*|libgcc_s\.so[^/]*|libpython3[^/]*|libz\.so[^/]*|libutil\.so[^/]*|libresolv\.so[^/]*)$'
NAME_RE='^lib(dnn|hb|hbrt|hbmem|hbucp|ucp|cnn_intf|alog|hlog|hbtl|hobot|bpu|vio|spcdev|gdcbin|multimedia)[^/]*\.so[^/]*$'

declare -A LIBS=()     # 容器内名字 -> 宿主真实路径
ARGS=()
SUMMARY=()

add_lib() {   # $1 = 宿主路径 (可为软链)
    local p=$1 name real
    [ -e "$p" ] || return 0
    name=$(basename "$p")
    real=$(readlink -f "$p")
    [ -n "${LIBS[$name]:-}" ] && return 0
    LIBS[$name]=$real
}

# ---------------------------------------------------------------- 1. Python 包
PKG_COUNT=0
for m in $MODS; do
    loc=$("$PY" - "$m" 2>/dev/null <<'EOF'
import importlib.util, sys
s = importlib.util.find_spec(sys.argv[1])
if s is None:
    sys.exit(1)
if s.submodule_search_locations:
    print(list(s.submodule_search_locations)[0])
else:
    print(s.origin)
EOF
) || continue
    [ -n "$loc" ] && [ -e "$loc" ] || continue
    ARGS+=("-v" "$loc:/opt/hostbpu/py/$(basename "$loc"):ro")
    SUMMARY+=("python 包  $m  <- $loc")
    PKG_COUNT=$((PKG_COUNT + 1))
    # 包内扩展的依赖
    while IFS= read -r so; do
        while IFS= read -r dep; do
            [[ "$dep" =~ $SKIP_RE ]] && continue
            add_lib "$dep"
        done < <(ldd "$so" 2>/dev/null | awk '/=>/ && $3 ~ /^\// {print $3}')
    done < <(find "$loc" -name "*.so*" -type f 2>/dev/null)
done

# ---------------------------------------------------------------- 2. 按名字收集
for d in /usr/lib /usr/lib/aarch64-linux-gnu /usr/local/lib /usr/hobot/lib; do
    [ -d "$d" ] || continue
    for f in "$d"/lib*.so*; do
        [ -e "$f" ] || continue
        [[ "$(basename "$f")" =~ $NAME_RE ]] && add_lib "$f"
    done
done
# 这些库自己的依赖
for name in "${!LIBS[@]}"; do
    while IFS= read -r dep; do
        [[ "$dep" =~ $SKIP_RE ]] && continue
        add_lib "$dep"
    done < <(ldd "${LIBS[$name]}" 2>/dev/null | awk '/=>/ && $3 ~ /^\// {print $3}')
done

for name in "${!LIBS[@]}"; do
    ARGS+=("-v" "${LIBS[$name]}:/opt/hostbpu/lib/$name:ro")
done
# /usr/hobot 整体只读挂到同路径 (部分库按绝对路径读配置/固件)
[ -d /usr/hobot ] && ARGS+=("-v" "/usr/hobot:/usr/hobot:ro")
[ -f /etc/version ] && ARGS+=("-v" "/etc/version:/etc/rdk_host_version:ro")
ARGS+=("-e" "PYTHONPATH=/opt/hostbpu/py")
ARGS+=("-e" "LD_LIBRARY_PATH=/opt/hostbpu/lib:/usr/hobot/lib")
ARGS+=("-e" "IS_HOST_BPU_BRIDGE=1")

# ---------------------------------------------------------------- 输出
echo "[bpu_bridge] 宿主系统: $(cat /etc/version 2>/dev/null || echo 未知)  python: $("$PY" -V 2>&1)"
for s in "${SUMMARY[@]}"; do echo "[bpu_bridge] $s"; done
echo "[bpu_bridge] 动态库 ${#LIBS[@]} 个 -> /opt/hostbpu/lib"
if [ "$PKG_COUNT" = 0 ]; then
    echo "[bpu_bridge][警告] 宿主 python3 里没找到 hobot_dnn / hbm_runtime。"
    echo "                  先在宿主确认: python3 -c 'from hobot_dnn import pyeasy_dnn'"
    echo "                  或安装: sudo apt install hobot-dnn  (RDK OS 3.x)"
fi
if [ "$PRINT_ONLY" = 1 ]; then
    printf '%s\n' "${ARGS[@]}"
    exit 0
fi
printf '%s\n' "${ARGS[@]}" > "$OUT" || { echo "[bpu_bridge][失败] 无法写 $OUT" >&2; exit 1; }
echo "[bpu_bridge] 已写入 $OUT (重建容器后生效: deploy/provision_intelligentstoring.sh --recreate)"
[ "$PKG_COUNT" -gt 0 ]
