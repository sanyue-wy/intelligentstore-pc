#!/bin/bash
# intelligentstoring 快捷工具 (在 RDK X5 宿主上执行)
C=${CONTAINER:-intelligentstoring}
D="$(cd "$(dirname "$0")" && pwd)"
case "${1:-help}" in
  start|web)  # 统一网页调试台 :8090 (视觉/流程/通讯/完整); 比赛模式下应先关闭
              shift; docker exec -it $C bash -lc "cd /app && python3 tools/vision_tool.py web $*" ;;
  shell)      docker exec -it $C bash -c 'cd /app && exec bash' ;;
  log)        docker exec $C tail -f /tmp/run_main.log ;;
  stop)       docker exec $C bash -c 'pkill -f "[r]un_competition.py"; pkill -f "[v]ision_tool"' 2>/dev/null; echo "程序已停止" ;;
  status)     docker ps -a --filter name=$C --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
              docker exec $C pgrep -af "[r]un_competition|[v]ision_tool" 2>/dev/null || echo "程序未运行" ;;
  restart)    docker restart $C >/dev/null && echo "容器已重启" ;;
  race)       # 比赛模式开关: on = 开机自启比赛程序 (待机等启动键); off = 关闭, 回到调试
              case "${2:-}" in
                on)  docker exec $C pkill -f "[v]ision_tool" 2>/dev/null
                     systemctl enable --now intelligentstoring-competition.service && echo "比赛模式已开启 (开机自动待机), 执行页 :8088" ;;
                off) systemctl disable --now intelligentstoring-competition.service && echo "比赛模式已关闭" ;;
                *)   systemctl status intelligentstoring-competition.service --no-pager 2>/dev/null | head -5 ;;
              esac ;;
  run)        shift; exec "$D/start_competition.sh" "$@" ;;
  precheck)   docker exec -it $C bash -lc 'cd /app && python3 UI/precheck.py' ;;
  gates)      # A3 视觉门禁 (11 项: 标定/成像/限位/位姿); 不改 precheck 语义 (那个仍走 UI/precheck.py)
              # 默认 --check: 放行=0, 有 FAIL 项=非零; 现场也可 ./is.sh gates --seal 给参考点盖章
              shift; A="$*"; [ -z "$A" ] && A="--check"
              docker exec $C bash -lc "cd /app && python3 -m vision.gates $A" ;;
  convert)    shift; exec "$D/convert.sh" "$@" ;;
  doctor)     exec bash "$D/deploy/bpu_doctor.sh" ;;
  migrate)    # 迁移到 ubuntu:22.04 + BPU 挂载的新容器 (旧容器改名保留)
              bash "$D/deploy/bpu_bridge.sh" && HOST_DIR="$D" bash "$D/deploy/provision_intelligentstoring.sh" --recreate ;;
  *) cat <<EOT
用法: ./is.sh <命令>
  比赛
    run              比赛程序前台运行 (待机 -> 启动键开始), 执行页 :8088
    race on|off      比赛模式开机自启 开/关 (systemd)
    precheck         赛前自检清单
    gates [--seal]   视觉门禁 11 项 (标定/成像/限位/位姿); --seal = 给当前参考点盖章
  模型
    convert [参数]   一键转换 (先把 .pt 放进 vision/models/)
  调试 (全部网页, 笔记本浏览器访问)
    web / start [--arm]   统一调试台 :8090 (视觉/流程/通讯/完整; 比赛模式下会被自动关闭)
    shell / log / stop / status / restart
  环境
    doctor           BPU / 容器诊断 (把输出发给维护者)
    migrate          迁移到 ubuntu:22.04 + 宿主 BPU 挂载的新容器 (旧容器改名保留)
EOT
  ;;
esac
