#!/bin/bash
# intelligentstoring 快捷工具 (在 RDK X5 宿主上执行)
C=${CONTAINER:-intelligentstoring}
D="$(cd "$(dirname "$0")" && pwd)"
case "${1:-help}" in
  start)    shift; exec bash "$D/deploy/is_debug.sh" on "$@" ;;
  shell)    docker exec -it $C bash -c 'cd /app && exec bash' ;;
  log)      docker exec $C tail -f /tmp/run_main.log ;;
  stop)     docker exec $C bash -c 'pkill -f "[m]ain\.py"' 2>/dev/null; echo "程序已停止" ;;
  status)   docker ps -a --filter name=$C --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
            docker exec $C pgrep -af "[m]ain\.py|[r]un_competition|[v]ision_tool" 2>/dev/null || echo "程序未运行" ;;
  restart)  docker restart $C >/dev/null && echo "容器已重启" ;;
  race)     # 比赛模式开关: on = 开机自启比赛程序 (待机等启动键); off = 关闭, 回到调试
            case "${2:-}" in
              on)  systemctl stop 'intelligentstoring-debug@*' 2>/dev/null
                   systemctl enable --now intelligentstoring-competition.service && echo "比赛模式已开启 (开机自动待机), 执行页 :8088" ;;
              off) systemctl disable --now intelligentstoring-competition.service && echo "比赛模式已关闭" ;;
              *)   systemctl status intelligentstoring-competition.service --no-pager 2>/dev/null | head -5 ;;
            esac ;;
  run)      shift; exec "$D/start_competition.sh" "$@" ;;
  precheck) docker exec -it $C bash -lc 'cd /app && python3 UI/precheck.py' ;;
  convert)  shift; exec "$D/convert.sh" "$@" ;;
  web)      shift; docker exec -it $C bash -lc "cd /app && python3 tools/vision_tool.py web $*" ;;
  doctor)   exec bash "$D/deploy/bpu_doctor.sh" ;;
  migrate)  # 迁移到 ubuntu:22.04 + BPU 挂载的新容器 (旧容器改名保留)
            bash "$D/deploy/bpu_bridge.sh" && HOST_DIR="$D" bash "$D/deploy/provision_intelligentstoring.sh" --recreate ;;
  *) cat <<EOT
用法: ./is.sh <命令>
  比赛
    run              比赛程序前台运行 (待机 -> 启动键开始), 执行页 :8088
    race on|off      比赛模式开机自启 开/关 (systemd)
    precheck         赛前自检清单
  模型
    convert [参数]   一键转换 (先把 .pt 放进 vision/models/)
  调试
    web [--arm]      网页调试台 :8090 (比赛模式下会被自动关闭)
    start [参数]     Tk 调试工作台 (参数: --comm --msg --monitor; 视觉调参用 web)
    shell / log / stop / status / restart
  环境
    doctor           BPU / 容器诊断 (把输出发给维护者)
    migrate          迁移到 ubuntu:22.04 + 宿主 BPU 挂载的新容器 (旧容器改名保留)
EOT
  ;;
esac
