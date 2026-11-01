#!/bin/bash
# ============================================================================
# provision_intelligentstoring.sh —— 容器「一条命令恢复」幂等脚本 (RDK X5)
#
# 2026-10 起容器改为: ubuntu:22.04 (Python 3.10, 与宿主一致) + 只读挂载宿主 BPU 运行库
#   * 旧镜像 ultralytics/ultralytics:latest-arm64 的 Python 更新, 加载不了宿主为 3.10 编译的
#     hobot_dnn / hbm_runtime, YOLO 只能 CPU 推理 (每帧几百 ms)。新方案 BPU 每帧十几 ms。
#   * 不需要 docker build (本机 BuildKit 已损坏、legacy builder 会 OOM):
#     直接 docker pull ubuntu:22.04, 依赖在容器创建后由本脚本安装 (幂等)。
#     也可以在笔记本 WSL 里 build 好镜像再 docker save | ssh 板子 docker load (见 deploy/OPERATIONS.md)。
#   * BPU 运行库由 deploy/bpu_bridge.sh 在宿主上探测, 生成 deploy/.bpu_bridge.args, 创建容器时挂载。
#
# 背景 (仍然成立)
#   本机 BuildKit 存储已损坏, legacy builder 在 apt 阶段 OOM, docker commit 也失败,
#   所以容器被删后依赖要靠本脚本补回来。
#
# 幂等性
#     - 容器存在且 Running  -> 不做任何 docker run
#     - 容器存在但 Exited   -> docker start
#     - 容器不存在          -> docker run (含 BPU 挂载)
#     - 依赖已存在          -> 跳过 apt / pip / 字体写入
#
#   --recreate    旧容器改名为 <名字>_old_<时间> 保留 (可回退), 按新镜像 + BPU 挂载重建
#   --check-only  只检查不修改（用于健康检查 / 自检）
#   --force-apt   即使检测通过也重新执行 apt-get install
#   --no-export   不安装 torch/ultralytics (板上一键转换需要它们导出 ONNX)
#   --quiet       少输出（systemd 里用）
#
# 退出码  0 依赖齐备（或已补齐）  1 失败
# ============================================================================
set -uo pipefail

# ---------------------------------------------------------------- 配置 ----
CONTAINER=${CONTAINER:-intelligentstoring}
IMAGE=${IMAGE:-ubuntu:22.04}
HOST_DIR=${HOST_DIR:-"$(cd "$(dirname "$0")/.." && pwd)"}   # 默认 = 本脚本所在工程目录
# 调试界面虚拟屏（物理屏 :0 留给比赛大屏，绝不与调试窗口抢）
UI_DISPLAY=${UI_DISPLAY:-:99}
UI_SIZE=${UI_SIZE:-1280x820}
LOG_DIR=${LOG_DIR:-/var/log/intelligentstoring}

# pip 走清华镜像（apt 走容器自带的 ports.ubuntu.com，实测可达）
PIP_INDEX=${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}
PIP_HOST=${PIP_HOST:-pypi.tuna.tsinghua.edu.cn}

# apt 包：Python3.10 / 图像 / X 工具 / 中文字体 / ssh(一键转换连笔记本)
APT_PACKAGES="python3 python3-pip python3-tk python3-dev ca-certificates libgl1 libglib2.0-0 openssh-client xauth x11-utils x11-xserver-utils fonts-wqy-zenhei fonts-wqy-microhei fontconfig procps"
# pip 包：numpy 必须 < 2 (宿主 hobot_dnn 按 numpy 1.x 编译), opencv 4.11 是最后支持 numpy 1.x 的版本
# 版本与根目录 requirements.txt / requirements-export.txt 一致 (改一处要同步)
# 每项带单引号: 要原样传进容器里的 bash, 否则 '<' 会被当成重定向
PIP_CONSTRAINTS="'numpy==1.26.4' 'opencv-python==4.11.0.86' 'opencv-python-headless==4.11.0.86' 'ultralytics<9' 'onnx<1.19' 'onnxruntime<1.23'"
PIP_PACKAGES="numpy opencv-python pillow psutil pyserial onnxruntime"
# 板上一键转换 (.pt -> ONNX) 需要: CPU 版 torch + ultralytics + onnx
PIP_EXPORT="torch ultralytics onnx"
TORCH_INDEX=${TORCH_INDEX:-https://download.pytorch.org/whl/cpu}

CHECK_ONLY=0
FORCE_APT=0
QUIET=0
RECREATE=0
WITH_EXPORT=1
for a in "$@"; do
    case "$a" in
        --check-only) CHECK_ONLY=1 ;;
        --force-apt)  FORCE_APT=1 ;;
        --quiet)      QUIET=1 ;;
        --recreate)   RECREATE=1 ;;
        --no-export)  WITH_EXPORT=0 ;;
        -h|--help)    sed -n '2,36p' "$0"; exit 0 ;;
        *) echo "[错误] 未知参数: $a" >&2; exit 2 ;;
    esac
