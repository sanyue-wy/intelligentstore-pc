"""
上位机通讯接口调用示例 (面向视觉系统或自动化脚本开发人员)
对应 D:\\stm32\\stm32cubemx\\IntelligentSorting\\User\\Module\\Communication\\HOST_COMM_GUIDE.md
"""

import time
import os
import sys

# 保证父路径在搜索路径中
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from communication import (
    IntelligentSortingClient,
    IntelligentSortingHost,
    SysState,
    ErrorMask,
    GripperState,
    ResStage
)


def main():
    print("=" * 65)
    print("  STM32 智能分拣系统 - 上位机通讯客户端示例")
    print("  依据 HOST_COMM_GUIDE.md: 32字节定长帧 | USB CDC | 20Hz 状态交互")
    print("=" * 65)

    # 1. 实例化客户端 (可以使用 IntelligentSortingClient 或别名 IntelligentSortingHost)
    client = IntelligentSortingHost()

    # 2. 列出系统可用串口
    ports = client.list_serial_ports()
    print(f"检测到可用串口: {ports}")

    # 自动发现串口 (Linux: /dev/ttyACM* /dev/ttyUSB*; Windows: COMx);
    # 找不到下位机时自动降级到虚拟仿真模式, 保证流程不中断。
    print("\n[1] 正在连接设备 (自动发现, 无硬件则降级虚拟仿真)...")
    client.connect_auto()
    print(f"    当前链路: {client.link_description}")
    print(f"    选择依据: {client.last_port_detail}")

    # 3. 注册状态与报警监听回调
    def on_status(st):
        # 20Hz 实时状态回调，按需获取
        pass

    def on_alarm(st):
        print(f"  [告警回调] 触发异常: 0x{st.error:04X} -> {st.error_desc}")

    client.on_status_callbacks.append(on_status)
    client.on_alarm_callbacks.append(on_alarm)

    time.sleep(0.1)

    # 4. 链路健康与看门狗状态检查
    print(f"[2] 通信链路状态: 活跃={client.is_link_alive}, 距上次接收={client.link_age_s*1000:.1f}ms")

    # 5. 前置检查与清警
    print("[3] 下发清除报警与急停复位...")
    client.send_clear()
    time.sleep(0.05)

    # 6. 执行一次标准分拣任务 (工件坐标: X=45.0mm, Y=25.0mm)
    task_id = 101
    print(f"\n[4] 开始执行分拣任务 ID={task_id}, 目标坐标: (45.0, 25.0) mm...")

    def progress_monitor(stage_name, pct):
        print(f"  -> [{pct:3d}%] {stage_name}")

    def vision_secondary_check():
        print("  -> [视觉二次确认] 机械臂已到达工件正上方，开始二次精定位...")
        time.sleep(0.2)
        print("  -> [视觉二次确认] 目标确认无偏差，授权抓取 (PERMIT)！")
        return True

    res = client.execute_sorting_task_ex(
        task_id=task_id,
        target_x_mm=45.0,
        target_y_mm=25.0,
        timeout_s=15.0,
        progress_callback=progress_monitor,
        permit_callback=vision_secondary_check
    )

    if res.success:
        print("\n[OK] 分拣任务圆满完成！")
    else:
        if res.is_grip_fail:
            print(f"\n[WARN] 抓空/抓取失败！{res.message}")
            print("       -> 依据协议指南，机械臂已自动抬起安全待命，可触发视觉重新寻位。")
        else:
            print(f"\n[FAIL] 分拣任务未成功！原因: {res.message} (错误码: 0x{res.error:04X})")

    # 7. 手动控制示例 (张开夹爪、闭合夹爪)
    print("\n[5] 单动控制: 闭合夹爪...")
    client.send_grip_close()
    time.sleep(0.3)
    st = client.latest_status
    print(f"    夹爪状态: {st.gripper_desc if st else '未知'}")

    print("[6] 单动控制: 张开夹爪...")
    client.send_grip_open()
    time.sleep(0.3)
    st = client.latest_status
    print(f"    夹爪状态: {st.gripper_desc if st else '未知'}")

    # 8. 断开连接
    client.disconnect()
    print("\n已断开连接，测试完成。")


if __name__ == "__main__":
    main()
