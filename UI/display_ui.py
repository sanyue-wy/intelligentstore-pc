# UI/display_ui.py
# -*- coding: utf-8 -*-
"""
分拣成功页 —— 唯一允许显示在比赛装置那块「高亮显示屏」上的内容。

赛项要求（2027 工创《智能分拣》PDF 第 10 / 12 页）:
  * 装置上的显示屏**只具有显示功能**，不支持交互；
  * 每分拣完成一件，须显示: 序号 / 货物名称 / 图片 / 分拣成功总数量；
  * 其它调试界面、操作界面不得出现在这块屏上。

显示通道（按优先级自动降级，任一环节缺失不影响分拣主流程）:
  1. RDK X5 大屏服务 :8756  —— 物理屏 HDMI + 远程客户端同时显示
       python3 x5_remote_server.py --bigscreen :0
  2. 无大屏服务时回退到本地 Tk 窗口（固定在 800x480，无任何控件）

远端的调试界面走 /ui 通道显示在笔记本上，与本文件无关。
"""

from __future__ import annotations

import os
import sys
import threading
import time

# 允许在容器内 / 根目录两种布局下都能 import 到 remote_screen_client
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
from UI.interfaces import ScreenBase
try:
    from UI.remote_screen_client import ScreenClient
except ImportError:                                    # 兼容根目录直接放置
    from remote_screen_client import ScreenClient      # type: ignore


def _font(size):
    from PIL import ImageFont
    for p in (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()


def render_screen(seq, name, total, image_path: str = "", stage: str = "分拣中",
                  W: int = 800, H: int = 480):
    """
    分拣成功页 (PIL Image): 序号 / 货物名称 / 图片 / 分拣成功总数量。
    本地兜底窗口与网页「大屏预览」共用, 保证调试时看到的就是比赛时显示的。
    """
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), (12, 16, 24))
    d = ImageDraw.Draw(img)
    font = _font

    d.rectangle([0, 0, W, 58], fill=(0, 120, 215))
    d.text((22, 13), "智能分拣", font=font(32), fill=(255, 255, 255))
    stage = stage or ("分拣中" if total else "待机")
    tw = d.textlength(stage, font=font(26))
    d.text((W - tw - 22, 17), stage, font=font(26), fill=(255, 255, 255))

    box = (26, 80, 336, 400)
    d.rectangle(box, outline=(58, 68, 88), width=2)
    shown = False
    if image_path and os.path.exists(image_path):
        try:
            pic = Image.open(image_path).convert("RGB")
            iw, ih = box[2] - box[0] - 18, box[3] - box[1] - 18
            pic.thumbnail((iw, ih))
            img.paste(pic, (box[0] + (iw + 18 - pic.width) // 2, box[1] + (ih + 18 - pic.height) // 2))
            shown = True
        except Exception:
            shown = False
    if not shown:
        t = "货物图片"
        tw = d.textlength(t, font=font(24))
        d.text(((box[0] + box[2] - tw) / 2, (box[1] + box[3]) / 2 - 15), t, font=font(24),
               fill=(150, 160, 175))

    x = 366
    d.text((x, 88), "序号", font=font(24), fill=(150, 160, 175))
    d.text((x, 116), str(seq), font=font(66), fill=(255, 255, 255))
    d.text((x, 204), "货物名称", font=font(24), fill=(150, 160, 175))
    name = str(name or "--")
    nf = font(44)
    while d.textlength(name, font=nf) > W - x - 22 and getattr(nf, "size", 10) > 18:
        nf = font(nf.size - 2)
    d.text((x, 232), name, font=nf, fill=(255, 255, 255))
    d.text((x, 306), "分拣成功总数量", font=font(24), fill=(150, 160, 175))
    d.text((x, 334), str(total), font=font(66), fill=(0, 220, 120))
    return img


class CompetitionUI(ScreenBase):
    """分拣成功页控制器。

    主链路：远程/物理大屏（通过 X5 大屏服务，:8756）。
    本地 Tk 兜底窗口已在阶段 2 移除，改由网页大屏 /screen 提供只读预览。
    """

    SCREEN_W = 800
    SCREEN_H = 480

    def __init__(self, endpoint: str = None, force_local: bool = False,
                 fullscreen: bool = True):
        self._seq = 0
        self._total = 0
        self._name = "--"
        self._stage = "待机"
        self._image_path = ""
        self._closed = False

        # 主链路：远程大屏 (物理屏由 x5_bigscreen / 网页 /screen 承载)
        self.client = ScreenClient(
            endpoint or os.environ.get("X5_SCREEN_ENDPOINT",
                                       "http://127.0.0.1:8756/api/screen"))
        # force_local/fullscreen 参数保留以兼容旧调用；本地 Tk 兜底已移除

    # ------------------------------------------------------------------
    # 对外接口（与原有调用方式完全兼容）
    # ------------------------------------------------------------------
    def update_status(self, seq, name, total, image_path: str = "",
                      stage: str = "分拣中") -> None:
        """更新大屏：序号 / 货物名称 / 图片 / 分拣成功总数量。"""
        self._seq = seq
        self._name = name
        self._total = total
        self._stage = stage
        if image_path:
            self._image_path = image_path

        # 主链路：非阻塞推送
        try:
            self.client.show(seq=seq, name=name, total=total,
                             image_path=self._image_path, stage=stage)
        except Exception:
            pass

    def flush(self, timeout: float = 1.0) -> bool:
        """等大屏推送完成 (赛规: 显示完本件信息后才分拣下一件)。"""
        try:
            return self.client.flush(timeout)
        except Exception:
            return False

    @property
    def state(self) -> dict:
        """当前大屏内容 (执行页/调试台预览用)。"""
        return {"seq": self._seq, "name": self._name, "total": self._total,
                "stage": self._stage, "image_path": self._image_path,
                "screen_service_ok": self.client.available()}

    def render(self):
        """当前大屏画面 (PIL Image)。"""
        return render_screen(self._seq, self._name, self._total, self._image_path, self._stage,
                             self.SCREEN_W, self.SCREEN_H)

    def set_image(self, path: str) -> None:
        """单独设置货物图片（下一次 update_status 时生效）。"""
        self._image_path = path

    def set_image_bytes(self, data: bytes) -> None:
        """直接给图片二进制（避免落盘）。"""
        try:
            self.client.show_image_bytes(self._seq, self._name, self._total,
                                         data)
        except Exception:
            pass

    def reset(self, stage: str = "待机") -> None:
        self._image_path = ""
        self.update_status(0, "等待开始", 0, stage=stage)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.client.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 比赛现场用：让大屏定格的等待
    # ------------------------------------------------------------------
    def hold(self, seconds: float = 5.0) -> None:
        """保持大屏显示 seconds 秒（供评委查看），不阻塞主线程以外的东西。"""
        time.sleep(seconds)


if __name__ == "__main__":
    # 手动测试: python3 -m UI.display_ui
    ui = CompetitionUI(force_local=False)
    for i in range(1, 6):
        ui.update_status(i, ["五棱柱", "正四面体", "球体", "圆柱体", "锥体"][i - 1], i)
        print(f"已推送 #{i}")
        time.sleep(1.2)
    ui.close()
    print("完成")
