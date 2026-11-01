# -*- coding: utf-8 -*-
"""公共网页包 —— 智能分拣上位机的统一网页基座。

仅用标准库 (http.server / json / threading)，不引入任何外部依赖或 CDN，
比赛现场断网也能用。两个网页服务共用这里的样式与脚本:

    * 调试台   :8090  (tools/vision_tool.py web, 视觉/流程/通讯/完整 四节)
    * 比赛执行页 :8088  (UI/run_page.py, 只读 + 开始/停止/急停/重启)

模块:
    base.py       口令校验 / 静态资源 / JSON 助手
    pages.py      调试台三节页面 (流程 / 通讯 / 完整) 的 HTML
    comm_debug.py 通讯调试后端
    flow_debug.py 流程调试 / 完整流程后端
    screen.py     比赛大屏全屏页 (/screen)
    static/       一份 CSS、一份公共 JS
"""

__version__ = "1.0"
