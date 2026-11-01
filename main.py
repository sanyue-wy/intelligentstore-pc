"""
STM32 智能分拣系统 - 上位机调试启动入口
用法:
  python main.py             # 启动全系统综合联合调试工作台 (推荐)
  python main.py --comm      # 独立启动下位机通讯调试器
  python main.py --vision    # 视觉调参 -> 浏览器调试台 http://<板子IP>:8090 (所有实时画面调参都在这里)
  python main.py --msg       # 独立启动消息中心监听与事件注入器
  python main.py --monitor   # 独立启动系统监控与看门狗看板

指定模块后端 (替换某模块实现后用于验证, 上层代码无需改动):
  python main.py --vision --vision-backend=<后端名>
  python main.py --comm   --comm-backend=<后端名>
  python main.py --msg    --msg-backend=<后端名>
  python main.py --monitor --monitor-backend=<后端名>

  综合工作台可同时指定多个:
  python main.py --vision-backend=xxx --comm-backend=yyy --monitor-backend=zzz

  各模块的后端名见对应模块的 interfaces.py 注册表。

各调试器按业务模块分别位于 debug/<模块>_debug/ 子文件夹, 详见 debug/README.md
"""

import sys
import os

# 将根目录加入模块搜索路径
cur_dir = os.path.dirname(os.path.abspath(__file__))
if cur_dir not in sys.path:
    sys.path.insert(0, cur_dir)


#: 命令行后端参数名 -> 内部统一称呼
_BACKEND_FLAGS = {
    "--vision-backend": "vision",
    "--comm-backend": "comm",
    "--msg-backend": "msg",
    "--monitor-backend": "monitor",
}


def _parse_backends(args):
    """
    解析各模块的后端指定参数。

    返回 {模块: 后端名}, 未指定的模块不出现在结果中 (由该模块使用默认后端)。
    同时兼容单个 --backend=<名> 写法, 按当前模式作用于视觉模块。
    """
    picked = {}
    for a in args:
        if "=" not in a:
            continue
        flag, value = a.split("=", 1)
        module = _BACKEND_FLAGS.get(flag)
        if module and value:
            picked[module] = value

    # 兼容写法: --backend=xxx 在视觉模式下作用于视觉
    legacy = "--backend"
    for a in args:
        if a.startswith(legacy + "="):
            value = a.split("=", 1)[1]
            if value:
                picked.setdefault("vision", value)

    return picked


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    backends = _parse_backends(args)
    unknown = [a for a in args if "=" in a and a.split("=", 1)[0] not in _BACKEND_FLAGS
               and not a.startswith("--backend=")]
    if unknown:
        print(f"[错误] 无法识别的参数: {', '.join(unknown)}")
        print("       可用: " + "  ".join(_BACKEND_FLAGS) + "  --backend=")
        sys.exit(1)

    if "--comm" in args:
        try:
            from debug.communication_debug.run_comm_standalone import run_comm_standalone
        except ImportError as e:
            print(f"[错误] 加载通讯调试模块失败: {e}")
            sys.exit(1)
        run_comm_standalone(backend=backends.get("comm"))
    elif "--vision" in args:
        print("[启动] 视觉浏览器调参台...")
        try:
            from debug.vision_debug.run_vision_standalone import run_vision_standalone
        except ImportError as e:
            print(f"[错误] 加载视觉调试模块失败: {e}")
            print(f"       提示: 请检查当前 Python 环境是否安装了 opencv-python: pip install opencv-python")
            sys.exit(1)
        run_vision_standalone(backend=backends.get("vision"))
    elif "--msg" in args:
        print("[启动] 消息中心独立监听器...")
        try:
            from debug.message_center_debug.run_standalone import run_message_center_standalone
        except ImportError as e:
            print(f"[错误] 加载消息中心调试模块失败: {e}")
            sys.exit(1)
        run_message_center_standalone(backend=backends.get("msg"))
    elif "--monitor" in args:
        print("[启动] 程序监控独立诊断器...")
        try:
            from debug.monitor_debug.run_standalone import run_monitor_standalone
        except ImportError as e:
            print(f"[错误] 加载监控调试模块失败: {e}")
            sys.exit(1)
        run_monitor_standalone(backend=backends.get("monitor"))
    else:
        print("=" * 65)
        print("  STM32 智能分拣系统 - 全系统联合调试与排障工作台")
        print("  协议: USB CDC 32 字节定长二进制帧 | 模块: 视觉 + 通讯 + 消息 + 监控")
        for module, label in (("vision", "视觉"), ("comm", "通讯"),
                              ("msg", "消息中心"), ("monitor", "监控")):
            if module in backends:
                print(f"  {label}后端: {backends[module]}")
        print("=" * 65)
        try:
            from debug.integrated_debug.unified_debugger import run_unified_debugger
        except ImportError as e:
            print(f"[错误] 加载综合调试工作台失败: {e}")
            print(f"       提示: 综合工作台需要 opencv-python, PIL 与 tkinter。")
            print(f"       若是独立调试某一模块，可尝试运行对应参数: --comm, --msg, --monitor, --vision")
            sys.exit(1)
        run_unified_debugger(
            vision_backend=backends.get("vision"),
            comm_backend=backends.get("comm"),
            monitor_backend=backends.get("monitor"),
            msg_backend=backends.get("msg"),
        )