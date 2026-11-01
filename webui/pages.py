# -*- coding: utf-8 -*-
"""调试台三节页面 (流程调试 / 通讯调试 / 完整过程调试) 的 HTML。

由 tools/vision_tool.py 的网页服务在 /flow /comm /run 三个路由直接返回，
共用 /static/app.css 与 /static/common.js，顶部导航与视觉页一致。
纯字符串拼接 (脚本里的花括号不做 Python 格式化)。
"""
from __future__ import annotations

LINKS = [("视觉调试", "/"), ("流程调试", "/flow"), ("通讯调试", "/comm"), ("完整调试", "/run")]


def nav(active):
    s = '<div class="topnav"><span class="brand">智能分拣调试台</span>'
    for name, href in LINKS:
        cls = ' class="on"' if href == active else ''
        s += '<a href="%s"%s>%s</a>' % (href, cls, name)
    s += '<span class="spacer"></span><span class="badge dbg">调试模式 · 非比赛</span>'
    s += '<a href="/screen.png" target="_blank" style="margin-left:8px">大屏预览</a></div>'
    return s


def _doc(title, active, body, script):
    return ('<!doctype html><html lang="zh"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + title + '</title>'
            '<link rel="stylesheet" href="/static/app.css"></head>'
            '<body class="is-debug">' + nav(active) +
            '<main class="page">' + body + '</main>'
            '<script src="/static/common.js"></script>'
            '<script>' + script + '</script></body></html>')


# ------------------------------------------------------------------ 通讯调试
_COMM_BODY = """
<div class="grid2">
  <div class="card"><h3>串口连接</h3>
    <div class="row"><label>端口</label><select id="port" style="min-width:220px"></select>
      <button class="btn sm ghost" onclick="ports()">刷新</button></div>
    <div class="row"><label>波特率</label><input id="baud" value="115200" style="width:100px">
      <button class="btn primary" onclick="conn()">连接</button>
      <button class="btn" onclick="mock()">Mock 模拟</button>
      <button class="btn danger" onclick="disc()">断开</button>
      <span id="link" class="badge">--</span></div>
    <h3 style="margin-top:16px">实时状态帧 (HostStatus)</h3>
    <table id="status"></table>
  </div>
  <div class="card"><h3>发送命令 <span class="sub">急停一键直达, 危险动作二次确认</span></h3>
    <div class="row">
      <button class="btn sm" onclick="send('clear')">清报警</button>
      <button class="btn sm danger" onclick="send('estop')">急停</button>
      <button class="btn sm" onclick="send('cancel')">取消任务</button>
      <button class="btn sm" onclick="send('permit')">允许抓取</button>
    </div>
    <div class="row">
      <button class="btn sm" onclick="send('grip_open')">夹爪开</button>
      <button class="btn sm" onclick="send('grip_close')">夹爪合</button>
      <button class="btn sm" onclick="send('z_up')">Z 抬</button>
      <button class="btn sm" onclick="send('z_down')">Z 下潜</button>
    </div>
    <div class="row">
      <button class="btn sm" onclick="send('tray_shake')">托盘抖料</button>
      <button class="btn sm" onclick="send('tray_home')">托盘回零</button>
      <button class="btn sm" onclick="send('poll')">轮询</button>
    </div>
    <div class="row"><label>储物盘到槽</label><input id="slot" value="0" style="width:64px">
      <button class="btn sm" onclick="send('storage_slot',{slot:+$('slot').value})">GOTO_SLOT</button>
      <button class="btn sm" onclick="send('storage_step',{ccw:true})">步进 CCW</button>
      <button class="btn sm" onclick="send('storage_step',{ccw:false})">步进 CW</button></div>
    <div class="row"><label>运动到 x,y</label><input id="mx" value="0" style="width:76px">
      <input id="my" value="0" style="width:76px">
      <button class="btn sm" onclick="send('move',{x:+$('mx').value,y:+$('my').value})">MOVE</button></div>
    <div class="row muted">状态帧包含 state / busy / estop / alarm / res_flags / pick_ready / 当前坐标 / 储物盘槽位。</div>
  </div>
</div>
<div class="card" style="margin-top:12px"><h3>收发日志 <span class="sub" id="logsub"></span></h3>
  <div class="log" id="log"></div></div>
"""