done

log()  { [ "$QUIET" = 1 ] || echo "[provision] $*"; }
warn() { echo "[provision][警告] $*" >&2; }
die()  { echo "[provision][失败] $*" >&2; exit 1; }

# ------------------------------------------------------- 0. 前置检查  ----
command -v docker >/dev/null 2>&1 || die "找不到 docker 命令"

# 容器内执行（总是 bash -lc，保证 PATH 与交互 shell 一致）
dexec() { docker exec "$CONTAINER" bash -lc "$*"; }

container_exists() {
    docker inspect "$CONTAINER" >/dev/null 2>&1
}
container_running() {
    [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]
}

# ------------------------------------------------- 1. 创建 / 启动容器 ----
bpu_args_file() { echo "$HOST_DIR/deploy/.bpu_bridge.args"; }

load_bpu_args() {
    # 宿主 BPU 运行库挂载参数; 没有就现场生成
    local f; f=$(bpu_args_file)
    BPU_ARGS=()
    if [ ! -s "$f" ]; then
        local gen="$HOST_DIR/deploy/bpu_bridge.sh"
        [ -x "$gen" ] || gen="$(dirname "$0")/bpu_bridge.sh"
        if [ -f "$gen" ]; then
            BPU_BRIDGE_ARGS="$f" bash "$gen" >/dev/null 2>&1 || warn "宿主未找到 BPU 运行库, 容器内 YOLO 将退回 CPU"
        fi
    fi
    if [ -s "$f" ]; then
        mapfile -t BPU_ARGS < "$f"
        log "BPU 运行库挂载参数: $f ($(grep -c -- '^-v' "$f") 项)"
    else
        warn "没有 BPU 挂载参数 ($f), 容器内 YOLO 将退回 CPU。宿主执行 deploy/bpu_bridge.sh 后 --recreate"
    fi
}

create_container() {
    [ -d "$HOST_DIR" ] || die "宿主程序目录不存在: $HOST_DIR（磁盘或项目被移除了？）"
    docker image inspect "$IMAGE" >/dev/null 2>&1 || {
        log "拉取镜像 $IMAGE ..."
        docker pull "$IMAGE" >/dev/null || die "docker pull $IMAGE 失败 (网络? 可在笔记本 docker save 后 docker load)"
    }
    load_bpu_args
    log "创建容器 $CONTAINER (镜像 $IMAGE) ..."
    #   --privileged   摄像头 /dev/video*、串口 /dev/ttyACM*、BPU 设备节点需要
    #   --network host 容器内直接用 127.0.0.1:8756 访问宿主大屏服务, 执行页 :8088 直接对外
    #   /dev           透传硬件节点
    #   /tmp/.X11-unix X11 socket（连接宿主 :0 / :99）
    #   $LOG_DIR       程序日志落在宿主机独立目录, 由宿主 logrotate 统一轮转
    #   BPU_ARGS       宿主 BPU 运行库只读挂载 (deploy/bpu_bridge.sh 生成)
    mkdir -p "$LOG_DIR" || die "无法创建日志目录 $LOG_DIR"
    docker run -d --name "$CONTAINER" --privileged --network host \
        -v "$HOST_DIR:/app" \
        -v /dev:/dev \
        -v /tmp/.X11-unix:/tmp/.X11-unix:rw \
        -v /root/.Xauthority:/root/.Xauthority:ro \
        -v "$LOG_DIR:$LOG_DIR" \
        "${BPU_ARGS[@]}" \
        -w /app \
        -e DISPLAY="$UI_DISPLAY" \
        -e PYTHONUNBUFFERED=1 \
        -e TZ=Asia/Shanghai \
        --restart unless-stopped \
        --entrypoint bash "$IMAGE" -c "sleep infinity" \
        >/dev/null || die "docker run 失败"
    log "容器已创建"
}

retire_container() {
    container_exists || return 0
    local bak="${CONTAINER}_old_$(date +%Y%m%d_%H%M%S)"
    docker stop "$CONTAINER" >/dev/null 2>&1 || true
    docker update --restart=no "$CONTAINER" >/dev/null 2>&1 || true
    docker rename "$CONTAINER" "$bak" || die "无法改名旧容器"
    log "旧容器已停用并改名为 $bak (确认新容器正常后可 docker rm $bak; 回退: docker rename $bak $CONTAINER)"
}

