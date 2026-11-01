# -*- coding: utf-8 -*-
"""
比赛执行页  http://<X5_IP>:8088   (比赛程序自带, 随 start_competition.sh 启动)

和网页调试台 (tools/vision_tool.py web, :8090) 的区别:
    执行页只做"看"和"开关": 状态、流程、每件结果、错误、自检清单、相机画面、大屏预览,
    以及 开始 / 停止本轮 / 急停 / 一键重启 四个按钮。没有任何手动控制机械臂、改参数的功能。
    比赛模式下网页调试台会被自动关闭 (避免被认定为遥控), 执行页保留。
    运行中只有「急停」「停止本轮」可用; 「开始」只在待机时可用, 按下后双手离开即可。

app 需要提供 (UI/run_competition.py 的 CompetitionApp):
    app.state_dict() -> dict
    app.frame_jpeg() -> bytes | None
    app.screen_png() -> bytes | None
    app.request(action: str) -> (ok: bool, msg: str)       action: start / stop / estop / restart / precheck
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>智能分拣 · 执行页</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:"Microsoft YaHei",sans-serif;background:#10141c;color:#dde;margin:0}
header{display:flex;align-items:center;gap:16px;padding:10px 16px;background:#18202c}
#state{font-size:28px;font-weight:bold;padding:4px 14px;border-radius:6px}
.standby{background:#225}.running{background:#063}.finished{background:#335}.error{background:#811}.checking{background:#552}
#timer{font-size:28px;font-family:monospace}#done{font-size:28px}
button{font-size:18px;padding:10px 18px;margin:0 4px;border:0;border-radius:6px;color:#fff;cursor:pointer}
button:disabled{opacity:.3;cursor:not-allowed}
#bstart{background:#0a6}#bstop{background:#a60}#bestop{background:#c00}#brestart{background:#446}#bcheck{background:#335}
main{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:10px}
.card{background:#18202c;border-radius:8px;padding:10px}
h3{margin:0 0 6px 0;font-size:15px;color:#9ab}
img{max-width:100%;border-radius:4px}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid #2a3444;padding:3px 5px;text-align:left}
.ok{color:#3d8}.warn{color:#fb3}.err{color:#f55}.mono{font-family:monospace;font-size:12px}
#events{max-height:230px;overflow:auto}#msg{color:#fb3}
</style></head><body>
<header><span id="state" class="standby">--</span><span id="timer">0.0s</span><span id="done">0 件</span>
<span style="flex:1"></span>
<button id="bstart" onclick="act('start')">开始</button>
<button id="bstop" onclick="if(confirm('停止本轮, 回到待机?'))act('stop')">停止本轮</button>
<button id="bestop" onclick="act('estop')">急停</button>
<button id="brestart" onclick="if(confirm('重启比赛程序? (约 10 秒回到待机)'))act('restart')">一键重启</button>
<button id="bcheck" onclick="act('precheck')">刷新自检</button></header>
<div style="padding:4px 16px"><span id="hint"></span> <span id="msg"></span></div>
<main>
<div class="card"><h3>相机 (最近一次识别)</h3><img id="cam" src="/frame.jpg"></div>
<div class="card"><h3>大屏预览 (与装置屏一致)</h3><img id="scr" src="/screen.png"></div>
<div class="card"><h3>自检 <span id="checksum"></span></h3><table id="check"></table></div>
<div class="card"><h3>本轮分拣</h3><table id="items"></table><div id="slots" class="mono"></div></div>
<div class="card" style="grid-column:span 2"><h3>流程 / 错误</h3><div id="events" class="mono"></div></div>
</main>
<script>
const $=i=>document.getElementById(i);
async function act(a){let r=await fetch('/api/'+a,{method:'POST'});let d=await r.json();$('msg').innerText=d.msg||'';tick()}
const SN={standby:'待机',running:'分拣中',finished:'已完成',error:'异常',checking:'自检中',restarting:'重启中'};
let lastSeq=-1,lastScan=-1;
async function tick(){
 let s; try{s=await (await fetch('/api/state')).json()}catch(e){$('state').innerText='连接断开';$('state').className='error';return}
 $('state').innerText=SN[s.state]||s.state;$('state').className=s.state;
 $('timer').innerText=s.elapsed_s.toFixed(1)+'s';$('done').innerText=s.total_done+' 件';
 $('bstart').disabled=s.state!='standby'&&s.state!='finished';$('bstop').disabled=s.state!='running';
 $('hint').innerText=s.hint||'';
 $('checksum').innerHTML=s.precheck_summary?`<span class="${s.precheck_ok?'ok':'err'}">${s.precheck_summary}</span>`:'';
 $('check').innerHTML=(s.precheck||[]).map(i=>`<tr><td class="${i.level=='ok'?'ok':i.level=='warn'?'warn':'err'}">${i.level=='ok'?'✔':i.level=='warn'?'!':'✘'} ${i.name}</td><td>${i.detail}</td></tr>`).join('');
 $('items').innerHTML='<tr><th>#</th><th>货物</th><th>置信</th><th>盒</th><th>结果</th><th>用时</th></tr>'+(s.items||[]).slice(-14).reverse().map(i=>
  `<tr><td>${i.seq||''}</td><td>${i.label}</td><td>${(i.conf||0).toFixed(2)}</td><td>${i.slot}</td><td class="${i.result=='ok'?'ok':'err'}">${i.result}</td><td>${(i.elapsed_s||0).toFixed(1)}s</td></tr>`).join('');
 $('slots').innerHTML=Object.entries(s.slots||{}).map(([k,v])=>`${k}号盒: ${v.label} ×${v.count}`).join('<br>')+((s.warnings||[]).length?'<br><span class="warn">'+s.warnings.join('<br>')+'</span>':'');
 $('events').innerHTML=(s.events||[]).slice(-60).reverse().map(e=>`<div class="${e.level=='error'?'err':e.level=='warn'?'warn':''}">[${e.t.toFixed(1)}] ${e.msg}</div>`).join('');
 if(s.screen_seq!=lastSeq){lastSeq=s.screen_seq;$('scr').src='/screen.png?'+Date.now()}
 if(s.scan_n!=lastScan){lastScan=s.scan_n;$('cam').src='/frame.jpg?'+Date.now()}
}
setInterval(tick,700);tick();
</script></body></html>"""


