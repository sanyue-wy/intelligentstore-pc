# -*- coding: utf-8 -*-
"""比赛大屏全屏页 (/screen)。赛规要求装置屏只能显示, 无任何交互控件。"""

SCREEN_HTML = (
    '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>分拣大屏</title>'
    '<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}'
    'img{width:100vw;height:100vh;object-fit:contain;display:block}</style></head>'
    "<body><img id=\"s\" src=\"/screen.png\" alt=\"\">"
    "<script>setInterval(function(){var e=document.getElementById('s');e.src='/screen.png?'+Date.now();},1200);</script>"
    "</body></html>"
)
