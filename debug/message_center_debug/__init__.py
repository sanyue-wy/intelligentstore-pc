"""
[消息中心] 独立监听与事件注入工具 —— 与 MessegeCenter/ 配套
"""

from .run_standalone import MessageCenterDebugger, run_message_center_standalone

__all__ = ["MessageCenterDebugger", "run_message_center_standalone"]
