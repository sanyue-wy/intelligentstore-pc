#!/bin/bash
# ============================================================================
# bpu_doctor.sh —— BPU / 容器环境一键诊断 (在 RDK X5 宿主上运行, 把输出整段发给维护者)
#   sudo deploy/bpu_doctor.sh
# ============================================================================
CONTAINER=${CONTAINER:-intelligentstoring}
HERE=$(cd "$(dirname "$0")" && pwd)
sec() { printf '\n==== %s ====\n' "$*"; }

sec "宿主系统"
echo "RDK 版本: $(cat /etc/version 2>/dev/null || echo 无 /etc/version)"
grep PRETTY_NAME /etc/os-release 2>/dev/null
uname -rm
echo "内存: $(free -h 2>/dev/null | awk '/Mem:/{print $2" 总, "$7" 可用"}')"
echo "磁盘: $(df -h / 2>/dev/null | awk 'NR==2{print $4" 可用"}')"
echo "python3: $(python3 -V 2>&1)"

sec "宿主 BPU 运行库"
python3 - <<'PY'
import importlib.util
for m in ("hbm_runtime", "hobot_dnn", "hobot_dnn_rdkx5", "Hobot.GPIO"):
    try:
        s = importlib.util.find_spec(m)
    except Exception as e:
        s = None
    loc = (list(s.submodule_search_locations)[0] if s and s.submodule_search_locations else (s.origin if s else ""))
    ok = "无"
    if s:
        try:
            __import__(m)
            ok = "可导入"
        except Exception as e:
            ok = f"导入失败: {e}"
    print(f"  {m:<16} {ok:<10} {loc}")
try:
    from hobot_dnn import pyeasy_dnn  # noqa
    print("  hobot_dnn.pyeasy_dnn OK")
except Exception as e:
    print("  hobot_dnn.pyeasy_dnn 失败:", e)
PY
pip3 list 2>/dev/null | grep -i -E "hobot|hbm|numpy|opencv" | sed 's/^/  pip: /'
ls -d /usr/hobot/lib 2>/dev/null && ls /usr/hobot/lib 2>/dev/null | head -20 | sed 's/^/  /'
ls /dev 2>/dev/null | grep -i -E "bpu|ion|hobot|cnn" | head | sed 's/^/  dev: /'

sec "docker"
docker --version 2>&1
docker ps -a --filter "name=$CONTAINER" --format '  {{.Names}}  {{.Image}}  {{.Status}}'
if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "  容器 python: $(docker exec "$CONTAINER" python3 -V 2>&1)"
    echo "  BPU 挂载: $(docker inspect -f '{{range .Mounts}}{{.Destination}} {{end}}' "$CONTAINER" | tr ' ' '\n' | grep -c hostbpu) 项"
    docker exec "$CONTAINER" bash -lc 'cd /app 2>/dev/null && python3 - <<PY
try:
    from vision.yolo_engine import is_bpu_available, resolve_model_path
    print("  容器内 BPU:", "可用" if is_bpu_available() else "不可用")
    print("  将加载模型:", resolve_model_path())
except Exception as e:
    print("  容器内检查失败:", e)
for m in ("torch", "ultralytics", "onnx", "onnxruntime", "cv2", "numpy"):
    try:
        mod = __import__(m)
        print(f"  {m:<12} {getattr(mod, \"__version__\", \"ok\")}")
    except Exception as e:
        print(f"  {m:<12} 缺失")
PY'
fi

sec "BPU 挂载探测 (deploy/bpu_bridge.sh --print 摘要)"
bash "$HERE/bpu_bridge.sh" --print 2>&1 | grep "^\[bpu_bridge\]"
echo
echo "把以上全部输出发给维护者即可。"