class RunPage:
    def __init__(self, app, port: int = 8088, host: str = "0.0.0.0"):
        self.app = app
        self.port = port
        self.host = host
        self._srv = None

    def start(self) -> bool:
        app = self.app

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body: bytes, ctype: str, code: int = 200):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code=200):
                self._send(json.dumps(obj, ensure_ascii=False, default=str).encode(),
                           "application/json; charset=utf-8", code)

            def do_GET(self):
                p = urlparse(self.path).path
                try:
                    if p == "/":
                        self._send(HTML.encode(), "text/html; charset=utf-8")
                    elif p == "/api/state":
                        self._json(app.state_dict())
                    elif p == "/frame.jpg":
                        b = app.frame_jpeg()
                        self._send(b, "image/jpeg") if b else self.send_error(404)
                    elif p == "/screen.png":
                        b = app.screen_png()
                        self._send(b, "image/png") if b else self.send_error(404)
                    else:
                        self.send_error(404)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as e:
                    logger.debug("执行页 GET 异常: %s", e)

            def do_POST(self):
                p = urlparse(self.path).path
                if p.startswith("/api/"):
                    try:
                        ok, msg = app.request(p[5:])
                    except Exception as e:
                        ok, msg = False, str(e)
                    self._json({"ok": ok, "msg": msg})
                else:
                    self.send_error(404)

        try:
            ThreadingHTTPServer.allow_reuse_address = True
            self._srv = ThreadingHTTPServer((self.host, self.port), H)
            self._srv.daemon_threads = True
        except OSError as e:
            logger.error("执行页端口 %d 打不开: %s (比赛流程不受影响)", self.port, e)
            return False
        threading.Thread(target=self._srv.serve_forever, name="RunPage", daemon=True).start()
        logger.info("执行页: http://<本机IP>:%d", self.port)
        return True

    def stop(self):
        if self._srv:
            try:
                self._srv.shutdown()
                self._srv.server_close()
            except Exception:
                pass
