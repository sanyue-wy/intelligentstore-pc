"""
STM32 智能分拣系统 - 调试工作台与各模块独立调试器

目录按业务模块一一对应组织, **替换某模块的实现时, 连同其对应的 debug 子文件夹
一起替换即可**, 上层无需改动:

    debug/common/                  跨模块共用组件 (2D 画布、深色主题)
    debug/communication_debug/     <->  communication/
    debug/vision_debug/            <->  vision/
    debug/message_center_debug/    <->  MessegeCenter/
    debug/monitor_debug/           <->  moniter/
    debug/integrated_debug/        全系统综合联调 (依赖全部模块, 不单独替换)

各调试器的具体说明见 debug/README.md。
"""

from typing import Any

# 惰性导出表: 公开名 -> (子包路径, 子包内的对象名)
# 采用 PEP 562 惰性导入, 避免 `import debug` 就把 tkinter/cv2/PIL/psutil 全部拉进来。
_LAZY_EXPORTS = {
    "UnifiedSystemDebugger": (".integrated_debug.unified_debugger", "UnifiedSystemDebugger"),
    "run_unified_debugger": (".integrated_debug.unified_debugger", "run_unified_debugger"),

    "SortingDebugApp": (".communication_debug.app", "SortingDebugApp"),
    "run_debug_gui": (".communication_debug.app", "run_debug_gui"),

    "VisionStandaloneDebugger": (".vision_debug.run_vision_standalone", "VisionStandaloneDebugger"),
    "run_vision_standalone": (".vision_debug.run_vision_standalone", "run_vision_standalone"),

    "MessageCenterDebugger": (
        ".message_center_debug.run_standalone", "MessageCenterDebugger"),
    "run_message_center_standalone": (
        ".message_center_debug.run_standalone", "run_message_center_standalone"),

    "MonitorStandaloneDebugger": (".monitor_debug.run_standalone", "MonitorStandaloneDebugger"),
    "run_monitor_standalone": (".monitor_debug.run_standalone", "run_monitor_standalone"),

    "PlatformCanvasView": (".common.canvas_view", "PlatformCanvasView"),
    "COLORS": (".common.theme", "COLORS"),
    "apply_dark_theme": (".common.theme", "apply_dark_theme"),
}

__all__ = list(_LAZY_EXPORTS.keys())


def __getattr__(name: str) -> Any:
    """PEP 562 模块级惰性导入。"""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module
    module_path, attr = target
    value = getattr(import_module(module_path, __name__), attr)
    globals()[name] = value  # 缓存, 后续直接命中
    return value


def __dir__():
    return sorted(set(globals().keys()) | set(_LAZY_EXPORTS.keys()))