#!/usr/bin/env python3
"""
视觉调试工具 (RDK X5 上直接运行, 无需显示器: 用笔记本浏览器打开网页即可)

    python3 tools/vision_tool.py web                 # 网页调试台 http://<板子IP>:8090
    python3 tools/vision_tool.py check               # 自检 + 实拍 3 次 scan, 打印每件结果
    python3 tools/vision_tool.py check --images DIR  # 对图片目录逐张识别 (PC/板子都可), 标注图存 /tmp/vision_check
    python3 tools/vision_tool.py capture --out dataset/raw --interval 1   # 采集训练/校准图片
    python3 tools/vision_tool.py bench               # 推理测速
    python3 tools/vision_tool.py regress             # 回归集: 换模型后重跑归档的问题画面, 对比结果

通用参数: --camera uvc|hikrobot|replay|synthetic  --replay DIR  (临时覆盖 vision_config.json)

网页调试台功能 (python3 tools/vision_tool.py web [--arm]):
    * 实时标注画面 (MJPEG), 帧率 / 推理耗时 / 自检问题
    * [Scan] 按比赛方式做一次多帧融合识别;  [Snap] 保存原图 (训练数据、量化校准图)
    * 点击画面: 像素对应的机械臂坐标 + HSV 值
    * 点哪去哪 (--arm): 机械臂开到点击位置上方停住 0.8s 再取消 (不下潜不夹取), 肉眼验标定
    * 托盘: 白色托盘自动定位 + 托盘坐标系 (内框尺寸/外沿/参考点/机械臂轴向), 叠加显示检测结果
    * 畸变: 棋盘格逐张拍摄 + 计算内参; 或用托盘直边估计畸变; 可切换去畸变画面对比
    * 标定/ROI: 点击画面加标定点 (托盘模式下作为参考点; --arm 时自动读取机械臂坐标), 手动 ROI
    * 相机: 曝光/增益/白平衡滑块实时生效, 一键写回 vision_config.json
    * 颜色: 点选各色货物采样, 自动生成 classes.json 的 HSV 区间 (换灯光后 1 分钟重标)
    * 大屏: 按最近一次识别渲染比赛大屏画面 (与装置屏一致)
    * 复盘: 浏览每轮比赛报告 (每件结果/截图/识别画面/流程), 识别错的画面一键归档到回归集
比赛程序运行时 (执行页 :8088 在跑) 拒绝启动, 避免遥控嫌疑和抢相机/串口; --force 强制。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# RDK X5 容器的 OpenCL 上下文不可用, 畸变页「一键校正」走 undistortPoints 会无限挂起
# (见 vision/__init__.py)。必须在 import cv2 前关掉。
os.environ.setdefault("OPENCV_OPENCL_RUNTIME", "disabled")

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import logging  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vision_tool")


def build_service(args, continuous=True):
    if args.camera:
        os.environ["IS_CAM_TYPE"] = args.camera
    if args.replay:
        os.environ["IS_CAM_REPLAY"] = args.replay
    if args.model:
        os.environ["IS_YOLO_MODEL"] = os.path.abspath(args.model)
    from vision import create_vision_service
    svc = create_vision_service(backend=args.backend or None, continuous_detect=continuous)
    svc.set_camera_source(use_synthetic=(args.camera == "synthetic"))
    svc.start()
    return svc


def t2d(t):
    # A1: 多带 shape 与 diagnostics —— 调试台靠 color_conflict 把冲突目标标红并给三个按钮。
    return {"id": t.target_id, "label": t.label, "class": t.class_name, "color": t.color,
            "conf": round(t.confidence, 3), "u": t.u_pixel, "v": t.v_pixel,
            "x_mm": t.x_mm, "y_mm": t.y_mm, "hits": t.hits, "category": t.category,
            "shape": getattr(t, "shape", ""),
            "diag": dict(getattr(t, "diagnostics", None) or {}),
            "angle": t.angle_deg}


def print_targets(ts):
    if not ts:
        print("  (无目标)")
    for t in ts:
        print(f"  #{t.target_id:<2} {t.label or t.color:<12} cls={t.class_name or '-':<16} "
              f"color={t.color:<10} conf={t.confidence:.2f} px=({t.u_pixel:6.1f},{t.v_pixel:6.1f}) "
              f"mm=({t.x_mm:7.1f},{t.y_mm:7.1f}) hits={t.hits}")


# ============================================================================== check
def cmd_check(args):
    if args.images:
        from vision.calibration import CoordinateCalibrator
        from vision.yolo_detector import YoloWorkpieceDetector
        det = YoloWorkpieceDetector(CoordinateCalibrator())
        print(det.engine.describe() if det.using_yolo else "YOLO 不可用, HSV 检测")
        os.makedirs("/tmp/vision_check", exist_ok=True)
        files = sorted(f for f in glob.glob(os.path.join(args.images, "*"))
                       if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")))
        for f in files:
            img = cv2.imread(f)
            if img is None:
                continue
            t0 = time.perf_counter()
            ts = det.detect(img)
            ms = (time.perf_counter() - t0) * 1e3
            print(f"{os.path.basename(f)}: {len(ts)} 件 {ms:.0f} ms | {det.engine.describe()} | 过滤 {det.rejected}")
            print_targets(ts)
            cv2.imwrite(os.path.join("/tmp/vision_check", os.path.basename(f)), det.annotate(img, ts))
        print("标注图: /tmp/vision_check")
        return
    svc = build_service(args, continuous=False)
    try:
        rep = svc.self_check()
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
        for i in range(args.n):
            ts, _ = svc.scan()
            print(f"scan {i + 1}: {svc.last_scan}")
            print_targets(ts)
    finally:
        svc.stop()


# ============================================================================== capture
def cmd_capture(args):
    svc = build_service(args, continuous=False)
    os.makedirs(args.out, exist_ok=True)
    n = 0
    try:
        print(f"采集到 {args.out}, 每 {args.interval}s 一张, 共 {args.count} 张 (Ctrl+C 结束)。"
              f"建议: 每次换货物摆放/数量/角度, 覆盖托盘各区域与现场灯光")
        while n < args.count:
            fr = svc.get_fresh_frames(1, 3.0)
            if fr:
                p = os.path.join(args.out, time.strftime("%Y%m%d_%H%M%S") + f"_{n:04d}.jpg")
                cv2.imwrite(p, fr[0], [cv2.IMWRITE_JPEG_QUALITY, 95])
                n += 1
                print(f"  [{n}] {p}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        svc.stop()


# ============================================================================== bench
def cmd_bench(args):
    svc = build_service(args, continuous=False)
    try:
        fr = svc.get_fresh_frames(1, 5.0)
        if not fr:
            print("无图像")
            return
        det = svc.detector
        eng = getattr(det, "engine", None)
        for _ in range(3):
            det.detect(fr[0])
        ts = []
        for _ in range(args.n):
            t0 = time.perf_counter()
            if eng is not None and eng.is_ready:
                eng.infer(fr[0])
            ts.append((time.perf_counter() - t0) * 1e3)
        print(f"纯推理: 平均 {np.mean(ts):.1f} ms, P95 {np.percentile(ts, 95):.1f} ms | "
              f"{eng.describe() if eng else 'HSV'}")
        t0 = time.perf_counter()
        for _ in range(3):
            svc.scan()
        print(f"scan() 平均 {(time.perf_counter() - t0) / 3 * 1e3:.0f} ms (含取 {svc.cfg.scan['frames']} 帧 + 融合 + 精定位)")
    finally:
        svc.stop()


# ============================================================================== regress
def cmd_regress(args):
    """
    回归集对比: dataset/regression/ 里每张图 (从复盘页归档的识别出错/可疑画面) 用当前模型重新识别,
    与归档时的结果并排打印; 差异行标黄。换模型 (./convert.sh) 后跑一遍, 确认老问题修好、没有新问题。
    """
    from vision.calibration import CoordinateCalibrator
    from vision.yolo_detector import YoloWorkpieceDetector
    det = YoloWorkpieceDetector(CoordinateCalibrator())
    print(det.engine.describe() if det.using_yolo else "YOLO 不可用, HSV 检测")
    files = sorted(glob.glob(os.path.join(args.dir, "*.jpg")))
    if not files:
        print(f"{args.dir} 里没有图片 (在调试台「复盘」页点「归档」添加)")
        return
    out = os.path.join(args.dir, "_latest")
    os.makedirs(out, exist_ok=True)
    changed = 0
    for f in files:
        img = cv2.imread(f)
        if img is None:
            continue
        ts = det.detect(img)
        new = sorted(t.label for t in ts)
        old = []
        jp = os.path.splitext(f)[0] + ".json"
        if os.path.exists(jp):
            with open(jp, encoding="utf-8") as fh:
                meta = json.load(fh)
            old = sorted(x.get("label", "") for x in meta.get("targets", []))
            note = meta.get("note", "")
        else:
            note = ""
        diff = old != new
        changed += diff
        c = "\033[1;33m" if diff else ""
        print(f"{c}{os.path.basename(f)}  {note}\n   归档时: {old}\n   现在:   {new}\033[0m")
        cv2.imwrite(os.path.join(out, os.path.basename(f)), det.annotate(img, ts))
    print(f"\n{len(files)} 张, {changed} 张结果有变化。标注图: {out}")


# ============================================================================== web
TOKEN_FILE = os.path.join(ROOT, "tools", ".web_token")


def _web_token(args) -> str:
    """网页口令: IS_WEB_TOKEN > tools/.web_token (首次生成 6 位数字并保存, 重启不变, 书签可用)。"""
    if getattr(args, "no_auth", False):
        return ""
    t = os.environ.get("IS_WEB_TOKEN", "").strip()
    if t:
        return t
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            t = f.read().strip()
    except OSError:
        t = ""
    if not t:
        import secrets
        t = f"{secrets.randbelow(10 ** 6):06d}"
        try:
            with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                f.write(t + "\n")
            os.chmod(TOKEN_FILE, 0o600)
        except OSError as e:
            log.warning("口令文件写入失败 (%s), 本次口令仅在日志里", e)
    return t


LOGIN_HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>视觉调试台 - 口令</title><body style="font-family:sans-serif;margin:3em">
<h3>视觉调试台</h3><p>输入口令（启动日志里打印的 6 位数字，也保存在板子上 tools/.web_token）。</p>
<form onsubmit="location='/?t='+encodeURIComponent(document.getElementById('t').value);return false">
<input id=t autofocus inputmode=numeric style="font-size:1.4em;width:8em"> <button style="font-size:1.2em">进入</button></form>"""


HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>智能分拣调试台 · 视觉</title><link rel="stylesheet" href="/static/app.css">
<style>body{font-family:"Microsoft YaHei",sans-serif;background:#1e1e24;color:#ddd;margin:0;display:flex}
#l{padding:8px}#r{padding:8px;flex:1;font-size:13px;max-width:560px}img{border:1px solid #555}#v{cursor:crosshair}
button{margin:2px;padding:6px 10px;background:#335;color:#fff;border:0;border-radius:4px;cursor:pointer}
button.on{background:#a60}table{border-collapse:collapse}td,th{border:1px solid #444;padding:2px 5px}
pre{white-space:pre-wrap;background:#111;padding:6px;max-height:200px;overflow:auto}.bad{color:#f66}.ok{color:#3d8}.hint{color:#fa4}
.tabs button{background:#223}.tabs button.on{background:#a60}.pane{display:none}.pane.on{display:block}.dim{opacity:.6}
input[type=range]{width:220px}#msg{color:#fb3;min-height:18px}.thumb{width:160px;cursor:pointer}</style></head>
<body><style>.isnav{position:fixed;top:0;left:0;right:0;height:36px;background:#12161e;display:flex;align-items:center;gap:4px;padding:0 10px;z-index:999;border-bottom:1px solid #2a3444;font-family:"Microsoft YaHei",sans-serif}.isnav a{color:#9aa7b8;text-decoration:none;padding:7px 12px;border-radius:6px;font-size:14px}.isnav a:hover{background:#26303f;color:#fff}.isnav a.on{background:#2f6fdf;color:#fff}.isnav .sp{flex:1}.isnav .bdg{font-size:12px;color:#8fa6c6;border:1px solid #34435c;border-radius:10px;padding:2px 8px}body{padding-top:40px}</style><div class="isnav"><span style="color:#fff;font-weight:700;margin-right:10px">智能分拣调试台</span><a href="/" class="on">视觉调试</a><a href="/flow">流程调试</a><a href="/comm">通讯调试</a><a href="/run">完整调试</a><span class="sp"></span><span class="bdg">调试模式 · 非比赛</span><a href="/screen.png" target="_blank">大屏预览</a></div><div id="l"><img id="v" src="/stream"><br>
<button onclick="scan()">Scan (比赛同款)</button><button onclick="snap()">Snap 存原图</button>
置信度 <input type="range" min="5" max="95" value="40" id="conf" oninput="setconf(this.value)" style="width:120px"> <span id="cv">0.40</span>
<div id="msg"></div><div id="tb"></div></div>
<div id="r"><div class="tabs"><button onclick="tab('st')" id="t_st" class="on">状态</button><button onclick="tab('cal')" id="t_cal">标定/ROI</button>
<button onclick="tab('tray')" id="t_tray">托盘</button><button onclick="tab('dist')" id="t_dist">畸变</button>
<button onclick="tab('cam')" id="t_cam">相机</button><button onclick="tab('col')" id="t_col">颜色</button>
<button onclick="tab('scr')" id="t_scr">大屏</button><button onclick="tab('run')" id="t_run">复盘</button></div>
<div class="pane on" id="p_st"><pre id="st"></pre>
 <b>点击画面</b>: 显示像素对应的机械臂坐标。<span id="gotobox"><br><label><input type="checkbox" id="goto"> 点哪去哪: 机械臂开到点击位置上方停住 (不夹取), 用来验标定</label>
 抓取高 <input type="number" id="gh" value="0" min="0" max="150" step="1" style="width:52px"> mm
 <span class="dim">(0 = 按托盘底面算; 点货物顶面时填实际抓取高度, 例如 30mm 货 × 0.6 = 18, 用来验高度投影)</span></span></div>
<div class="pane" id="p_cal"><button id="bc" onclick="mode('calib')">标定模式</button><button id="br" onclick="mode('roi')">ROI 模式</button><span id="extra"></span>
 <p>标定: 托盘内均匀放 9 个标记, 逐个点击并输入机械臂坐标 (--arm 时自动读取当前位置)。ROI: 依次点击托盘轮廓。</p><b>标定点</b><div id="cp"></div></div>
<div class="pane" id="p_tray"><p>托盘坐标系: 原点 = 画面左上方的内框角, +x 沿画面向右, +y 沿画面向下。画面里绿框 = 检测到的白色外边界 (红 = 本帧没检测到), 青框 = 推算的内框, 红/绿箭头 = 托盘 +x/+y, 紫叉 = 参考点。</p>
 <label><input type="checkbox" id="tr_en"> 启用托盘模式</label> <label><input type="checkbox" id="tr_roi"> 托盘内框自动作 ROI</label><br>
 内框 W (左右) <input id="tr_w" size="5"> mm  H (上下) <input id="tr_h" size="5"> mm<br>
 外沿 rim <input id="tr_rim" size="4"> mm (白色外边界到内框)  壁高 <input id="tr_rh" size="4"> mm
 镜头高 <input id="tr_ch" size="5"> mm (镜头光心到托盘底面的垂直高度; 和反算值差得多会报警)<br>
 机械臂 +X = <select id="tr_ax"><option>tray_x</option><option>-tray_x</option><option>tray_y</option><option>-tray_y</option></select>
 机械臂 +Y = <select id="tr_ay"><option>tray_y</option><option>-tray_y</option><option>tray_x</option><option>-tray_x</option></select> (≥2 个参考点时自动求, 此项忽略)<br>
 <b>参考点</b> (托盘坐标 → 机械臂坐标; 也可在「标定/ROI」页点击画面添加):<div id="tr_refs"></div>
 托盘 x,y <input id="tr_rt" size="9" placeholder="20,30"> 机械臂 x,y <input id="tr_rr" size="9" placeholder="150,-20"> <button onclick="trref()">加参考点</button><br>
 <button onclick="trsave()">保存并应用</button><button onclick="trdet()">重新检测托盘</button><button onclick="trk1(false)">用托盘直边估计畸变</button><button onclick="trk1(true)">估计并写入</button>
 <p><b>固定托盘 (托盘不动时推荐)</b>: 点「点四角」, 在画面上依次点托盘<b>内框底面</b>的四个角 (顺序随意), 点满 4 个自动保存, 之后不再自动检测托盘。
 相机斜着看时托盘是梯形, 必须点四角 (矩形 x,y,w,h 只适合正对)。青框应贴住内框, 红/绿箭头从左上内角出发。</p>
 <label><input type="checkbox" id="fx_snap" checked> 自动吸附到白色边缘 (= 壁顶内沿, 需填壁高)</label> 不吸附时点的是 <select id="fx_z"><option value="0">底面内角</option><option value="rim">壁顶内沿</option></select>
 <button id="bfx" onclick="mode('corners')">点四角</button><button onclick="fxoff()">取消固定 (恢复自动检测)</button> <span id="fxst"></span><br>
 透视 <select id="tr_ps"><option value="auto">auto (按相机位姿, 斜看也准)</option><option value="off">off (旧公式)</option></select>
 焦距 <input id="tr_f" size="5" placeholder="自动"> px (一般留空)
 <pre id="trst"></pre></div>
<div class="pane" id="p_dist"><p><b>一键校正 (推荐)</b>: <a href="/calib_board.pdf" target="_blank" style="color:#8cf">下载打印纸</a> (A4 棋盘格, 打印缩放无所谓), 平铺在托盘上 (尽量盖满托盘, 压平, 别反光) → 点「一键校正」, 几秒后自动写入并切到去畸变画面, 托盘边/格子线应变直。
 想更准: 把纸挪到画面另一侧/角落, 点「再拍一张叠加」(2~3 张就够)。不满意点「撤销」。</p>
 <button onclick="done(false)">一键校正</button><button onclick="done(true)">再拍一张叠加</button><button onclick="dundo()">撤销 (恢复上一次内参)</button><pre id="dost"></pre>
<details><summary>完整棋盘格标定 (一般不需要)</summary><p>棋盘格贴在硬板上, 每次换位置/倾角点「拍一张」(检测到角点才保存, 画面上出现橙点), 覆盖画面四角, 15~25 张后「计算内参」。</p>
 内角点 列 <input id="ch_c" size="2" value="9"> 行 <input id="ch_r" size="2" value="6"> 方格 <input id="ch_s" size="3" value="20"> mm<br>
 <button onclick="chsnap()">拍一张</button><button onclick="chsolve()">计算内参并写入</button><button onclick="chclear()">清空图片</button>
 <pre id="chst"></pre></details>
 <label><input type="checkbox" id="undv" onchange="post('/api/view',{undist:this.checked})"> 画面显示去畸变结果 (直线应变直)</label></div>
<div class="pane" id="p_cam"><p>曝光/增益/白平衡实时生效, 满意后点保存 (写入 vision_config.json, 比赛时锁定)。</p>
 曝光 us <input type="range" id="exp" min="100" max="40000" step="100" oninput="$('expv').innerText=this.value"> <span id="expv"></span><br>
 增益 dB <input type="range" id="gain" min="0" max="24" step="0.5" oninput="$('gainv').innerText=this.value"> <span id="gainv"></span><br>
 白平衡 K <input type="range" id="wb" min="2500" max="7500" step="100" oninput="$('wbv').innerText=this.value"> <span id="wbv"></span><br>
 <button onclick="camset(false)">应用</button><button onclick="camset(true)">应用并保存</button><pre id="camst"></pre></div>
<div class="pane" id="p_col"><p>选颜色, 点击画面里该颜色的货物 (每色 2~5 点, 不同位置), 全部点完后「生成」, 自动写 classes.json 的 hsv 区间并立即生效。
<div class="dim">颜色冲突处理 (Scan 表格里「颜色冲突」列, 画面里洋红框 + 「冲突」字样): <b>按 HSV</b> = 接受 HSV 判定 (本类本次会话生效);
<b>去颜色页重新采样</b> = 跳本页并预选该类名色, 重新点几处「生成颜色区间」; <b>本类强制颜色</b> = 把 HSV 判定的颜色写进 classes.json 该类的 color (唯一写 color 的路径, 写前备份), 之后该类不再投票。详见 vision/GUIDE.md。</div></p>
 <select id="cname"><option>red</option><option>orange</option><option>yellow</option><option>green</option><option>light_blue</option><option>blue</option><option>purple</option><option>white</option><option>black</option><option>gray</option></select>
 <label><input type="checkbox" id="colmode"> 采样模式</label> <button onclick="colsolve()">生成颜色区间</button><button onclick="colclear()">清空</button><pre id="cols"></pre>
 <div class="dim">托盘白基准归一化 (A6, <b>默认关</b>, <b>[现场待验证]</b>): 每次 scan 从托盘内框取「低饱和 + 高亮度 + 不在检测框内」的像素, 自算 BGR 三通道增益 (限幅 0.7~1.4), <b>只用于颜色投票前的裁剪图</b>, 送模型的那张图不动。现场光线偏色导致颜色误判时才打开; 取样像素不足 (&lt; wb_min_pixels) 就沿用上次增益; 关掉后与旧行为完全一致。</div>
 <label><input type="checkbox" id="wbmode"> 托盘白基准</label> <button onclick="wbset(false)">仅本次</button><button onclick="wbset(true)">应用并保存</button><pre id="wb"></pre></div>
<div class="pane" id="p_scr"><p>按最近一次 Scan 置信度最高的货物渲染, 与比赛大屏完全相同。</p><img id="scrimg" style="width:100%"></div>
<div class="pane" id="p_run"><div id="runs"></div><div id="rundetail"></div></div>
</div>
<script>
let M='view', roi=[], ARM=false;
const $=id=>document.getElementById(id);
async function J(u,o){let r=await fetch(u,o);return r.json()}
function post(u,b){return J(u,{method:'POST',body:JSON.stringify(b||{})})}
function tab(t){for(let k of ['st','cal','tray','dist','cam','col','scr','run']){$('p_'+k).className='pane'+(k==t?' on':'');$('t_'+k).className=k==t?'on':''}
 if(t=='cam')camget();if(t=='scr')$('scrimg').src='/screen.png?'+Date.now();if(t=='run')runs();if(t=='col'){collist();wbget()}if(t=='tray')trget();if(t=='dist')chget()}
let TR={};
async function trget(){let d=await J('/api/tray');TR=d.cfg;$('tr_en').checked=!!d.cfg.enabled;$('tr_roi').checked=d.cfg.auto_roi!==false;
 $('tr_w').value=d.cfg.inner_w_mm||'';$('tr_h').value=d.cfg.inner_h_mm||'';$('tr_rim').value=d.cfg.rim_mm||0;$('tr_rh').value=d.cfg.rim_height_mm||0;$('tr_ch').value=d.cam_height_mm||0;
 $('tr_ps').value=d.cfg.perspective||'auto';$('tr_f').value=d.cfg.focal_px||'';
 $('fxst').innerText=d.cfg.fixed_px&&Object.keys(d.cfg.fixed_px).length?('已固定: '+JSON.stringify(d.cfg.fixed_px.corners||d.cfg.fixed_px)):'未固定 (自动检测)';
 let ax=d.cfg.axes||['tray_x','tray_y'];$('tr_ax').value=ax[0];$('tr_ay').value=ax[1];trrefs();$('trst').innerText=JSON.stringify(d.status,null,1)}
function trrefs(){$('tr_refs').innerHTML=(TR.ref_points||[]).map((r,i)=>`${i+1}: 托盘(${r.tray_mm}) → 机械臂(${r.robot_mm}) ${r.src||''} <a href="#" onclick="TR.ref_points.splice(${i},1);trrefs();return false">删</a>`).join('<br>')||'(无)'}
function trref(){let t=$('tr_rt').value.split(/[ ,，]+/).map(Number),r=$('tr_rr').value.split(/[ ,，]+/).map(Number);if(t.length<2||r.length<2||t.concat(r).some(isNaN)){alert('格式: x,y');return}
 TR.ref_points=(TR.ref_points||[]).concat([{tray_mm:t.slice(0,2),robot_mm:r.slice(0,2),src:'manual'}]);trrefs()}
async function trsave(){let d=await post('/api/tray',{enabled:$('tr_en').checked,auto_roi:$('tr_roi').checked,inner_w_mm:$('tr_w').value,inner_h_mm:$('tr_h').value,rim_mm:$('tr_rim').value,
 rim_height_mm:$('tr_rh').value,cam_height_mm:$('tr_ch').value,perspective:$('tr_ps').value,focal_px:$('tr_f').value,axes:[$('tr_ax').value,$('tr_ay').value],ref_points:TR.ref_points||[]});$('trst').innerText=JSON.stringify(d,null,1)}
async function trdet(){let d=await post('/api/tray/redetect');$('trst').innerText=JSON.stringify(d,null,1)}
async function trk1(save){if(save&&!confirm('用托盘直边估计的畸变覆盖当前内参? (已用棋盘格标定的话不要覆盖)'))return;let d=await post('/api/tray/k1',{save});$('trst').innerText=JSON.stringify(d,null,1)}
async function chget(){let d=await J('/api/chess');$('ch_c').value=d.cols;$('ch_r').value=d.rows;$('ch_s').value=d.square;$('undv').checked=d.view_undist;$('chst').innerText=`已拍 ${d.n} 张  当前${d.undistort?'已有':'没有'}内参`}
async function chsnap(){let d=await post('/api/chess/snap',{cols:+$('ch_c').value,rows:+$('ch_r').value,square:+$('ch_s').value});$('chst').innerText=d.ok?`已保存, 共 ${d.n} 张`:d.error}
async function chsolve(){$('chst').innerText='计算中...';let d=await post('/api/chess/solve');$('chst').innerText=JSON.stringify(d,null,1)}
async function done(add){$('dost').innerText='拍照计算中...';let d=await post('/api/dist/oneshot',{add});
 let t=d.ok?`✔ 已写入: k1=${d.k1} k2=${d.k2}, 共 ${d.views} 张\n格线拟合误差 ${d.before_px}px → ${d.after_px}px, 画面边角最大修正 ${d.max_shift_px}px`:'✘ '+(d.error||JSON.stringify(d));
 if(d.tip)t+='\n提示: '+d.tip; if(d.ok){$('undv').checked=true;t+='\n已切到去畸变画面; 托盘模式会自动重新定位托盘'}$('dost').innerText=t}
async function dundo(){if(!confirm('恢复上一次的内参?'))return;let d=await post('/api/dist/undo');$('dost').innerText=d.ok?'已恢复: '+d.source:'✘ '+d.error}
async function chclear(){if(!confirm('删除已拍的棋盘格图片?'))return;let d=await post('/api/chess/clear');chget()}
let FX=[];
async function fxoff(){if(!confirm('取消固定托盘, 恢复自动检测?'))return;let d=await post('/api/tray/fixed',{off:true});$('trst').innerText=JSON.stringify(d,null,1);trget()}
function mode(m){M=(M==m)?'view':m;roi=[];FX=[];$('bc').className=M=='calib'?'on':'';$('br').className=M=='roi'?'on':'';$('bfx').className=M=='corners'?'on':'';
 if(M=='corners')$('msg').innerText='依次点托盘内框的 4 个角 (顺序随意)';
 $('extra').innerHTML=M=='calib'?'<button onclick="csolve()">求解并保存</button><button onclick="cclear()">清空</button>':
 M=='roi'?'<button onclick="rsave()">保存 ROI</button><button onclick="rclear()">清除 ROI</button>':'';refresh()}
$('v').onclick=async e=>{let r=$('v').getBoundingClientRect();let k=$('v').naturalWidth/r.width;
 let u=Math.round((e.clientX-r.left)*k),v=Math.round((e.clientY-r.top)*k);
 if($('colmode').checked){let d=await post('/api/color/add',{u,v,color:$('cname').value});collist();$('msg').innerText=`采样 ${$('cname').value}: H=${d.h} S=${d.s} V=${d.v}`;return}
 if(M=='calib'){let a=await J('/api/arm');let def=a.ok?(a.x_mm+','+a.y_mm):'';
  let s=prompt(`像素(${u},${v}) 对应的机械臂坐标 x,y (mm)`+(TRAYMODE?' [托盘模式: 作为参考点]':'')+(a.ok?' [已读取机械臂当前位置]':''),def);if(!s)return;
  let [x,y]=s.split(/[ ,，]+/).map(Number);await post('/api/calib/add',{u,v,x,y});refresh();return}
 if(M=='corners'){FX.push([u,v]);$('msg').innerText=`角点 ${FX.length}/4: `+JSON.stringify(FX);
  if(FX.length==4){let d=await post('/api/tray/fixed',{corners:FX,snap:$('fx_snap').checked,z:$('fx_z').value});mode('corners');
   $('msg').innerText=d.ok?('已固定托盘四角 '+(d.snap||'')+' '+((d.status.perspective||{}).msg||'')):'失败: '+d.error;$('trst').innerText=JSON.stringify(d,null,1);trget()}return}
 if(M=='roi'){roi.push([u,v]);$('msg').innerText='ROI 点: '+JSON.stringify(roi);return}
 let hh=+($('gh').value||0);
 let d=await J('/api/pixel?u='+u+'&v='+v+'&h='+hh);let hsv=await J('/api/hsv?u='+u+'&v='+v);
 $('msg').innerText=`像素(${u},${v})${hh>0?` @高${hh}mm`:''} -> 机械臂(${d.x_mm}, ${d.y_mm}) mm   HSV=(${hsv.h},${hsv.s},${hsv.v})`;
 if($('goto').checked){if(!confirm(`机械臂开到 (${d.x_mm}, ${d.y_mm}) 上方? 确认周围无障碍`))return;$('msg').innerText+='  移动中...';
  let g=await post('/api/goto',{u,v,h:hh});$('msg').innerText=g.ok?`到位: 目标(${g.target}) 实际(${g.reached}) 偏差 ${g.err_mm} mm — 看夹爪是否正对货物中心`:'失败: '+g.error}}
async function scan(){$('msg').innerText='扫描中...';let d=await post('/api/scan');
 $('msg').innerText=JSON.stringify(d.stat);let h='<table><tr><th>#</th><th>名称</th><th>类别</th><th>颜色</th><th>置信</th><th>像素</th><th>mm</th><th>帧</th><th>颜色冲突</th></tr>';
 for(let t of d.targets){let g=t.diag||{},cf=g.color_conflict;
  h+=`<tr${cf?' style="background:#3a1030"':''}><td>${t.id}</td><td>${t.label}</td><td>${t.class}</td><td>${t.color}</td><td>${t.conf}</td><td>${t.u},${t.v}</td><td>${t.x_mm},${t.y_mm}</td><td>${t.hits}</td>`;
  h+=cf?`<td><span class=bad>HSV=${g.hsv_color} / 类名=${g.model_color}</span><br><button onclick="accHSV('${t.class}','${t.shape}','${g.model_color}','${g.hsv_color}')">按 HSV</button><button onclick="goColor('${g.model_color}')">去颜色页重新采样</button><button onclick="forceColor('${t.class}','${g.hsv_color}')">本类强制颜色</button></td>`:'<td>-</td>';
  h+='</tr>'}
 $('tb').innerHTML=h+'</table>'}
async function accHSV(cls,shape,mc,hc){let d=await post('/api/color/accept_hsv',{class_name:cls,shape:shape,model_color:mc,hsv_color:hc});
 $('msg').innerText=d.ok?`已接受 HSV=${hc} (本类/本次会话), key=${d.key}`:('失败: '+d.error)}
function goColor(mc){tab('col');let s=$('cname');if(mc){for(let o of s.options)if(o.value==mc)s.value=mc}
 $('msg').innerText='颜色页: 点画面里该颜色的货物重新采样 (2~5 点), 再点「生成颜色区间」'}
async function forceColor(cls,c){if(!confirm(`把 ${cls} 的 color 强制写成 ${c}?\n会写 classes.json (先备份 .bak), 之后该类不再走 HSV 投票。`))return;
 let d=await post('/api/color/force_class',{class_name:cls,color:c});$('msg').innerText=d.ok?`已写入 ${d.saved}: ${cls}.color=${d.color}`:('失败: '+d.error)}
async function snap(){let d=await post('/api/snap');$('msg').innerText='已保存 '+d.path}
async function setconf(v){$('cv').innerText=(v/100).toFixed(2);await J('/api/conf?value='+(v/100))}
async function csolve(){let d=await post('/api/calib/solve');$('msg').innerText=JSON.stringify(d);refresh()}
async function cclear(){await post('/api/calib/clear');refresh()}
async function rsave(){let d=await post('/api/roi',{points:roi});$('msg').innerText=JSON.stringify(d);roi=[]}
async function rclear(){let d=await post('/api/roi',{points:[]});$('msg').innerText=JSON.stringify(d);roi=[]}
async function camget(){let c=await J('/api/camera');for(let k of [['exp','exposure_us'],['gain','gain_db'],['wb','wb_kelvin']]){if(c[k[1]]!=null){$(k[0]).value=c[k[1]];$(k[0]+'v').innerText=c[k[1]]}else $(k[0]+'v').innerText='自动'}
 $('camst').innerText=JSON.stringify(c,null,1)}
async function camset(save){let d=await post('/api/camera',{exposure_us:+$('exp').value,gain_db:+$('gain').value,wb_kelvin:+$('wb').value,save});$('camst').innerText=JSON.stringify(d,null,1)}
async function collist(){let d=await J('/api/color/list');$('cols').innerText=d.samples.map(s=>`${s.color}: H${s.h} S${s.s} V${s.v}`).join('\n')||'(无采样)'}
async function colsolve(){let d=await post('/api/color/solve');$('cols').innerText=JSON.stringify(d,null,1)}
async function colclear(){await post('/api/color/clear');collist()}
async function wbget(){let d=await J('/api/wb');$('wbmode').checked=!!d.enabled;$('wb').innerText='enabled='+!!d.enabled+'  gain='+JSON.stringify(d.gain)+'  reason='+d.reason+'  pixels='+d.pixels+'  限幅='+d.gain_min+'~'+d.gain_max+(d.means?'  白斑均值='+JSON.stringify(d.means):'')}
async function wbset(save){let d=await post('/api/wb',{enabled:$('wbmode').checked,save});$('wb').innerText=JSON.stringify(d,null,1);wbget()}
async function runs(){let d=await J('/api/runs');$('runs').innerHTML='<table><tr><th>时间</th><th>结果</th><th>件数</th><th>用时</th><th>告警</th></tr>'+
 d.runs.map(r=>`<tr onclick="run('${r.id}')" style="cursor:pointer"><td>${r.start}</td><td>${r.result}</td><td>${r.total_done}</td><td>${r.elapsed_s}s</td><td>${r.warnings}</td></tr>`).join('')+'</table>'}
async function run(id){let r=await J('/api/run?id='+id);let h=`<h4>${id} ${r.result} ${r.total_done} 件 ${r.elapsed_s}s</h4>`+(r.warnings||[]).map(w=>`<div class=bad>${w}</div>`).join('');
 h+='<table><tr><th>#</th><th>货物</th><th>盒</th><th>结果</th><th>截图</th><th>识别画面</th><th></th></tr>'+r.items.map(i=>`<tr><td>${i.seq||''}</td><td>${i.label} ${(i.conf||0).toFixed(2)}</td><td>${i.slot}</td><td class="${i.result=='ok'?'ok':'bad'}">${i.result}</td>
 <td>${i.shot?`<img class=thumb style="width:70px" src="/runs/${id}/${i.shot}">`:''}</td><td>${i.scan?`<img class=thumb src="/runs/${id}/${i.scan}" onclick="window.open(this.src)">`:''}</td>
 <td>${i.scan?`<button onclick="arch('${id}','${i.scan}')">归档</button>`:''}</td></tr>`).join('')+'</table>';
 h+='<pre>'+(r.events||[]).map(e=>`[${e.t}] ${e.msg}`).join('\n')+'</pre>';$('rundetail').innerHTML=h}
async function arch(id,scan){let note=prompt('备注 (哪里识别错了)','');if(note===null)return;let d=await post('/api/regress/add',{run:id,scan,note});$('msg').innerText=d.ok?'已归档到 '+d.path+' (换模型后: vision_tool.py regress)':d.error}
let TRAYMODE=false;
async function refresh(){let s=await J('/api/status');ARM=s.arm;TRAYMODE=s.tray_mode;$('gotobox').style.display=ARM?'':'none';
 let p=s.problems.length?'<span class=bad>'+s.problems.join('\n')+'</span>':'<span class=ok>无问题</span>';
 if(s.hints&&s.hints.length)p+='\n<span class=hint>提示: '+s.hints.join('\n')+'</span>';
 $('st').innerHTML=`相机: ${s.camera}\nFPS: ${s.fps}  相机FPS: ${s.camera_fps}\n引擎: ${s.engine}\n标定: ${s.calib}${s.tray_mode?`\n托盘: ${s.tray.ok?'已定位':'未定位'} ${s.tray.msg||''} 参考点 ${s.tray.ref_points||0} 个 误差 ${JSON.stringify(s.tray.per_point_mm||[])} mm`:''}\n下位机: ${s.arm?'已连接 (--arm)':'未连接'}\n${p}`;
 let c=await J('/api/calib/list');$('cp').innerHTML=c.points.map((q,i)=>`${i+1}: 像素(${q.u},${q.v}) -> (${q.x},${q.y})`).join('<br>')||'(无)'}
setInterval(refresh,2500);refresh();
</script></body></html>"""


def _competition_running() -> bool:
    try:
        pid = int(open("/tmp/is_competition.lock").read().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _load_json(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def cmd_web(args):
    if _competition_running() and not args.force:
        print("比赛程序正在运行 (比赛模式下不允许网页调试台, 避免遥控嫌疑, 也会抢相机/串口)。\n"
              "需要调试请先在执行页停止或 ./is.sh race off; 强制启动加 --force")
        sys.exit(2)
    svc = build_service(args, continuous=True)
    calib_pts = []
    color_samples = []
    arm = {"client": None, "task": 200, "lock": threading.Lock()}
    token = _web_token(args)
    from UI.run_report import RUNS_DIR, list_runs
    if args.arm:
        try:
            from communication import create_comm_client
            c = create_comm_client()
            if c.connect(None):
                arm["client"] = c
                log.info("已连接下位机: 标定时自动读取机械臂坐标, 可用「点哪去哪」")
            else:
                log.warning("下位机连接失败, 标定坐标需手动输入")
        except Exception as e:
            log.warning("下位机不可用: %s", e)

    # ---- 统一网页附加章节: 通讯调试 / 流程调试 / 完整过程调试 (webui 包) ----
    _wpages = None
    _webui_comm = None
    _webui_flow = None
    try:
        from webui import pages as _wpages
        from webui.comm_debug import CommDebug
        from webui.flow_debug import FlowDebug
        from communication import create_comm_client as _mk_client
        try:
            _shared_client = _mk_client()
        except Exception:
            _shared_client = None
        # A2: 把 calibrator 交给 /comm 页, MOVE 才能在发送前用 vision/limits.py 的同一检查函数拦越界
        _webui_comm = CommDebug(_shared_client, calibrator=svc.calibrator)
        _webui_flow = FlowDebug(svc=svc, client=_shared_client, target_to_dict=t2d)
        log.info("调试台附加章节已就绪: 流程 / 通讯 / 完整过程")
    except Exception as e:
        log.warning("调试台附加章节不可用: %s", e)

    def goto(u, v, h=0.0):
        c = arm["client"]
        if c is None:
            return {"ok": False, "error": "未连接下位机 (启动时加 --arm)"}
        if not arm["lock"].acquire(blocking=False):    # 并发请求不能穿透: 一次只允许一个运动
            return {"ok": False, "error": "上一次移动还没结束"}
        try:
            # h = 点击处离托盘底面的高度。0 只验平面标定; 填实际抓取高度 (货物高 × parallax_factor)
            # 才会走透视位姿的高度投影, 这时夹爪对准的才是货物顶面中心而不是底面投影点。
            x, y = svc.calibrator.pixel_to_robot(float(u), float(v), float(h or 0.0))
            # A2/A3: 发送前越界拦截 —— 与 scan() 的过滤、调试台 /comm MOVE 共用同一个检查函数
            # (vision/limits.py::check_point, 闭区间, 唯一硬拦截项 robot_limits_mm)。
            # 越界就不发 send_move (物理上不会走到下面), 页面用 g.error 显示中文原因与三套坐标。
            verdict = svc.calibrator.check_robot_xy(x, y)
            if not verdict.ok:
                return {"ok": False, "target": [round(x, 1), round(y, 1)],
                        "error": "机械臂限位拦截 (指令未下发): %s | 触发=%s 轴=%s | %s"
                                 % (verdict.reason, verdict.trigger or "-",
                                    "/".join(verdict.axes) or "-", verdict.text()),
                        "limit": verdict.as_dict()}
            arm["task"] = 200 + (arm["task"] - 199) % 50
            tid = arm["task"]
            try:
                c.send_clear()
            except Exception:
                pass
            if not c.send_move(tid, x, y, timeout_ms=10000):
                return {"ok": False, "error": "运动指令无 ACK"}
            t0 = time.time()
            st = None
            while time.time() - t0 < 9.0:
                st = c.latest_status
                if st is not None and (st.is_permit_required or st.is_estop):
                    break
                time.sleep(0.05)
            reached = st is not None and st.is_permit_required
            pos = (round(st.cur_x_mm, 1), round(st.cur_y_mm, 1)) if st is not None else (None, None)
            time.sleep(0.8)                      # 停 0.8s 便于肉眼看夹爪是否正对货物
            c.send_cancel(tid)                   # 不发 PERMIT: 不下潜不夹取, 直接取消
            if not reached:
                return {"ok": False, "error": "9 秒内未到达取件位 (或急停)"}
            err = round(float(np.hypot(pos[0] - x, pos[1] - y)), 2) if pos[0] is not None else None
            return {"ok": True, "target": [round(x, 1), round(y, 1)], "reached": list(pos), "err_mm": err}
        finally:
            arm["lock"].release()

    def wb_get():
        """A6: 托盘白基准状态 (只读; 调试台显示当前增益与取样情况)。"""
        det = getattr(svc, "detector", None)
        info = det.wb_info() if hasattr(det, "wb_info") else {}
        out = dict(info)
        out["saved"] = bool((svc.cfg.color or {}).get("tray_white_balance", False))
        out["tip"] = ("[现场待验证] 默认关; 只在现场光线偏色导致颜色误判时才打开, "
                      "并在同一光照/曝光下对比 Scan 结果")
        return out

    def wb_set(body):
        """A6: 托盘白基准对比开关 (可开/关)。enabled 对本次会话立即生效;
        save=true 时把 color.tray_white_balance 写回 vision_config.json。"""
        col = svc.cfg.color
        if body.get("enabled") is not None:
            col["tray_white_balance"] = bool(body.get("enabled"))
        det = getattr(svc, "detector", None)
        if det is not None and hasattr(det, "wb_enabled"):
            det.wb_enabled = bool(col.get("tray_white_balance", False))
            if not det.wb_enabled:                 # 关掉后别再用旧增益
                det._wb_gain = None
                det._wb_info["reason"] = "disabled"
                det._wb_info["gain"] = None
        saved = ""
        if body.get("save"):
            from vision.config import DEFAULT_CONFIG_PATH
            path = os.environ.get("IS_VISION_CONFIG") or DEFAULT_CONFIG_PATH
            data = _load_json(path, {})
            data.setdefault("color", {})["tray_white_balance"] = bool(col.get("tray_white_balance", False))
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            saved = path
        out = wb_get()
        out["saved"] = saved or out["saved"]
        return out

    def camera_get():
        cfg = svc._cam_cfg
        cam = svc.camera
        return {"type": cfg.get("type"), "exposure_us": cfg.get("exposure_us"), "gain_db": cfg.get("gain_db"),
                "wb_kelvin": cfg.get("wb_kelvin"), "desc": cam.describe() if cam else "未打开"}

    def camera_set(body):
        from vision.cameras import apply_controls
        vals = {k: body[k] for k in ("exposure_us", "gain_db", "wb_kelvin") if body.get(k) is not None}
        cam = svc.camera
        res = apply_controls(cam, vals) if cam else {}
        svc._cam_cfg.update(vals)
        saved = ""
        if body.get("save"):
            from vision.config import DEFAULT_CONFIG_PATH
            p = os.environ.get("IS_VISION_CONFIG") or DEFAULT_CONFIG_PATH
            data = _load_json(p, {})
            data.setdefault("camera", {}).update(vals)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            saved = p
        return {"applied": res, "saved": saved, "tip": "不支持的项说明该相机/驱动不能手动控制 (UVC 需关自动曝光)"}

    def color_solve():
        from vision.color_calib import solve_hsv
        meta_path = getattr(svc.detector, "meta_path", os.path.join(ROOT, "vision", "classes.json"))
        data = _load_json(meta_path, {})
        try:
            new = solve_hsv(color_samples, data.get("hsv", {}))
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        # A3 门禁 color_imaging: 记下这套 HSV 区间是在什么成像条件下采的。指纹与 vision/gates.py
        # 的 imaging_fp() 是同一个函数 (曝光/增益/白平衡/分辨率/device/fourcc/swap_rb), 保证写读一致;
        # 之后换照明、自动曝光漂了、或分辨率变了, 门禁立刻判"颜色标定与当前成像不一致"。
        try:
            from vision import gates as _gates
            cam = dict(getattr(svc, "_cam_cfg", None) or getattr(svc.cfg, "camera", {}) or {})
            new["imaging_fp"] = _gates.imaging_fp(cam)
        except Exception as e:                    # 指纹算不出来不能挡住颜色标定本身
            log.warning("imaging_fp 计算失败 (颜色区间仍会保存): %s", e)
        if os.path.exists(meta_path):
            import shutil
            shutil.copy(meta_path, meta_path + ".bak")
        data["hsv"] = new
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if hasattr(svc.detector, "hsv_cfg"):
            svc.detector.hsv_cfg = new
        return {"ok": True, "hsv": new, "saved": meta_path}

    def color_force_class(body):
        """A1 调试台「本类强制颜色」: 把 classes.json 里该类的 color 写死。

        这是唯一允许写 classes.json `color` 字段的路径, 写前备份 .bak 且其余键原样保留。
        该类 color 非 null 后 -> detector 不再对它走 HSV 投票 (人为强制优先)。
        """
        cls = str(body.get("class_name") or "")
        col = str(body.get("color") or "")
        if not cls or not col:
            return {"ok": False, "error": "需要 class_name 与 color"}
        meta_path = getattr(svc.detector, "meta_path", os.path.join(ROOT, "vision", "classes.json"))
        data = _load_json(meta_path, None)
        if not isinstance(data, dict):
            return {"ok": False, "error": f"读不到 {meta_path}"}
        hit = None
        for c in (data.get("classes") or []):
            if c.get("name") == cls:
                hit = c
                break
        if hit is None:
            return {"ok": False, "error": f"classes.json 里没有类 {cls}"}
        names = list((data.get("color_names") or {}).keys())
        if names and col not in names:
            return {"ok": False, "error": f"颜色 {col} 不在 color_names: {names}"}
        if os.path.exists(meta_path):
            import shutil
            shutil.copy(meta_path, meta_path + ".bak")
        hit["color"] = col
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        cm = getattr(svc.detector, "class_meta", None)
        if isinstance(cm, dict) and cls in cm:      # 立即生效, 不必重启
            cm[cls]["color"] = col
        return {"ok": True, "saved": meta_path, "class_name": cls, "color": col}

    def color_accept_hsv(body):
        """A1 调试台「按 HSV」: 把该冲突组合记进 service 的冲突记忆 (本类/本次会话生效)。

        等价于"第二次遇见该组合自动采信 HSV", 省掉现场再抖一次料。
        """
        key = f"{body.get('shape', '')}|{body.get('model_color', '')}|{body.get('hsv_color', '')}"
        mem = getattr(svc, "_color_conflict_mem", None)
        if mem is None:
            return {"ok": False, "error": "当前视觉服务不支持冲突记忆"}
        mem[key] = time.time()
        return {"ok": True, "key": key, "entries": len(mem)}

    def screen_png():
        from UI.display_ui import render_screen
        import io as _io
        ts = svc._latest_targets
        frame = svc.get_latest_raw_frame()
        name, img_path = "等待开始", ""
        if ts and frame is not None:
            t = ts[0]
            name = t.label or t.color
            if t.bbox:
                x1, y1, x2, y2 = [int(v) for v in t.bbox]
                h, w = frame.shape[:2]
                crop = frame[max(0, y1 - 20):min(h, y2 + 20), max(0, x1 - 20):min(w, x2 + 20)]
                img_path = "/tmp/_screen_preview.jpg"
                cv2.imwrite(img_path, crop)
        buf = _io.BytesIO()
        render_screen(1 if ts else 0, name, 1 if ts else 0, img_path, "分拣中" if ts else "待机").save(buf, "PNG")
        return buf.getvalue()

    view = {"undist": False}
    chess = {"dir": os.path.join(ROOT, "tools", "chess_images"), "cols": 9, "rows": 6, "square": 20.0}

    def tray_get():
        cal = svc.calibrator
        keys = ("enabled", "inner_w_mm", "inner_h_mm", "rim_mm", "rim_height_mm", "axes", "ref_points", "auto_roi",
                "fixed_px", "fixed_z_mm", "perspective", "focal_px")
        return {"cfg": {k: cal.tray_cfg.get(k) for k in keys}, "status": cal.tray_status,
                "cam_height_mm": cal.cam_height_mm, "undistort": cal.camera_matrix is not None}

    def tray_set(body):
        cal = svc.calibrator
        upd = {k: body[k] for k in ("enabled", "inner_w_mm", "inner_h_mm", "rim_mm", "rim_height_mm",
                                    "axes", "ref_points", "auto_roi", "perspective", "focal_px") if k in body}
        if "focal_px" in upd:
            try:
                upd["focal_px"] = float(upd["focal_px"]) if upd["focal_px"] not in ("", None) else None
            except ValueError:
                upd["focal_px"] = None
        for k in ("inner_w_mm", "inner_h_mm", "rim_mm", "rim_height_mm"):
            if k in upd:
                upd[k] = float(upd[k] or 0)
        if "cam_height_mm" in body:
            cal.cam_height_mm = float(body["cam_height_mm"] or 0)
        try:
            st = cal.configure_tray(upd)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        fr = svc.get_latest_raw_frame()
        if fr is not None and cal.tray_mode:
            with svc._detect_lock:
                svc._update_tray(svc.get_fresh_frames(3, 2.0) or [fr])
        cal.save_config()
        return {"ok": True, "status": cal.tray_status, "saved": cal.config_path}

    def tray_fixed(body):
        """网页点四角 -> tray.fixed_px.corners (可选吸附到白色边缘); off=True 取消固定。"""
        from vision.tray import order_corners, snap_corners
        cal = svc.calibrator
        if body.get("off"):
            st = cal.configure_tray({"fixed_px": {}})
            cal.save_config()
            return {"ok": True, "status": st}
        if not (cal.tray_cfg.get("inner_w_mm") and cal.tray_cfg.get("inner_h_mm")):
            return {"ok": False, "error": "先填内框 W/H 并保存"}
        pts = body.get("corners") or []
        if len(pts) != 4:
            return {"ok": False, "error": "需要 4 个角点"}
        q = order_corners(pts)
        fr = svc.get_latest_raw_frame()
        if fr is None:
            return {"ok": False, "error": "无画面"}
        h, w = fr.shape[:2]
        snap_msg, snap_ok = "", False
        if body.get("snap"):
            undist = (lambda p: cal._undistort(p)) if cal.camera_matrix is not None else None
            r = snap_corners(fr, q, cal.tray_cfg, undist)
            q, snap_msg, snap_ok = np.float32(r["corners"]), r["msg"], bool(r["ok"])
        z = body.get("z", 0)
        rim_h = float(cal.tray_cfg.get("rim_height_mm") or 0)
        if snap_ok:
            # 白色区域的边界 = 壁顶内沿 (远端能看到内壁, 近端被壁顶挡住), 所以吸附后的四角在壁顶高度
            z = "rim"
            snap_msg += f"; 吸附到的是壁顶内沿, 按壁高 {rim_h} mm 计算" + ("" if rim_h > 0 else " (请填壁高!)")
        z_mm = rim_h if z == "rim" else float(z or 0)
        st = cal.configure_tray({"enabled": True, "fixed_z_mm": z_mm,
                                 "fixed_px": {"corners": np.round(q, 1).tolist(), "image_size": [w, h]}})
        with svc._detect_lock:
            svc._update_tray([fr])
        cal.save_config()
        return {"ok": True, "status": cal.tray_status, "snap": snap_msg, "corners": np.round(q, 1).tolist()}

    def tray_redetect():
        cal = svc.calibrator
        if not cal.tray_mode:
            return {"ok": False, "error": "托盘模式未开启 (先填内框尺寸并勾选启用)"}
        cal.tray_tracker.hist.clear()
        cal.tray_tracker.locked_live = False
        with svc._detect_lock:
            svc._update_tray(svc.get_fresh_frames(5, 3.0))
        cal.save_config()
        return {"ok": bool(cal.tray_status.get("ok")), "status": cal.tray_status}

    def tray_k1(save):
        from vision.tray import estimate_k1
        cal = svc.calibrator
        fr = svc.get_latest_raw_frame()
        if fr is None:
            return {"ok": False, "error": "无画面"}
        r = estimate_k1(fr, cal.tray_cfg)
        if r.get("ok") and save:
            cal.set_intrinsics(r["camera_matrix"], r["dist_coeffs"], persist=True)
            r["saved"] = cal.config_path
        return r

    def chess_snap(body):
        from tools.calib_intrinsics import find_corners
        chess.update({k: type(chess[k])(body[k]) for k in ("cols", "rows", "square") if body.get(k)})
        fr = svc.get_fresh_frames(1, 2.0)
        if not fr:
            return {"ok": False, "error": "无画面"}
        g = cv2.cvtColor(fr[0], cv2.COLOR_BGR2GRAY)
        c = find_corners(g, chess["cols"], chess["rows"])
        if c is None:
            return {"ok": False, "error": f"没找到 {chess['cols']}x{chess['rows']} 内角点棋盘格 (整块进画面, 别反光)"}
        os.makedirs(chess["dir"], exist_ok=True)
        p = os.path.join(chess["dir"], time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time()*1000)%1000:03d}.png")
        cv2.imwrite(p, fr[0])
        chess["last"] = c.reshape(-1, 2).tolist()
        return {"ok": True, "n": len(glob.glob(os.path.join(chess["dir"], "*.png")))}

    def chess_solve():
        from tools.calib_intrinsics import calibrate
        files = sorted(glob.glob(os.path.join(chess["dir"], "*.png")))
        r = calibrate(files, chess["cols"], chess["rows"], chess["square"], svc.calibrator.config_path)
        if r.get("ok"):
            svc.calibrator.set_intrinsics(r["camera_matrix"], r["dist_coeffs"], source="chessboard")
        return r

    oneshot = {"views": [], "size": None}

    def dist_oneshot(body):
        from tools.calib_intrinsics import detect_board, calibrate_views, write_intrinsics
        frs = svc.get_fresh_frames(5, 3.0)
        if not frs:
            return {"ok": False, "error": "无画面"}
        g = np.mean([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frs], 0)
        g = np.clip(g, 0, 255).astype(np.uint8)
        c, cr = detect_board(g)
        if c is None:
            return {"ok": False, "error": "没认出棋盘格: 整张纸要在画面里、压平、别反光; 太小就把纸放近/换大方格"}
        size = (g.shape[1], g.shape[0])
        if not body.get("add") or oneshot["size"] != size:
            oneshot["views"] = []
        oneshot["size"] = size
        oneshot["views"].append((c, cr))
        chess["last"] = c.tolist()
        r = calibrate_views(oneshot["views"], size)
        r["board"] = f"{cr[0]}x{cr[1]}"
        if r.get("ok"):
            cal = svc.calibrator
            write_intrinsics(r["camera_matrix"], r["dist_coeffs"], cal.config_path,
                             f"one-shot k1={r['k1']} k2={r['k2']} views={r['views']}")
            cal.set_intrinsics(r["camera_matrix"], r["dist_coeffs"])
            view["undist"] = True
            if cal.tray_mode:
                with svc._detect_lock:
                    svc._update_tray(svc.get_fresh_frames(3, 2.0))
            r["saved"] = cal.config_path
        r.pop("camera_matrix", None)
        r.pop("dist_coeffs", None)
        return r

    def dist_undo():
        cal = svc.calibrator
        data = _load_json(cal.config_path, {})
        prev = data.get("prev_intrinsics")
        if prev is None:
            if data.get("camera_matrix") is None:
                return {"ok": False, "error": "没有可恢复的内参"}
            cal.set_intrinsics(None, None, persist=True)
            src = "无内参 (不做去畸变)"
        else:
            cal.set_intrinsics(prev.get("camera_matrix"), prev.get("dist_coeffs"), persist=True,
                              source=prev.get("source") or "")
            src = prev.get("source") or "上一次内参"
        data = _load_json(cal.config_path, {})
        data.pop("prev_intrinsics", None)
        data["intrinsics_source"] = src
        with open(cal.config_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        if cal.tray_mode:
            with svc._detect_lock:
                svc._update_tray(svc.get_fresh_frames(3, 2.0))
        return {"ok": True, "source": src}

    def chess_clear():
        for f in glob.glob(os.path.join(chess["dir"], "*.png")):
            os.remove(f)
        chess.pop("last", None)
        return {"ok": True, "n": 0}

    def regress_add(body):
        import shutil
        run, scan = os.path.basename(body.get("run", "")), os.path.basename(body.get("scan", ""))
        src = os.path.join(RUNS_DIR, run, scan.replace(".jpg", "_raw.jpg"))
        if not os.path.exists(src):
            return {"ok": False, "error": f"没有原图 {src}"}
        d = os.path.join(ROOT, args.regress_dir)
        os.makedirs(d, exist_ok=True)
        base = f"{run}_{os.path.splitext(scan)[0]}"
        shutil.copy(src, os.path.join(d, base + ".jpg"))
        targets = _load_json(os.path.join(RUNS_DIR, run, scan.replace(".jpg", ".json")), [])
        with open(os.path.join(d, base + ".json"), "w", encoding="utf-8") as f:
            json.dump({"note": body.get("note", ""), "targets": targets}, f, ensure_ascii=False, indent=1)
        hard = os.path.join(ROOT, "dataset", "hard_cases")          # 待标注的难例, 下次训练加进去
        os.makedirs(hard, exist_ok=True)
        shutil.copy(src, os.path.join(hard, base + ".jpg"))
        return {"ok": True, "path": os.path.join(args.regress_dir, base + ".jpg")}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, b, ctype, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _json(self, obj, code=200):
            try:
                from webui.base import json_bytes as _jb
                self._send(_jb(obj), "application/json; charset=utf-8", code)
            except Exception:
                self._send(json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8", code)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            b = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if not isinstance(b, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return b

        def _authed(self) -> bool:
            if not token:
                return True
            if self.headers.get("X-Token", "") == token:
                return True
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "is_token" and v == token:
                    return True
            return False

        def _guard(self, fn):
            """口令校验 + 异常兜底: 非法参数回 400, 内部错误回 500, 不让一个坏请求拖垮调试台。"""
            try:
                if not self._authed():
                    u = urlparse(self.path)
                    q = {k: v[0] for k, v in parse_qs(u.query).items()}
                    if u.path == "/" and q.get("t") == token:
                        self.send_response(302)
                        self.send_header("Set-Cookie", f"is_token={token}; Path=/; Max-Age=2592000; SameSite=Strict")
                        self.send_header("Location", "/")
                        self.end_headers()
                    elif u.path == "/":
                        self._send(LOGIN_HTML.encode(), "text/html; charset=utf-8", 401)
                    else:
                        self._json({"ok": False, "error": "需要口令 (先打开首页输入)"}, 401)
                    return
                fn()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (ValueError, KeyError, TypeError) as e:
                log.warning("网页请求 %s 参数错误: %s", self.path, e)
                self._safe_json({"ok": False, "error": f"参数错误: {e}"}, 400)
            except Exception as e:
                log.exception("网页请求 %s 处理异常", self.path)
                self._safe_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

        def _safe_json(self, obj, code):
            try:
                self._json(obj, code)
            except Exception:
                pass

        def do_GET(self):
            self._guard(self._get)

        def do_POST(self):
            self._guard(self._post)

        def _get(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/":
                self._send(HTML.encode(), "text/html; charset=utf-8")
            elif u.path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f")
                self.end_headers()
                try:
                    while True:
                        img, _ = svc.get_latest_frame_and_targets()
                        if img is not None:
                            cal = svc.calibrator
                            if view["undist"] and cal.camera_matrix is not None:
                                img = cv2.undistort(img, cal.camera_matrix, cal.dist_coeffs)
                            else:
                                ov = cal.tray_overlay_px() if cal.tray_mode else {}
                                if ov:
                                    good = cal.tray_status.get("detect_ok", False)
                                    cv2.polylines(img, [np.int32(ov["outer"])], True, (0, 255, 0) if good else (0, 0, 255), 1)
                                    cv2.polylines(img, [np.int32(ov["inner"])], True, (255, 255, 0), 1)
                                    o, ax, ay = [tuple(int(v) for v in p) for p in ov["axes"]]
                                    cv2.arrowedLine(img, o, ax, (0, 0, 255), 2, tipLength=0.15)
                                    cv2.arrowedLine(img, o, ay, (0, 255, 0), 2, tipLength=0.15)
                                    cv2.putText(img, "x", ax, cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
                                    cv2.putText(img, "y", ay, cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
                                    for rp in ov["refs"]:
                                        cv2.drawMarker(img, (int(rp[0]), int(rp[1])), (255, 0, 255), cv2.MARKER_TILTED_CROSS, 12, 2)
                                if chess.get("last"):
                                    for q in chess["last"]:
                                        cv2.circle(img, (int(q[0]), int(q[1])), 2, (0, 200, 255), -1)
                            for p in calib_pts:
                                cv2.circle(img, (int(p["u"]), int(p["v"])), 5, (255, 0, 255), 2)
                            for s_ in color_samples:
                                cv2.circle(img, (int(s_["u"]), int(s_["v"])), 4, (0, 255, 255), 1)
                            ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
                            self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\n\r\n" + jpg.tobytes() + b"\r\n")
                        time.sleep(0.08)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            elif u.path == "/api/status":
                rep = svc.self_check(log=False)
                cal = rep["calibration"]
                self._json({"camera": rep["camera"]["desc"], "fps": round(svc.fps, 1),
                            "camera_fps": rep["camera"]["fps"], "engine": rep["engine"]["desc"],
                            "calib": f"{cal['points']} 点, 误差 {cal['reproj_error_mm']} mm, "
                                     f"{'已标定' if cal['calibrated'] else '未标定'}",
                            "problems": rep["problems"], "hints": rep.get("hints", []),
                            "last_scan": svc.last_scan,
                            "arm": arm["client"] is not None, "tray_mode": svc.calibrator.tray_mode,
                            "tray": svc.calibrator.tray_status})
            elif u.path == "/api/pixel":
                hh = float(q.get("h", ["0"])[0] or 0)
                x, y = svc.calibrator.pixel_to_robot(float(q["u"]), float(q["v"]), hh)
                self._json({"x_mm": round(x, 1), "y_mm": round(y, 1)})
            elif u.path == "/api/hsv":
                from vision.color_calib import sample_hsv
                fr = svc.get_latest_raw_frame()
                self._json(sample_hsv(fr, float(q["u"]), float(q["v"])) if fr is not None else {})
            elif u.path == "/api/conf":
                eng = getattr(svc.detector, "engine", None)
                if eng is not None:
                    eng.conf_thresh = float(q.get("value", 0.4))
                self._json({"ok": eng is not None})
            elif u.path == "/api/arm":
                c = arm["client"]
                st = c.latest_status if c else None
                self._json({"ok": st is not None, "x_mm": getattr(st, "cur_x_mm", None),
                            "y_mm": getattr(st, "cur_y_mm", None)})
            elif u.path == "/api/calib/list":
                self._json({"points": calib_pts})
            elif u.path == "/api/camera":
                self._json(camera_get())
            elif u.path == "/api/wb":
                self._json(wb_get())
            elif u.path == "/api/tray":
                self._json(tray_get())
            elif u.path == "/calib_board.pdf":
                pdf = os.path.join(ROOT, "tools", "calib_board_A4.pdf")
                if not os.path.exists(pdf):
                    from tools.calib_intrinsics import make_board_pdf
                    make_board_pdf(pdf)
                with open(pdf, "rb") as fh:
                    self._send(fh.read(), "application/pdf")
            elif u.path == "/api/chess":
                self._json({**{k: chess[k] for k in ("cols", "rows", "square")},
                            "n": len(glob.glob(os.path.join(chess["dir"], "*.png"))),
                            "undistort": svc.calibrator.camera_matrix is not None, "view_undist": view["undist"]})
            elif u.path == "/api/color/list":
                self._json({"samples": color_samples})
            elif u.path == "/screen.png":
                self._send(screen_png(), "image/png")
            elif u.path == "/api/runs":
                self._json({"runs": list_runs()})
            elif u.path == "/api/run":
                r = _load_json(os.path.join(RUNS_DIR, os.path.basename(q.get("id", "")), "report.json"), {})
                self._json(r or {"items": [], "events": []})
            elif u.path.startswith("/runs/"):
                parts = u.path.split("/")
                p = os.path.join(RUNS_DIR, os.path.basename(parts[2]), os.path.basename(parts[-1])) if len(parts) >= 4 else ""
                if p and os.path.isfile(p):
                    self._send(open(p, "rb").read(), "image/jpeg")
                else:
                    self.send_error(404)
            elif u.path in ("/static/app.css", "/static/common.js"):
                from webui.base import read_static, guess_type
                name = u.path.rsplit("/", 1)[-1]
                b = read_static(name)
                if b is None:
                    self.send_error(404)
                else:
                    self._send(b, guess_type(name))
            elif _webui_comm is not None and u.path in ("/comm", "/flow", "/run"):
                h = _wpages.page(u.path)
                if h:
                    self._send(h.encode(), "text/html; charset=utf-8")
                else:
                    self.send_error(404)
            elif _webui_comm is not None and u.path == "/api/comm/state":
                self._json(_webui_comm.snapshot())
            elif _webui_comm is not None and u.path == "/api/comm/ports":
                self._json({"ports": _webui_comm.list_ports()})
            elif _webui_flow is not None and u.path == "/api/flow/state":
                self._json(_webui_flow.snapshot())
            elif _webui_flow is not None and u.path == "/api/run/state":
                d = _webui_flow.snapshot()
                d["running"] = _webui_flow.running()
                self._json(d)
            else:
                self.send_error(404)

        def _post(self):
            u = urlparse(self.path).path
            if u == "/api/scan":
                ts, _ = svc.scan()
                self._json({"targets": [t2d(t) for t in ts], "stat": svc.last_scan})
            elif u == "/api/snap":
                fr = svc.get_fresh_frames(1, 2.0)
                os.makedirs(args.snap_dir, exist_ok=True)
                p = os.path.join(args.snap_dir, time.strftime("%Y%m%d_%H%M%S") + ".jpg")
                if fr:
                    cv2.imwrite(p, fr[0], [cv2.IMWRITE_JPEG_QUALITY, 95])
                self._json({"path": p if fr else "无图像"})
            elif u == "/api/calib/add":
                calib_pts.append(self._body())
                self._json({"n": len(calib_pts)})
            elif u == "/api/calib/clear":
                calib_pts.clear()
                self._json({"n": 0})
            elif u == "/api/calib/solve":
                self._json(solve_calib(svc, calib_pts, args.cam_height))
            elif u == "/api/roi":
                pts = self._body().get("points", [])
                save_roi(pts)
                if hasattr(svc.detector, "set_roi"):
                    svc.detector.set_roi(pts)
                self._json({"ok": True, "roi": pts})
            elif u == "/api/goto":
                b = self._body()
                self._json(goto(b.get("u", 0), b.get("v", 0), b.get("h", 0)))
            elif u == "/api/camera":
                self._json(camera_set(self._body()))
            elif u == "/api/wb":
                self._json(wb_set(self._body()))
            elif u == "/api/color/add":
                from vision.color_calib import sample_hsv
                b = self._body()
                fr = svc.get_latest_raw_frame()
                smp = sample_hsv(fr, float(b["u"]), float(b["v"])) if fr is not None else {}
                if smp:
                    smp.update(color=b.get("color", "red"), u=b["u"], v=b["v"])
                    color_samples.append(smp)
                self._json(smp)
            elif u == "/api/color/clear":
                color_samples.clear()
                self._json({"ok": True})
            elif u == "/api/color/solve":
                self._json(color_solve())
            elif u == "/api/color/force_class":
                self._json(color_force_class(self._body()))
            elif u == "/api/color/accept_hsv":
                self._json(color_accept_hsv(self._body()))
            elif u == "/api/tray":
                self._json(tray_set(self._body()))
            elif u == "/api/tray/fixed":
                self._json(tray_fixed(self._body()))
            elif u == "/api/tray/redetect":
                self._json(tray_redetect())
            elif u == "/api/tray/k1":
                self._json(tray_k1(bool(self._body().get("save"))))
            elif u == "/api/chess/snap":
                self._json(chess_snap(self._body()))
            elif u == "/api/chess/solve":
                self._json(chess_solve())
            elif u == "/api/dist/oneshot":
                self._json(dist_oneshot(self._body()))
            elif u == "/api/dist/undo":
                self._json(dist_undo())
            elif u == "/api/chess/clear":
                self._json(chess_clear())
            elif u == "/api/view":
                view["undist"] = bool(self._body().get("undist"))
                self._json(view)
            elif u == "/api/regress/add":
                self._json(regress_add(self._body()))
            elif _webui_comm is not None and u == "/api/comm/connect":
                self._json(_webui_comm.connect(self._body()))
            elif _webui_comm is not None and u == "/api/comm/mock":
                self._json(_webui_comm.connect_mock())
            elif _webui_comm is not None and u == "/api/comm/disconnect":
                self._json(_webui_comm.disconnect())
            elif _webui_comm is not None and u == "/api/comm/send":
                self._json(_webui_comm.send(self._body()))
            elif _webui_flow is not None and u == "/api/flow/scan":
                self._json(_webui_flow.scan())
            elif _webui_flow is not None and u == "/api/flow/plan":
                self._json(_webui_flow.plan())
            elif _webui_flow is not None and u == "/api/flow/exec":
                self._json(_webui_flow.exec_one(self._body()))
            elif _webui_flow is not None and u == "/api/flow/shake":
                self._json(_webui_flow.shake())
            elif _webui_flow is not None and u == "/api/flow/reset":
                self._json(_webui_flow.reset())
            elif _webui_flow is not None and u == "/api/run/start":
                self._json(_webui_flow.start_full(self._body()))
            elif _webui_flow is not None and u == "/api/run/stop":
                self._json(_webui_flow.stop_full())
            elif _webui_flow is not None and u == "/api/run/estop":
                c = _webui_flow.client
                self._json({"ok": bool(c and getattr(c, "is_connected", False) and c.send_estop())}
                           if (c and getattr(c, "is_connected", False)) else {"ok": False, "error": "未连接"})
            else:
                self.send_error(404)

    srv = ThreadingHTTPServer((args.host, args.port), H)
    log.info("网页调试台: http://<本机IP>:%d  (Ctrl+C 退出)", args.port)
    if token:
        log.info("网页口令: %s   (首次打开输入一次, 浏览器记住 30 天; 或直接打开 http://<本机IP>:%d/?t=%s)",
                 token, args.port, token)
    else:
        log.warning("网页调试台未设口令 (--no-auth): 同网段任何设备都能操作, 包括 --arm 下移动机械臂")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        svc.stop()
        if arm["client"]:
            arm["client"].disconnect()


def solve_calib(svc, pts, cam_height):
    cal = svc.calibrator
    if cal.tray_mode:
        # 托盘模式: 点击的点换算成托盘坐标存为参考点 (1 点也行, >=2 点自动求旋转, >=5 点残差修正)
        if not pts:
            return {"ok": False, "error": "托盘模式: 至少点 1 个参考点 (建议 2 点以上, 9 点最好)"}
        if cal.tray_H is None:
            return {"ok": False, "error": "托盘还没定位成功, 先在「托盘」页检查"}
        if cam_height:
            cal.cam_height_mm = float(cam_height)
        st = cal.set_tray_ref_points([(p["u"], p["v"]) for p in pts], [(p["x"], p["y"]) for p in pts])
        cal.save_config()
        return {"ok": True, "mode": "tray", "ref_points": st.get("ref_points"), "per_point_mm": st.get("per_point_mm"),
                "angle_deg": st.get("angle_deg"), "mirror": st.get("mirror"), "scale": st.get("scale"),
                "warn": st.get("warn", ""), "saved": cal.config_path}
    if len(pts) < 4:
        return {"ok": False, "error": "至少 4 个点 (建议托盘内均匀 9 点)"}
    try:
        cal.set_calibration_points([(p["u"], p["v"]) for p in pts], [(p["x"], p["y"]) for p in pts])
    except Exception as e:
        return {"ok": False, "error": str(e)}
    fr = svc.get_latest_raw_frame()
    if fr is not None:
        cal.image_size = (fr.shape[1], fr.shape[0])
    if cam_height:
        cal.cam_height_mm = float(cam_height)
    cal.save_config()
    res = []
    for p in pts:
        x, y = cal.pixel_to_robot(p["u"], p["v"])
        res.append(round(float(np.hypot(x - p["x"], y - p["y"])), 2))
    return {"ok": True, "reproj_error_mm": round(cal.reproj_error_mm, 2), "per_point_mm": res,
            "saved": cal.config_path, "tip": "单点误差 > 2mm 的点建议重新点选" if max(res) > 2 else ""}


def save_roi(pts):
    from vision.config import DEFAULT_CONFIG_PATH
    p = os.environ.get("IS_VISION_CONFIG") or DEFAULT_CONFIG_PATH
    data = {}
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    data.setdefault("detect", {})["roi"] = [[int(a), int(b)] for a, b in pts]
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="视觉调试工具")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("web", "check", "capture", "bench", "regress"):
        p = sub.add_parser(name)
        p.add_argument("--camera", default="", help="uvc|hikrobot|replay|synthetic (覆盖配置)")
        p.add_argument("--replay", default="", help="回放图片目录/视频")
        p.add_argument("--model", default="", help="指定模型文件")
        p.add_argument("--backend", default="", help="yolo|hsv_color")
        if name == "web":
            p.add_argument("--port", type=int, default=8090)
            p.add_argument("--host", default=os.environ.get("IS_WEB_HOST", "0.0.0.0"),
                           help="监听地址; 笔记本浏览器访问需要 0.0.0.0, 只在板上看用 127.0.0.1")
            p.add_argument("--no-auth", action="store_true", help="关闭网页口令 (不建议)")
            p.add_argument("--arm", action="store_true", help="连接下位机, 标定时自动读取机械臂坐标")
            p.add_argument("--snap-dir", default="tools/calib_images")
            p.add_argument("--cam-height", type=float, default=0, help="镜头到托盘面高度 mm (视差校正)")
            p.add_argument("--force", action="store_true", help="比赛程序运行中也强制启动")
            p.add_argument("--regress-dir", default="dataset/regression")
        if name == "regress":
            p.add_argument("--dir", default=os.path.join(ROOT, "dataset", "regression"))
        if name == "check":
            p.add_argument("--images", default="")
            p.add_argument("-n", type=int, default=3)
        if name == "capture":
            p.add_argument("--out", default="tools/calib_images")
            p.add_argument("--interval", type=float, default=1.0)
            p.add_argument("--count", type=int, default=100)
        if name == "bench":
            p.add_argument("-n", type=int, default=50)
    a = ap.parse_args()
    if not a.cmd:
        ap.print_help()
        return
    {"web": cmd_web, "check": cmd_check, "capture": cmd_capture, "bench": cmd_bench,
     "regress": cmd_regress}[a.cmd](a)


if __name__ == "__main__":
    main()
