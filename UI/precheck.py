# -*- coding: utf-8 -*-
"""
赛前自检清单 —— 全部为绿才算"可以比赛"。

执行页待机时自动刷新显示; 命令行:  ./is.sh precheck   (= python3 UI/precheck.py)

检查项:
    相机      有新图像、帧率、掉线重连次数
    BPU       运行库可用, 当前模型是 .bin (否则提示 CPU 推理很慢)
    模型      类别与 classes.json 一致, 没有"新 .pt 尚未转换"
    标定      已标定、重投影误差 <= 2mm、分辨率一致
    下位机    串口已连接 (不是仿真)
    大屏      8756 大屏服务可达 (否则只能本地窗口兜底)
    启动键    有装置上的物理按键 (USB 键 / GPIO)
    磁盘      记录目录剩余空间 > 500MB
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _item(name: str, ok: bool, detail: str = "", level: str = "") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail, "level": level or ("ok" if ok else "error")}


def run_precheck(vision=None, client=None, ui=None, trigger=None) -> List[dict]:
    items: List[dict] = []

    # ---------------------------------------------------------------- 视觉
    if vision is not None:
        try:
            rep = vision.self_check(log=False)
        except Exception as e:
            rep = {"camera": {}, "engine": {}, "calibration": {}, "problems": [f"自检异常: {e}"]}
        cam = rep.get("camera", {})
        items.append(_item("相机", cam.get("connected") and cam.get("fps", 0) > 0,
                           f"{cam.get('desc', '')} {cam.get('fps', 0)}fps 重连{cam.get('reconnects', 0)}次"))
        eng = rep.get("engine", {})
        det_eng = getattr(getattr(vision, "detector", None), "engine", None)
        backend = getattr(det_eng, "backend", "") if det_eng is not None else ""
        model = os.path.basename(getattr(det_eng, "model_path", "") or "") if det_eng is not None else ""
        if det_eng is None:
            items.append(_item("模型", False, "未启用 YOLO (HSV 颜色检测)", "warn"))
        else:
            notice = getattr(det_eng, "notice", "")
            items.append(_item("模型", eng.get("ready") and not notice,
                               notice or f"{model} ({backend}) {len(eng.get('classes', []))} 类",
                               "ok" if eng.get("ready") and not notice else ("warn" if eng.get("ready") else "error")))
            items.append(_item("BPU 加速", backend == "bpu",
                               "BPU" if backend == "bpu" else f"当前 {backend or '无'} 推理 (CPU 慢)。"
                               "检查: 宿主 deploy/bpu_doctor.sh / ./convert.sh", "ok" if backend == "bpu" else "warn"))
        cal = rep.get("calibration", {})
        cal_ok = cal.get("calibrated") and float(cal.get("reproj_error_mm", 99)) <= 2.0
        size_bad = [p for p in rep.get("problems", []) if "画面" in p and "不一致" in p]
        items.append(_item("标定", cal_ok and not size_bad,
                           (f"{cal.get('points', 0)} 点, 误差 {cal.get('reproj_error_mm')} mm"
                            if cal.get("calibrated") else "未标定 (坐标不可信)") + (" | " + size_bad[0] if size_bad else "")))
        rest = [p for p in rep.get("problems", []) if not any(k in p for k in ("相机", "YOLO", "标定", "画面", "转换", "ONNX"))]
        if rest:
            items.append(_item("视觉配置", False, "; ".join(rest), "warn"))

    # ---------------------------------------------------------------- 下位机
    if client is not None:
        try:
            mock = bool(getattr(client, "is_mock", False))
            conn = bool(getattr(client, "is_connected", False))
            desc = getattr(client, "link_description", "")
            st = getattr(client, "latest_status", None)
            alarm = bool(st and (st.is_alarm or st.is_estop))
            items.append(_item("下位机", conn and not mock and not alarm,
                               ("仿真下位机 (ALLOW_MOCK)" if mock else str(desc))
                               + (" | 报警/急停中" if alarm else ""),
                               "warn" if mock else ""))
        except Exception as e:
            items.append(_item("下位机", False, str(e)))

    # ---------------------------------------------------------------- 大屏
    if ui is not None:
        try:
            ok = ui.client.available()
            items.append(_item("大屏服务", ok, ui.client.endpoint if ok else
                               f"{ui.client.endpoint} 不可达 (x5_bigscreen 服务未启动? 将无法显示分拣信息)"))
        except Exception as e:
            items.append(_item("大屏服务", False, str(e)))

    # ---------------------------------------------------------------- 启动键
    if trigger is not None:
        phys = trigger.has_physical()
        items.append(_item("启动键", phys, trigger.describe() if phys else
                           f"没有装置上的物理按键 ({trigger.describe()})。赛规要求一键启动且不碰电脑, "
                           "建议接 USB 单键按钮或 GPIO 按键", "ok" if phys else "warn"))

    # ---------------------------------------------------------------- 磁盘
    try:
        free = shutil.disk_usage(ROOT).free / 1e6
        items.append(_item("磁盘", free > 500, f"剩余 {free:.0f} MB", "ok" if free > 500 else "warn"))
    except Exception:
        pass
    return items


def summarize(items: List[dict]) -> str:
    bad = [i for i in items if i["level"] == "error"]
    warn = [i for i in items if i["level"] == "warn"]
    if bad:
        return "未通过: " + "、".join(i["name"] for i in bad)
    if warn:
        return "可以比赛 (有提醒: " + "、".join(i["name"] for i in warn) + ")"
    return "全部通过, 可以比赛"


def main():
    import logging
    import time
    logging.basicConfig(level=logging.WARNING)
    from communication import create_comm_client
    from vision import create_vision_service
    from UI.display_ui import CompetitionUI
    from UI.start_trigger import StartTrigger

    vision = create_vision_service(backend=os.environ.get("IS_VISION_BACKEND", "yolo"), continuous_detect=False)
    vision.set_camera_source(use_synthetic=os.environ.get("USE_SYNTHETIC_CAM", "0") == "1")
    vision.start()
    client = create_comm_client()
    try:
        client.connect(os.environ.get("STM_PORT") or None, 115200)
    except Exception:
        pass
    ui = CompetitionUI()
    ui.reset("自检")
    trig = StartTrigger()
    trig.start()
    time.sleep(2.5)
    items = run_precheck(vision, client, ui, trig)
    for i in items:
        mark = {"ok": "\033[32m✔\033[0m", "warn": "\033[33m!\033[0m", "error": "\033[31m✘\033[0m"}[i["level"]]
        print(f" {mark} {i['name']:<8} {i['detail']}")
    print("\n" + summarize(items))
    vision.stop()
    try:
        client.disconnect()
    except Exception:
        pass
    ui.close()
    sys.exit(0 if not any(i["level"] == "error" for i in items) else 1)


if __name__ == "__main__":
    main()