_COMM_JS = """
async function ports(){
  let d=await J('/api/comm/ports'); let s=$('port');
  s.innerHTML=(d.ports||[]).map(p=>'<option value="'+esc(p.port)+'">'+esc(p.port)+' — '+esc(p.desc)+'</option>').join('')||'<option value="">(未发现串口, 可用 Mock)</option>';
}
async function refresh(){
  let d=await J('/api/comm/state');
  if(!d.available){$('link').innerText='通讯客户端不可用';$('link').className='badge bad';return}
  let lb=d.connected?(d.mock?'Mock 已连接':'已连接'):'未连接';
  $('link').innerText=lb+(d.link?' · '+d.link:'');
  $('link').className='badge '+(d.connected?'ok':'bad');
  let st=d.status||{}; let R=[
    ['状态',st.state_desc||st.state],['忙',st.busy?'是':'否'],['急停',st.estop?'是':'否'],
    ['报警',st.alarm?'是':'否'],['取件位',st.pick_ready?'是':'否'],
    ['坐标',(st.x_mm!==undefined?st.x_mm:'-')+', '+(st.y_mm!==undefined?st.y_mm:'-')+' mm'],
    ['res_flags','0x'+(st.res_flags||0).toString(16)],['res_stage',st.res_stage],
    ['储物槽',st.storage_slot],['链路年龄',(d.age_s===null||d.age_s===undefined)?'-':(d.age_s+' s')]];
  $('status').innerHTML=R.map(r=>'<tr><th>'+r[0]+'</th><td>'+esc(r[1])+'</td></tr>').join('');
  renderLog(d.logs||[]);
}
function renderLog(logs){
  let h=logs.map(l=>'<div class="'+l.kind+'">'+(l.kind=='tx'?'TX ':l.kind=='rx'?'RX ':'· ')+esc(l.text)+'</div>').join('');
  let el=$('log'); let atBottom=(el.scrollHeight-el.scrollTop-el.clientHeight)<40;
  el.innerHTML=h; $('logsub').innerText=logs.length+' 条';
  if(atBottom)el.scrollTop=el.scrollHeight;
}
async function conn(){let d=await post('/api/comm/connect',{port:$('port').value,baudrate:+$('baud').value});toast(d.ok?'连接成功':'连接失败: '+(d.error||''),d.ok);refresh()}
async function mock(){let d=await post('/api/comm/mock');toast(d.ok?'已用 MockSTM32 模拟':'失败: '+(d.error||''),d.ok);refresh()}
async function disc(){let d=await post('/api/comm/disconnect');toast(d.ok?'已断开':'失败',d.ok);refresh()}
async function send(cmd,extra){
  if(cmd!='estop'&&['move','storage_slot','storage_step','tray_shake','tray_home','grip_close','z_down'].includes(cmd)){
    if(!confirm2('确认发送 '+cmd+' ?'))return;
  }
  let b=Object.assign({cmd:cmd},extra||{});
  let d=await post('/api/comm/send',b);
  toast(cmd+(d.ok?' 有 ACK':' 无响应'+(d.error?' ('+d.error+')':'')),d.ok);refresh();
}
ports();poll(refresh,700);
"""


# ------------------------------------------------------------------ 流程调试
_FLOW_BODY = """
<div class="row">
  <button class="btn primary" onclick="step('scan')">扫描一次</button>
  <button class="btn" onclick="step('plan')">规划一次</button>
  <button class="btn" onclick="step('exec')">执行一件</button>
  <button class="btn warn" onclick="step('shake')">抖料</button>
  <button class="btn ghost" onclick="step('reset')">重置</button>
  <span id="connbadge" class="badge">--</span>
</div>
<div class="grid2" style="margin-top:12px">
  <div class="card"><h3>识别结果</h3><table id="targets"></table></div>
  <div class="card"><h3>料盒 / 规划</h3>
    <div id="slots" class="mono"></div>
    <div id="warn" class="warn" style="margin-top:6px"></div>
    <h3 style="margin-top:12px">选中 / 上次结果</h3><pre id="chosen" class="mono muted"></pre></div>
</div>
<div class="card" style="margin-top:12px"><h3>流程事件</h3><div class="log" id="log"></div></div>
"""

