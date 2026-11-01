#!/usr/bin/env python3
"""
视觉调试工具 (RDK X5 上直接运行, 无需显示器: 用笔记本浏览器打开网页即可)

    python3 tools/vision_tool.py web                 # 网页调试台 http://<板子IP>:8091
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
    return {"id": t.target_id, "label": t.label, "class": t.class_name, "color": t.color,
            "conf": round(t.confidence, 3), "u": t.u_pixel, "v": t.v_pixel,
            "x_mm": t.x_mm, "y_mm": t.y_mm, "hits": t.hits, "category": t.category,
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


HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>视觉调试台</title>
<style>body{font-family:"Microsoft YaHei",sans-serif;background:#1e1e24;color:#ddd;margin:0;display:flex}
#l{padding:8px}#r{padding:8px;flex:1;font-size:13px;max-width:560px}img{border:1px solid #555}#v{cursor:crosshair}
button{margin:2px;padding:6px 10px;background:#335;color:#fff;border:0;border-radius:4px;cursor:pointer}
button.on{background:#a60}table{border-collapse:collapse}td,th{border:1px solid #444;padding:2px 5px}
pre{white-space:pre-wrap;background:#111;padding:6px;max-height:200px;overflow:auto}.bad{color:#f66}.ok{color:#3d8}
.tabs button{background:#223}.tabs button.on{background:#a60}.pane{display:none}.pane.on{display:block}
input[type=range]{width:220px}#msg{color:#fb3;min-height:18px}.thumb{width:160px;cursor:pointer}</style></head>
<body><div id="l"><img id="v" src="/stream"><br>
<button onclick="scan()">Scan (比赛同款)</button><button onclick="snap()">Snap 存原图</button>
置信度 <input type="range" min="5" max="95" value="40" id="conf" oninput="setconf(this.value)" style="width:120px"> <span id="cv">0.40</span>
<div id="msg"></div><div id="tb"></div></div>
<div id="r"><div class="tabs"><button onclick="tab('st')" id="t_st" class="on">状态</button><button onclick="tab('cal')" id="t_cal">标定/ROI</button>
<button onclick="tab('tray')" id="t_tray">托盘</button><button onclick="tab('dist')" id="t_dist">畸变</button>
<button onclick="tab('cam')" id="t_cam">相机</button><button onclick="tab('col')" id="t_col">颜色</button>
<button onclick="tab('scr')" id="t_scr">大屏</button><button onclick="tab('run')" id="t_run">复盘</button></div>
<div class="pane on" id="p_st"><pre id="st"></pre>
 <b>点击画面</b>: 显示像素对应的机械臂坐标。<span id="gotobox"><br><label><input type="checkbox" id="goto"> 点哪去哪: 机械臂开到点击位置上方停住 (不夹取), 用来验标定</label></span></div>
<div class="pane" id="p_cal"><button id="bc" onclick="mode('calib')">标定模式</button><button id="br" onclick="mode('roi')">ROI 模式</button><span id="extra"></span>
 <p>标定: 托盘内均匀放 9 个标记, 逐个点击并输入机械臂坐标 (--arm 时自动读取当前位置)。ROI: 依次点击托盘轮廓。</p><b>标定点</b><div id="cp"></div></div>
<div class="pane" id="p_tray"><p>托盘坐标系: 原点 = 画面左上方的内框角, +x 沿画面向右, +y 沿画面向下。画面里绿框 = 检测到的白色外边界 (红 = 本帧没检测到), 青框 = 推算的内框, 红/绿箭头 = 托盘 +x/+y, 紫叉 = 参考点。</p>
 <label><input type="checkbox" id="tr_en"> 启用托盘模式</label> <label><input type="checkbox" id="tr_roi"> 托盘内框自动作 ROI</label><br>
 内框 W (左右) <input id="tr_w" size="5"> mm  H (上下) <input id="tr_h" size="5"> mm<br>
 外沿 rim <input id="tr_rim" size="4"> mm (白色外边界到内框)  壁高 <input id="tr_rh" size="4"> mm  镜头高 <input id="tr_ch" size="5"> mm (到托盘底面)<br>
 机械臂 +X = <select id="tr_ax"><option>tray_x</option><option>-tray_x</option><option>tray_y</option><option>-tray_y</option></select>
 机械臂 +Y = <select id="tr_ay"><option>tray_y</option><option>-tray_y</option><option>tray_x</option><option>-tray_x</option></select> (≥2 个参考点时自动求, 此项忽略)<br>
 <b>参考点</b> (托盘坐标 → 机械臂坐标; 也可在「标定/ROI」页点击画面添加):<div id="tr_refs"></div>
 托盘 x,y <input id="tr_rt" size="9" placeholder="20,30"> 机械臂 x,y <input id="tr_rr" size="9" placeholder="150,-20"> <button onclick="trref()">加参考点</button><br>
 <button onclick="trsave()">保存并应用</button><button onclick="trdet()">重新检测托盘</button><button onclick="trk1(false)">用托盘直边估计畸变</button><button onclick="trk1(true)">估计并写入</button>
 <pre id="trst"></pre></div>
<div class="pane" id="p_dist"><p>棋盘格标定镜头畸变: 打印棋盘格贴在硬板上, 放在托盘区域, 每次换位置/倾角点「拍一张」(检测到角点才保存, 画面上出现橙点), 覆盖画面四角, 15~25 张后「计算内参」。</p>
 内角点 列 <input id="ch_c" size="2" value="9"> 行 <input id="ch_r" size="2" value="6"> 方格 <input id="ch_s" size="3" value="20"> mm<br>
 <button onclick="chsnap()">拍一张</button><button onclick="chsolve()">计算内参并写入</button><button onclick="chclear()">清空图片</button>
 <label><input type="checkbox" id="undv" onchange="post('/api/view',{undist:this.checked})"> 画面显示去畸变结果 (直线应变直)</label><pre id="chst"></pre></div>
<div class="pane" id="p_cam"><p>曝光/增益/白平衡实时生效, 满意后点保存 (写入 vision_config.json, 比赛时锁定)。</p>
 曝光 us <input type="range" id="exp" min="100" max="40000" step="100" oninput="$('expv').innerText=this.value"> <span id="expv"></span><br>
 增益 dB <input type="range" id="gain" min="0" max="24" step="0.5" oninput="$('gainv').innerText=this.value"> <span id="gainv"></span><br>
 白平衡 K <input type="range" id="wb" min="2500" max="7500" step="100" oninput="$('wbv').innerText=this.value"> <span id="wbv"></span><br>
 <button onclick="camset(false)">应用</button><button onclick="camset(true)">应用并保存</button><pre id="camst"></pre></div>
<div class="pane" id="p_col"><p>选颜色, 点击画面里该颜色的货物 (每色 2~5 点, 不同位置), 全部点完后「生成」, 自动写 classes.json 的 hsv 区间并立即生效。</p>
 <select id="cname"><option>red</option><option>orange</option><option>yellow</option><option>green</option><option>light_blue</option><option>blue</option><option>purple</option><option>white</option><option>black</option><option>gray</option></select>
 <label><input type="checkbox" id="colmode"> 采样模式</label> <button onclick="colsolve()">生成颜色区间</button><button onclick="colclear()">清空</button><pre id="cols"></pre></div>
<div class="pane" id="p_scr"><p>按最近一次 Scan 置信度最高的货物渲染, 与比赛大屏完全相同。</p><img id="scrimg" style="width:100%"></div>
<div class="pane" id="p_run"><div id="runs"></div><div id="rundetail"></div></div>
</div>
<script>
let M='view', roi=[], ARM=false;
const $=id=>document.getElementById(id);
async function J(u,o){let r=await fetch(u,o);return r.json()}
function post(u,b){return J(u,{method:'POST',body:JSON.stringify(b||{})})}
function tab(t){for(let k of ['st','cal','tray','dist','cam','col','scr','run']){$('p_'+k).className='pane'+(k==t?' on':'');$('t_'+k).className=k==t?'on':''}
 if(t=='cam')camget();if(t=='scr')$('scrimg').src='/screen.png?'+Date.now();if(t=='run')runs();if(t=='col')collist();if(t=='tray')trget();if(t=='dist')chget()}
let TR={};
async function trget(){let d=await J('/api/tray');TR=d.cfg;$('tr_en').checked=!!d.cfg.enabled;$('tr_roi').checked=d.cfg.auto_roi!==false;
 $('tr_w').value=d.cfg.inner_w_mm||'';$('tr_h').value=d.cfg.inner_h_mm||'';$('tr_rim').value=d.cfg.rim_mm||0;$('tr_rh').value=d.cfg.rim_height_mm||0;$('tr_ch').value=d.cam_height_mm||0;
 let ax=d.cfg.axes||['tray_x','tray_y'];$('tr_ax').value=ax[0];$('tr_ay').value=ax[1];trrefs();$('trst').innerText=JSON.stringify(d.status,null,1)}
function trrefs(){$('tr_refs').innerHTML=(TR.ref_points||[]).map((r,i)=>`${i+1}: 托盘(${r.tray_mm}) → 机械臂(${r.robot_mm}) ${r.src||''} <a href="#" onclick="TR.ref_points.splice(${i},1);trrefs();return false">删</a>`).join('<br>')||'(无)'}
function trref(){let t=$('tr_rt').value.split(/[ ,，]+/).map(Number),r=$('tr_rr').value.split(/[ ,，]+/).map(Number);if(t.length<2||r.length<2||t.concat(r).some(isNaN)){alert('格式: x,y');return}
 TR.ref_points=(TR.ref_points||[]).concat([{tray_mm:t.slice(0,2),robot_mm:r.slice(0,2),src:'manual'}]);trrefs()}
async function trsave(){let d=await post('/api/tray',{enabled:$('tr_en').checked,auto_roi:$('tr_roi').checked,inner_w_mm:$('tr_w').value,inner_h_mm:$('tr_h').value,rim_mm:$('tr_rim').value,
 rim_height_mm:$('tr_rh').value,cam_height_mm:$('tr_ch').value,axes:[$('tr_ax').value,$('tr_ay').value],ref_points:TR.ref_points||[]});$('trst').innerText=JSON.stringify(d,null,1)}
async function trdet(){let d=await post('/api/tray/redetect');$('trst').innerText=JSON.stringify(d,null,1)}
async function trk1(save){if(save&&!confirm('用托盘直边估计的畸变覆盖当前内参? (已用棋盘格标定的话不要覆盖)'))return;let d=await post('/api/tray/k1',{save});$('trst').innerText=JSON.stringify(d,null,1)}
async function chget(){let d=await J('/api/chess');$('ch_c').value=d.cols;$('ch_r').value=d.rows;$('ch_s').value=d.square;$('undv').checked=d.view_undist;$('chst').innerText=`已拍 ${d.n} 张  当前${d.undistort?'已有':'没有'}内参`}
async function chsnap(){let d=await post('/api/chess/snap',{cols:+$('ch_c').value,rows:+$('ch_r').value,square:+$('ch_s').value});$('chst').innerText=d.ok?`已保存, 共 ${d.n} 张`:d.error}
async function chsolve(){$('chst').innerText='计算中...';let d=await post('/api/chess/solve');$('chst').innerText=JSON.stringify(d,null,1)}
async function chclear(){if(!confirm('删除已拍的棋盘格图片?'))return;let d=await post('/api/chess/clear');chget()}
function mode(m){M=(M==m)?'view':m;roi=[];$('bc').className=M=='calib'?'on':'';$('br').className=M=='roi'?'on':'';
 $('extra').innerHTML=M=='calib'?'<button onclick="csolve()">求解并保存</button><button onclick="cclear()">清空</button>':
 M=='roi'?'<button onclick="rsave()">保存 ROI</button><button onclick="rclear()">清除 ROI</button>':'';refresh()}
$('v').onclick=async e=>{let r=$('v').getBoundingClientRect();let k=$('v').naturalWidth/r.width;
 let u=Math.round((e.clientX-r.left)*k),v=Math.round((e.clientY-r.top)*k);
 if($('colmode').checked){let d=await post('/api/color/add',{u,v,color:$('cname').value});collist();$('msg').innerText=`采样 ${$('cname').value}: H=${d.h} S=${d.s} V=${d.v}`;return}
 if(M=='calib'){let a=await J('/api/arm');let def=a.ok?(a.x_mm+','+a.y_mm):'';
  let s=prompt(`像素(${u},${v}) 对应的机械臂坐标 x,y (mm)`+(TRAYMODE?' [托盘模式: 作为参考点]':'')+(a.ok?' [已读取机械臂当前位置]':''),def);if(!s)return;
  let [x,y]=s.split(/[ ,，]+/).map(Number);await post('/api/calib/add',{u,v,x,y});refresh();return}
 if(M=='roi'){roi.push([u,v]);$('msg').innerText='ROI 点: '+JSON.stringify(roi);return}
 let d=await J('/api/pixel?u='+u+'&v='+v);let hsv=await J('/api/hsv?u='+u+'&v='+v);
 $('msg').innerText=`像素(${u},${v}) -> 机械臂(${d.x_mm}, ${d.y_mm}) mm   HSV=(${hsv.h},${hsv.s},${hsv.v})`;
 if($('goto').checked){if(!confirm(`机械臂开到 (${d.x_mm}, ${d.y_mm}) 上方? 确认周围无障碍`))return;$('msg').innerText+='  移动中...';
  let g=await post('/api/goto',{u,v});$('msg').innerText=g.ok?`到位: 目标(${g.target}) 实际(${g.reached}) 偏差 ${g.err_mm} mm — 看夹爪是否正对货物中心`:'失败: '+g.error}}
async function scan(){$('msg').innerText='扫描中...';let d=await post('/api/scan');
 $('msg').innerText=JSON.stringify(d.stat);let h='<table><tr><th>#</th><th>名称</th><th>类别</th><th>颜色</th><th>置信</th><th>像素</th><th>mm</th><th>帧</th></tr>';
 for(let t of d.targets)h+=`<tr><td>${t.id}</td><td>${t.label}</td><td>${t.class}</td><td>${t.color}</td><td>${t.conf}</td><td>${t.u},${t.v}</td><td>${t.x_mm},${t.y_mm}</td><td>${t.hits}</td></tr>`;
 $('tb').innerHTML=h+'</table>'}
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

    def goto(u, v):
        c = arm["client"]
        if c is None:
            return {"ok": False, "error": "未连接下位机 (启动时加 --arm)"}
        if not arm["lock"].acquire(blocking=False):    # 并发请求不能穿透: 一次只允许一个运动
            return {"ok": False, "error": "上一次移动还没结束"}
        try:
            x, y = svc.calibrator.pixel_to_robot(float(u), float(v))
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
        if os.path.exists(meta_path):
            import shutil
            shutil.copy(meta_path, meta_path + ".bak")
        data["hsv"] = new
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if hasattr(svc.detector, "hsv_cfg"):
            svc.detector.hsv_cfg = new
        return {"ok": True, "hsv": new, "saved": meta_path}

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
        keys = ("enabled", "inner_w_mm", "inner_h_mm", "rim_mm", "rim_height_mm", "axes", "ref_points", "auto_roi")
        return {"cfg": {k: cal.tray_cfg.get(k) for k in keys}, "status": cal.tray_status,
                "cam_height_mm": cal.cam_height_mm, "undistort": cal.camera_matrix is not None}

    def tray_set(body):
        cal = svc.calibrator
        upd = {k: body[k] for k in ("enabled", "inner_w_mm", "inner_h_mm", "rim_mm", "rim_height_mm",
                                    "axes", "ref_points", "auto_roi") if k in body}
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
            svc.calibrator.set_intrinsics(r["camera_matrix"], r["dist_coeffs"])
        return r

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
                            "problems": rep["problems"], "last_scan": svc.last_scan,
                            "arm": arm["client"] is not None, "tray_mode": svc.calibrator.tray_mode,
                            "tray": svc.calibrator.tray_status})
            elif u.path == "/api/pixel":
                x, y = svc.calibrator.pixel_to_robot(float(q["u"]), float(q["v"]))
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
            elif u.path == "/api/tray":
                self._json(tray_get())
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
                self._json(goto(b.get("u", 0), b.get("v", 0)))
            elif u == "/api/camera":
                self._json(camera_set(self._body()))
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
            elif u == "/api/tray":
                self._json(tray_set(self._body()))
            elif u == "/api/tray/redetect":
                self._json(tray_redetect())
            elif u == "/api/tray/k1":
                self._json(tray_k1(bool(self._body().get("save"))))
            elif u == "/api/chess/snap":
                self._json(chess_snap(self._body()))
            elif u == "/api/chess/solve":
                self._json(chess_solve())
            elif u == "/api/chess/clear":
                self._json(chess_clear())
            elif u == "/api/view":
                view["undist"] = bool(self._body().get("undist"))
                self._json(view)
            elif u == "/api/regress/add":
                self._json(regress_add(self._body()))
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
            p.add_argument("--port", type=int, default=8091)
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
