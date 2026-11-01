# -*- coding: utf-8 -*-
"""网页服务公共基座: 口令校验、静态资源读取、JSON 助手。仅用标准库。"""
from __future__ import annotations

import json
import mimetypes
import os

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def static_path(name: str):
    """只允许访问 static 目录下的单层文件名，防目录穿越。"""
    name = os.path.basename((name or "").strip())
    if not name:
        return None
    p = os.path.join(STATIC_DIR, name)
    return p if os.path.isfile(p) else None


def read_static(name: str):
    p = static_path(name)
    if not p:
        return None
    with open(p, "rb") as f:
        return f.read()


def guess_type(name: str) -> str:
    p = name.lower()
    table = {
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".pdf": "application/pdf",
    }
    for ext, ctype in table.items():
        if p.endswith(ext):
            return ctype
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def token_ok(headers, token: str) -> bool:
    """口令校验: 支持 X-Token 请求头或 is_token Cookie。token 为空表示免口令。"""
    if not token:
        return True
    if headers.get("X-Token", "") == token:
        return True
    for part in (headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "is_token" and v == token:
            return True
    return False


def _sanitize(obj):
    """递归把 nan / inf 转成 None。

    json.dumps 默认允许 NaN/Infinity, 但这不是标准 JSON, 浏览器 JSON.parse 会直接
    抛 SyntaxError —— 通讯调试页 link_age_s 在还没有收到状态帧时是 float('inf'),
    会导致 /api/comm/state 变成非法 JSON、前端解析失败而误显示"通讯客户端不可用"。
    """
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def json_bytes(obj) -> bytes:
    return json.dumps(_sanitize(obj), ensure_ascii=False, default=str,
                      allow_nan=False).encode("utf-8")