_FLOW_JS = """
async function step(a){
  let d;
  if(a=='scan'){d=await post('/api/flow/scan');toast('扫描: '+((d.targets||[]).length)+' 件',d.ok)}
  else if(a=='plan'){d=await post('/api/flow/plan');toast(d.ok?('选中 '+(d.chosen||{}).label):'无可抓目标',d.ok)}
  else if(a=='exec'){d=await post('/api/flow/exec');toast(d.ok?'执行成功':('执行失败: '+(d.message||d.error||'')),d.ok)}
  else if(a=='shake'){d=await post('/api/flow/shake');toast('抖料',d.ok)}
  else if(a=='reset'){d=await post('/api/flow/reset');toast('已重置',d.ok)}
  refresh();
}
async function refresh(){
  let d=await J('/api/flow/state');
  $('connbadge').innerText=d.connected?'下位机已连接':'下位机未连接 (执行时自动 Mock)';
  $('connbadge').className='badge '+(d.connected?'ok':'');
  let ts=d.targets||[];
  $('targets').innerHTML='<tr><th>#</th><th>名称</th><th>类别</th><th>置信</th><th>像素</th><th>mm</th></tr>'+
    ts.map(t=>'<tr><td>'+esc(t.id)+'</td><td>'+esc(t.label)+'</td><td>'+esc(t.class)+'</td><td>'+(t.conf||0).toFixed(2)+'</td><td>'+esc(t.u)+','+esc(t.v)+'</td><td>'+esc(t.x_mm)+','+esc(t.y_mm)+'</td></tr>').join('')||'<tr><td colspan=6 class="muted">(无, 先扫描)</td></tr>';
  $('slots').innerHTML=Object.entries(d.slots||{}).map(([k,v])=>k+' 号盒: '+esc(v.label)+' ×'+v.count).join('<br>')||'(空)';
  $('warn').innerHTML=(d.warnings||[]).map(w=>esc(w)).join('<br>');
  let last=d.last_result?JSON.stringify(d.last_result,null,1):'(无)';
  $('chosen').innerText='上次结果: '+last+'\\n\\n'+((d.scan_stat)?('扫描统计: '+JSON.stringify(d.scan_stat)):'');
  $('log').innerHTML=(d.events||[]).map(e=>'<div class="ev">['+e.t.toFixed(1)+'] '+esc(e.msg)+'</div>').join('');
}
poll(refresh,900);
"""


# ------------------------------------------------------------------ 完整调试
_RUN_BODY = """
<div class="row">
  <button class="btn primary" id="bstart" onclick="start()">开始完整流程</button>
  <button class="btn warn" onclick="stop()">暂停 / 停止</button>
  <button class="btn danger" onclick="estop()">急停</button>
  <span id="runbadge" class="badge">--</span>
  <span class="muted">默认用 MockSTM32 + 合成相机离线联调; 接真机前先在通讯调试页连接。</span>
</div>
<div class="grid2" style="margin-top:12px">
  <div class="card"><h3>相机画面 (实时识别叠加)</h3>
    <img id="cam" src="/stream" style="width:100%;border-radius:6px;border:1px solid #2a3444"></div>
  <div class="card"><h3>当前目标 / 料盒</h3>
    <table id="targets"></table><div id="slots" class="mono" style="margin-top:8px"></div></div>
</div>
<div class="card" style="margin-top:12px"><h3>通讯 / 事件</h3><div class="log" id="log"></div></div>
"""

_RUN_JS = """
async function start(){let d=await post('/api/run/start');toast(d.ok?'已开始完整流程':'无法开始: '+(d.error||''),d.ok);refresh()}
async function stop(){let d=await post('/api/run/stop');toast('已请求停止',d.ok);refresh()}
async function estop(){let d=await post('/api/run/estop');toast(d.ok?'已发送急停':'急停失败: '+(d.error||''),d.ok===true)}
async function refresh(){
  let d=await J('/api/run/state');
  let run=d.running;
  $('runbadge').innerText=run?'运行中':'空闲';
  $('runbadge').className='badge '+(run?'ok':'');
  $('bstart').disabled=run;
  let ts=d.targets||[];
  $('targets').innerHTML='<tr><th>#</th><th>名称</th><th>置信</th><th>mm</th></tr>'+
    ts.map(t=>'<tr><td>'+esc(t.id)+'</td><td>'+esc(t.label)+'</td><td>'+(t.conf||0).toFixed(2)+'</td><td>'+esc(t.x_mm)+','+esc(t.y_mm)+'</td></tr>').join('')||'<tr><td colspan=4 class="muted">(无)</td></tr>';
  $('slots').innerHTML=Object.entries(d.slots||{}).map(([k,v])=>k+' 号盒: '+esc(v.label)+' ×'+v.count).join('<br>');
  $('log').innerHTML=(d.events||[]).map(e=>'<div class="ev">['+e.t.toFixed(1)+'] '+esc(e.msg)+'</div>').join('');
  $('log').scrollTop=$('log').scrollHeight;
}
poll(refresh,900);
"""


_PAGES = {
    "/comm": ("通讯调试 · 智能分拣", "通讯调试", _COMM_BODY, _COMM_JS),
    "/flow": ("流程调试 · 智能分拣", "流程调试", _FLOW_BODY, _FLOW_JS),
    "/run": ("完整过程调试 · 智能分拣", "完整调试", _RUN_BODY, _RUN_JS),
}


def page(path):
    item = _PAGES.get(path)
    if not item:
        return None
    title, active, body, script = item
    return _doc(title, active, body, script)
