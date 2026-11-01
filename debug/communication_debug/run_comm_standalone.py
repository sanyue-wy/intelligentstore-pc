"""
STM32 智能分拣系统 - 通讯模块独立调试运行入口
"""

import sys
import os

# 本文件位于 debug/communication_debug/, 需上溯两级到项目根
CUR_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(os.path.dirname(CUR_DIR))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from debug.communication_debug.app import run_debug_gui


def run_comm_standalone(backend: str = None):
    """通讯模块独立调试器启动入口"""
    print("[启动] 通讯模块独立调试器...")
    run_debug_gui(backend=backend)


if __name__ == "__main__":
    run_comm_standalone()