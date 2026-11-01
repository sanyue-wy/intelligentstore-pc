/* 智能分拣上位机 —— 公共网页脚本 (无外部依赖) */
const $ = (i) => document.getElementById(i);

async function J(u, o) {
  const r = await fetch(u, o || {});
  const txt = await r.text();
  let d;
  try { d = txt ? JSON.parse(txt) : {}; } catch (e) { d = { ok: false, error: txt }; }
  if (!r.ok && (d.ok === undefined)) { d.ok = false; if (!d.error) d.error = ('HTTP ' + r.status); }
  return d;
}
function post(u, b) {
  return J(u, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b || {}) });
}
function toast(msg, ok) {
  const d = document.createElement('div');
  d.className = 'toast ' + (ok === false ? 'bad' : 'ok');
  d.innerText = String(msg);
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 2800);
}
function confirm2(msg) { return window.confirm(msg); }
function poll(fn, ms) {
  const step = async () => {
    try { await fn(); } catch (e) { console.log(e); }
    finally { setTimeout(step, ms); }
  };
  step();
}
function esc(s) {
  return ('' + (s === undefined || s === null ? '' : s)).replace(/[&<>]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
}