ensure_container() {
    if [ "$RECREATE" = 1 ]; then
        retire_container
    fi
    if container_running; then
        log "容器 $CONTAINER 已在运行"
        return 0
    fi
    if container_exists; then
        log "容器 $CONTAINER 存在但未运行，启动中 ..."
        docker start "$CONTAINER" >/dev/null || die "docker start 失败"
    else
        warn "容器 $CONTAINER 不存在（被删除？），按标准参数重建"
        create_container
    fi
    # 等 bash 可用（容器刚起时 exec 可能还进不去）
    for _ in $(seq 1 30); do
        dexec true >/dev/null 2>&1 && return 0
        sleep 0.5
    done
    die "容器启动后 15s 内无法 exec 进入"
}

# -------------------------------------------------------- 2. 依赖检查 ----
# python 侧：一次 import 全部模块，任何缺失都会失败
PY_CHECK='import tkinter, serial, numpy, cv2, PIL, psutil, onnxruntime'
apt_ok() {
    dexec "dpkg -s python3-tk >/dev/null 2>&1 && dpkg -s x11-utils >/dev/null 2>&1 \
           && dpkg -s fonts-wqy-microhei >/dev/null 2>&1"
}
py_ok() {
    dexec "python3 -c '$PY_CHECK'" >/dev/null 2>&1
}
font_ok() {
    # 项目代码写死 "Segoe UI"，容器内已映射到文泉驿微米黑
    dexec "test -f /etc/fonts/conf.d/99-segoe-alias.conf && fc-match 'Segoe UI' | grep -qi wqy"
}
xdpy_ok() { dexec "command -v xdpyinfo >/dev/null 2>&1"; }

all_ok() { apt_ok && py_ok && font_ok && xdpy_ok; }

if [ "$CHECK_ONLY" = 1 ]; then
    if ! container_running; then
        if container_exists; then st="已停止"; else st="不存在"; fi
        echo "[check] 容器 $CONTAINER $st，依赖无法校验" >&2
        exit 1
    fi
    fail=""
    apt_ok   || fail="$fail apt"
    py_ok    || fail="$fail python-modules"
    font_ok  || fail="$fail font-alias"
    xdpy_ok  || fail="$fail xdpyinfo"
    dexec "cd /app && python3 -c 'from vision.yolo_engine import is_bpu_available as f; import sys; sys.exit(0 if f() else 1)'" >/dev/null 2>&1 \
        || echo "[check][警告] 容器内 BPU 不可用 (不计入失败)" >&2
    if [ -n "$fail" ]; then
        echo "[check] 缺失:$fail" >&2
        exit 1
    fi
    log "依赖检查通过（容器运行中，apt/python/字体/X 工具齐备）"
    exit 0
fi

ensure_container

cur_img=$(docker inspect -f '{{.Config.Image}}' "$CONTAINER" 2>/dev/null)
if [ -n "$cur_img" ] && [ "$cur_img" != "$IMAGE" ]; then
    warn "当前容器镜像是 $cur_img, 不是 $IMAGE (BPU 可能不可用)。迁移: $0 --recreate"
fi
PIPX="pip install"
dexec "pip install --help 2>/dev/null | grep -q break-system-packages" && PIPX="pip install --break-system-packages"

# ----------------------------------------------------- 3. 补齐 apt 包  ----
if [ "$FORCE_APT" = 1 ] || ! apt_ok; then
    log "补齐 apt 依赖 ..."
    # 注意: 必须清理 apt 列表缓存（容器镜像里 lists 是空的）
    dexec "export DEBIAN_FRONTEND=noninteractive; \
           apt-get update -qq && \
           apt-get install -y -qq --no-install-recommends $APT_PACKAGES && \
           rm -rf /var/lib/apt/lists/*" \
        || die "apt-get install 失败（网络或 apt 源问题？可在容器内手动重试）"
    log "apt 依赖已安装"
else
    log "apt 依赖已齐备，跳过"
fi

# ----------------------------------------------------- 4. 补齐 pip 包  ----
if ! py_ok; then
    log "补齐 python 依赖 (pip) ..."
    # 清华镜像 --trusted-host 兜底（容器证书链缺失时也能装）
    dexec "export PIP_INDEX_URL=$PIP_INDEX PIP_TRUSTED_HOST=$PIP_HOST PIP_NO_CACHE_DIR=1; \
           printf '%s\n' $PIP_CONSTRAINTS > /etc/pip-constraints.txt; \
           $PIPX -q -c /etc/pip-constraints.txt $PIP_PACKAGES" \
        || die "pip install 失败（网络问题？可手动在容器内执行: pip install -i $PIP_INDEX $PIP_PACKAGES）"
    py_ok || die "pip 安装后仍无法 import ($PY_CHECK)"
    log "python 依赖已安装"
