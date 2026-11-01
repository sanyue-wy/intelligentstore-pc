#!/usr/bin/env python3
"""
固定托盘位置: 把托盘内框在画面里的像素位置写进 vision/calib_config.json。
坐标系: 画面左上角 = (0,0), x 向右, y 向下, 单位像素 (原始画面, 不是去畸变后的)。

用法 (工程根目录):
  # 左上角 + 宽高 (像素), 托盘内框实际尺寸 (mm)
  python3 tools/set_tray_fixed.py --x 102 --y 64 --w 436 --h 327 --mm 240 180
  # 左上角 + 右下角
  python3 tools/set_tray_fixed.py --x 102 --y 64 --x2 538 --y2 391 --mm 240 180
  # 相机斜看 (托盘在画面里是梯形) 必须用四个角 (左上 右上 右下 左下); 矩形只适合相机正对
  python3 tools/set_tray_fixed.py --corners 102,64 538,66 537,392 101,390 --mm 240 180
  # 点的是壁顶内沿 (而不是底面内角) 时, 给出这四个角离托盘底面的高度
  python3 tools/set_tray_fixed.py --corners ... --mm 240 180 --z-mm 15
  # 镜头离托盘底面的高度 (斜看时用来求相机位姿, 强烈建议填)
  python3 tools/set_tray_fixed.py --corners ... --mm 240 180 --cam-height 380
  # 更简单: 调试台「托盘」页 →「点四角」, 在画面上点 4 个角, 自动吸附并保存
  # 可选: 托盘原点 (内框左上角) 在输出坐标系里的位置 mm, 以及轴向
  python3 tools/set_tray_fixed.py ... --origin-mm 35 20 --axes tray_x tray_y
  # 查看 / 取消
  python3 tools/set_tray_fixed.py --show
  python3 tools/set_tray_fixed.py --off            # 恢复自动检测托盘

改完重启视觉程序 (调试台 / 比赛程序) 生效; 调试台里绿框应正好压在托盘内框上。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "vision", "calib_config.json")


def load():
    if os.path.exists(CFG):
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    return {}


def main():
    ap = argparse.ArgumentParser(description="固定托盘像素位置 (画面左上角为原点)")
    ap.add_argument("--x", type=float, help="内框左上角 x 像素")
    ap.add_argument("--y", type=float, help="内框左上角 y 像素")
    ap.add_argument("--w", type=float, help="内框宽 像素")
    ap.add_argument("--h", type=float, help="内框高 像素")
    ap.add_argument("--x2", type=float, help="内框右下角 x 像素 (代替 --w)")
    ap.add_argument("--y2", type=float, help="内框右下角 y 像素 (代替 --h)")
    ap.add_argument("--corners", nargs=4, metavar="u,v", help="四角像素: 左上 右上 右下 左下")
    ap.add_argument("--mm", nargs=2, type=float, metavar=("W", "H"), help="托盘内框实际尺寸 mm (左右 上下)")
    ap.add_argument("--image-size", nargs=2, type=int, metavar=("W", "H"), default=None,
                    help="量取像素时的画面分辨率, 默认取配置里的 image_size 或 640 480")
    ap.add_argument("--origin-mm", nargs=2, type=float, metavar=("X0", "Y0"), default=None,
                    help="托盘内框左上角在输出坐标系里的位置 mm (没有参考点时生效)")
    ap.add_argument("--axes", nargs=2, default=None, metavar=("+X", "+Y"),
                    help="输出 +X/+Y 对应托盘哪个方向: tray_x -tray_x tray_y -tray_y (默认 tray_x tray_y)")
    ap.add_argument("--z-mm", type=float, default=None, help="四角离托盘底面的高度 mm (点壁顶内沿时=壁高, 默认 0)")
    ap.add_argument("--cam-height", type=float, default=None, help="镜头到托盘底面高度 mm (透视/视差用)")
    ap.add_argument("--clear-refs", action="store_true", help="清掉旧参考点 (改用 origin_mm 时需要)")
    ap.add_argument("--off", action="store_true", help="取消固定, 恢复自动检测托盘")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()

    data = load()
    tray = dict(data.get("tray") or {})
    if a.show:
        print(json.dumps({k: tray.get(k) for k in ("enabled", "inner_w_mm", "inner_h_mm", "fixed_px", "fixed_z_mm",
                                                   "origin_mm", "axes", "ref_points", "perspective")},
                         ensure_ascii=False, indent=2))
        return 0
    if a.off:
        tray.pop("fixed_px", None)
        data["tray"] = tray
    else:
        if a.corners:
            try:
                pts = [[float(v) for v in c.split(",")] for c in a.corners]
                assert all(len(p) == 2 for p in pts)
            except Exception:
                ap.error("--corners 格式: 102,64 538,66 537,392 101,390")
            fixed = {"corners": pts}
        else:
            if a.x is None or a.y is None:
                ap.error("需要 --x --y (以及 --w --h 或 --x2 --y2), 或 --corners")
            if a.w is not None and a.h is not None:
                fixed = {"x": a.x, "y": a.y, "w": a.w, "h": a.h}
            elif a.x2 is not None and a.y2 is not None:
                fixed = {"x1": a.x, "y1": a.y, "x2": a.x2, "y2": a.y2}
            else:
                ap.error("需要 --w --h 或 --x2 --y2")
            w = fixed.get("w", (fixed.get("x2") or 0) - a.x)
            h = fixed.get("h", (fixed.get("y2") or 0) - a.y)
            if w <= 0 or h <= 0:
                ap.error("宽高必须为正 (右下角要在左上角的右下方)")
        size = a.image_size or data.get("image_size") or [640, 480]
        fixed["image_size"] = [int(size[0]), int(size[1])]
        tray["fixed_px"] = fixed
        tray["enabled"] = True
        if a.mm:
            tray["inner_w_mm"], tray["inner_h_mm"] = a.mm
        if not tray.get("inner_w_mm") or not tray.get("inner_h_mm"):
            ap.error("还没有托盘内框尺寸, 加 --mm 宽 高")
        if a.origin_mm is not None:
            tray["origin_mm"] = list(a.origin_mm)
        if a.axes:
            tray["axes"] = list(a.axes)
        if a.z_mm is not None:
            tray["fixed_z_mm"] = a.z_mm
        if a.cam_height is not None:
            data["cam_height_mm"] = a.cam_height
        if not a.corners:
            print("提示: 矩形只适合相机正对托盘; 托盘在画面里是梯形时请用 --corners 四个角")
        if a.clear_refs:
            tray["ref_points"] = []
        tray.pop("corners_px", None)
        data["tray"] = tray
        if tray.get("ref_points") and tray.get("origin_mm") is not None:
            print("提示: 已有参考点 ref_points, origin_mm 不生效 (参考点优先); 要用 origin_mm 加 --clear-refs")
    if os.path.exists(CFG):
        with open(CFG, encoding="utf-8") as f:
            old = f.read()
        with open(CFG + ".bak", "w", encoding="utf-8") as f:
            f.write(old)
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print("已写入", CFG, "(旧文件备份为 calib_config.json.bak)")
    print(json.dumps({k: tray.get(k) for k in ("inner_w_mm", "inner_h_mm", "fixed_px", "origin_mm", "axes")},
                     ensure_ascii=False))
    print("重启视觉程序生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