else
    log "python 依赖已齐备，跳过"
fi

# -------------------------------------- 4b. 板上一键转换的导出依赖 (可选) ----
export_ok() { dexec "python3 -c 'import torch, ultralytics, onnx'" >/dev/null 2>&1; }
if [ "$WITH_EXPORT" = 1 ] && ! export_ok; then
    log "安装导出依赖 (CPU 版 torch + ultralytics + onnx, 首次约 5~15 分钟) ..."
    dexec "export PIP_NO_CACHE_DIR=1; printf '%s\n' $PIP_CONSTRAINTS > /etc/pip-constraints.txt; \
           $PIPX -q -c /etc/pip-constraints.txt --index-url $TORCH_INDEX torch \
             || $PIPX -q -c /etc/pip-constraints.txt -i $PIP_INDEX --trusted-host $PIP_HOST torch; \
           $PIPX -q -c /etc/pip-constraints.txt -i $PIP_INDEX --trusted-host $PIP_HOST ultralytics onnx" \
        || warn "导出依赖安装失败: 板上 ./convert.sh 无法导出 ONNX (比赛运行不受影响)"
fi

# ------------------------------------------------------- 4c. BPU 自检 ----
bpu_ok() { dexec "cd /app && python3 -c 'from vision.yolo_engine import is_bpu_available as f; import sys; sys.exit(0 if f() else 1)'" >/dev/null 2>&1; }
if bpu_ok; then
    log "BPU 运行库: 容器内可用"
else
    warn "BPU 运行库: 容器内不可用 (YOLO 退回 CPU)。排查: 宿主执行 deploy/bpu_doctor.sh"
fi

# ------------------------------------------------------- 5. 字体别名  ----
if ! font_ok; then
    log "写入 Segoe UI -> WenQuanYi Micro Hei 字体别名 ..."
    # 项目里字体族写死 "Segoe UI"（Windows 字体），容器内映射到文泉驿，避免豆腐块
    cat > /tmp/_99-segoe-alias.conf <<'FONTCONF'
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <alias binding="same"><family>Segoe UI</family><prefer><family>WenQuanYi Micro Hei</family></prefer></alias>
  <match target="pattern">
    <test qual="any" name="family"><string>Segoe UI</string></test>
    <edit name="family" mode="assign" binding="strong"><string>WenQuanYi Micro Hei</string></edit>
  </match>
</fontconfig>
FONTCONF
    docker cp /tmp/_99-segoe-alias.conf "$CONTAINER:/etc/fonts/conf.d/99-segoe-alias.conf" \
        >/dev/null || die "docker cp 字体配置失败"
    rm -f /tmp/_99-segoe-alias.conf
    dexec "fc-cache -f > /dev/null 2>&1"
    font_ok || die "字体别名写入后 fc-match 仍不匹配"
    log "字体别名已生效"
else
    log "字体别名已存在，跳过"
fi

# ------------------------------------------------- 6. 运行时目录准备  ----
# 容器内日志目录（挂载自宿主 /var/log/intelligentstoring，独立文件系统边界，
# 由宿主 logrotate 负责轮转）。
dexec "mkdir -p $LOG_DIR /tmp/sort_shots" >/dev/null 2>&1 || \
    warn "无法创建 $LOG_DIR（可能未挂载，日志将退回 journald）"

# 日志目录必须是「挂载进来的」而不是容器内随手建的，否则容器一删日志全丢，
# 宿主 logrotate 也轮转不到。老容器（本次改造前创建）缺这个挂载，这里检出并提示。
if ! docker inspect "$CONTAINER" --format '{{range .HostConfig.Binds}}{{.}}{{"\n"}}{{end}}' \
        2>/dev/null | grep -q ":$LOG_DIR"; then
    warn "当前容器没有挂载 $LOG_DIR（日志会丢在容器内层），如需修复请执行："
    warn "  docker rm -f $CONTAINER && $0"
fi

# ------------------------------------------------------ 7. 最终校验 ----
if ! all_ok; then
    die "依赖补齐后自检仍未通过，请手动排查：docker exec -it $CONTAINER bash"
fi

log "完成：容器 $CONTAINER 运行中，依赖齐备 (python3.10/tkinter/pyserial/opencv/onnxruntime/字体/X 工具)"
exit 0
