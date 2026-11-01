#!/usr/bin/env python3
"""
相机内参 / 畸变标定 (棋盘格), 结果写入 vision/calib_config.json 的
camera_matrix / dist_coeffs, CoordinateCalibrator 会自动做畸变校正。

【推荐: 一键单张校正】只校正镜头畸变 (k1/k2), 不需要多角度拍几十张:
    1. 打印 tools/calib_board_A4.pdf (棋盘格; 打印缩放比例无所谓), 平铺在托盘上 (尽量盖住托盘, 别反光、别拱起)
    2. 浏览器调试台「畸变」页点「一键校正」—— 自动认格子、计算、写入; 可换个位置再点一次叠加, 更准
    命令行 (已有照片): python3 tools/calib_intrinsics.py --one 照片.png [照片2.png ...]
原理: 平面上的格子线拍出来应是直线, 单张图里求让所有格点满足同一透视关系的径向畸变 k1/k2;
      焦距/主点固定 (主点=画面中心), 去畸变后的像素只依赖 k/f², 所以焦距不用准、方格尺寸也不用准。

【完整内参标定】(一般不需要) 打印 9x6 内角点棋盘格, 在托盘区域各位置/倾角拍 15~25 张
    python3 tools/calib_intrinsics.py --images tools/chess_images --cols 9 --rows 6 --square 20
"""
import argparse
import glob
import json
import os

import cv2
import numpy as np

CFG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vision", "calib_config.json")
_CRIT = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1e-3)


def find_corners(gray, cols, rows):
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_NORMALIZE_IMAGE + cv2.CALIB_CB_FAST_CHECK
    ok, c = cv2.findChessboardCorners(gray, (cols, rows), flags)
    if not ok:
        return None
    return cv2.cornerSubPix(gray, c, (11, 11), (-1, -1), _CRIT)


def calibrate(files, cols=9, rows=6, square=20.0, cfg_path=CFG, write=True):
    """返回 {"ok", "rms", "used", "camera_matrix", "dist_coeffs", "error"}; write=True 时写入 calib_config.json。"""
    objp = np.zeros((rows * cols, 3), np.float32)
    objp[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * square
    objs, imgs, size = [], [], None
    for f in files:
        g = cv2.imread(f, cv2.IMREAD_GRAYSCALE)
        if g is None:
            continue
        if size is not None and g.shape[::-1] != size:
            continue
        size = g.shape[::-1]
        c = find_corners(g, cols, rows)
        if c is not None:
            objs.append(objp)
            imgs.append(c)
    if len(objs) < 8:
        return {"ok": False, "used": len(objs), "error": f"有效棋盘格图片 {len(objs)} 张, 至少 8 张 (建议 15~25)"}
    rms, K, D, _, _ = cv2.calibrateCamera(objs, imgs, size, None, None)
    out = {"ok": True, "rms": round(float(rms), 3), "used": len(objs), "image_size": list(size),
           "camera_matrix": K.tolist(), "dist_coeffs": D.reshape(-1).tolist(),
           "tip": "RMS < 0.5px 为好; 偏大就补拍画面四角、倾斜的棋盘格" if rms >= 0.5 else ""}
    if write:
        data = {}
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding="utf-8") as fh:
                data = json.load(fh)
        data["camera_matrix"] = out["camera_matrix"]
        data["dist_coeffs"] = out["dist_coeffs"]
        data["intrinsics_source"] = f"chessboard rms={out['rms']}"
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        out["saved"] = cfg_path
    return out


# ============================================================================ 一键单张校正
BOARD_SIZES = [(12, 8), (9, 6), (8, 6), (10, 7), (11, 8), (7, 5), (8, 5), (6, 4), (13, 9), (7, 7), (5, 4)]


def detect_board(gray, sizes=None):
    """自动认棋盘格内角点尺寸, 返回 (corners Nx2 float32, (cols, rows)) 或 (None, None)。"""
    sizes = sizes or BOARD_SIZES
    small = gray
    for cr in sizes:
        c = None
        if hasattr(cv2, "findChessboardCornersSB"):
            ok, c = cv2.findChessboardCornersSB(small, cr, cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_EXHAUSTIVE
                                                | cv2.CALIB_CB_ACCURACY)
            if not ok:
                c = None
        if c is None:
            c = find_corners(gray, *cr)
        if c is not None:
            return np.float32(c).reshape(-1, 2), cr
    return None, None


def _homography_rms(obj2d, img):
    H, _ = cv2.findHomography(obj2d, img, 0)
    if H is None:
        return float("nan")
    p = cv2.perspectiveTransform(obj2d.reshape(-1, 1, 2), H).reshape(-1, 2)
    return float(np.sqrt(((p - img) ** 2).sum(1).mean()))


def _undist(pts, K, D):
    crit = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 50, 1e-6)
    p = np.float32(pts).reshape(-1, 1, 2)
    if hasattr(cv2, "undistortPointsIter"):
        return cv2.undistortPointsIter(p, K, D, None, K, crit).reshape(-1, 2)
    return cv2.undistortPoints(p, K, D, P=K).reshape(-1, 2)


def _nelder_mead(fun, x0, step, iters=200, tol=1e-7):
    n = len(x0)
    pts = [np.float64(x0)] + [np.float64(x0) + np.eye(n)[i] * step[i] for i in range(n)]
    vals = [fun(p) for p in pts]
    for _ in range(iters):
        order = np.argsort(vals)
        pts, vals = [pts[k] for k in order], [vals[k] for k in order]
        if abs(vals[-1] - vals[0]) < tol:
            break
        c = np.mean(pts[:-1], 0)
        xr = c + (c - pts[-1]); fr = fun(xr)
        if fr < vals[0]:
            xe = c + 2 * (c - pts[-1]); fe = fun(xe)
            pts[-1], vals[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = xr, fr
        else:
            xc = c + 0.5 * (pts[-1] - c); fc = fun(xc)
            if fc < vals[-1]:
                pts[-1], vals[-1] = xc, fc
            else:
                pts = [pts[0] + 0.5 * (p - pts[0]) for p in pts]
                vals = [fun(p) for p in pts]
    k = int(np.argmin(vals))
    return pts[k], vals[k]


def calibrate_views(views, image_size, prev_K=None):
    """
    views: [(corners Nx2, (cols, rows)), ...] 同一分辨率。
    平面格子在无畸变相机里 = 一个单应; 找让所有格点最符合单应的径向畸变 k1 (覆盖面够大再加 k2)。
    主点固定画面中心, 焦距取经验值 (去畸变像素只依赖 k/f², 焦距不用准)。纯 2D, 不需要多角度。
    返回 {"ok","k1","k2","before_px","after_px","max_shift_px","camera_matrix","dist_coeffs",...}
    """
    w, h = image_size
    if not views:
        return {"ok": False, "error": "没有检测到棋盘格"}
    f = 0.8 * max(w, h)
    K = np.float64([[f, 0, w / 2.0], [0, f, h / 2.0], [0, 0, 1]])
    objs, pts_all = [], []
    for c, (cols, rows) in views:
        objs.append(np.float32(np.mgrid[0:cols, 0:rows].T.reshape(-1, 2)))
        pts_all.append(np.float32(c).reshape(-1, 2))
    allp = np.concatenate(pts_all)
    cover = cv2.contourArea(cv2.convexHull(allp)) / float(w * h)
    r_max = float(np.max(np.hypot(allp[:, 0] - w / 2, allp[:, 1] - h / 2)) / np.hypot(w / 2, h / 2))
    solve_k2 = cover >= 0.25 and r_max >= 0.6

    def cost_of(D):
        return float(np.mean([_homography_rms(o, _undist(p, K, D)) for o, p in zip(objs, pts_all)]))

    def D_of(x):
        return np.float64([x[0], x[1] if len(x) > 1 else 0.0, 0, 0, 0])

    before = cost_of(np.zeros(5))
    grid = np.linspace(-0.6, 0.6, 49)
    costs = [cost_of(D_of([k])) for k in grid]
    k0 = float(grid[int(np.argmin(costs))])
    x, best = _nelder_mead(lambda x: cost_of(D_of(x)), [k0], [0.02])
    if solve_k2:
        x2, best2 = _nelder_mead(lambda x: cost_of(D_of(x)), [x[0], 0.0], [0.02, 0.02])
        if best2 < best * 0.9:            # k2 明显有用才保留, 防止过拟合
            x, best = x2, best2
    D = D_of(x)
    # 畸变明显且格子够大时, 再放开主点 (实际镜头光心常偏离画面中心 10~30px)
    pp_moved = False
    if before > 1.0 and cover >= 0.2 and best > 0.15:
        def cost_pp(y):
            Kp = K.copy(); Kp[0, 2] = w / 2.0 + y[0] * 10; Kp[1, 2] = h / 2.0 + y[1] * 10
            Dp = np.float64([y[2], y[3] if solve_k2 else 0.0, 0, 0, 0])
            return float(np.mean([_homography_rms(o, _undist(p, Kp, Dp)) for o, p in zip(objs, pts_all)]))
        y, best3 = _nelder_mead(cost_pp, [0.0, 0.0, D[0], D[1]], [0.5, 0.5, 0.02, 0.02], iters=400)
        if best3 < best * 0.8 and abs(y[0]) * 10 < 0.1 * w and abs(y[1]) * 10 < 0.1 * h:
            K[0, 2] = w / 2.0 + y[0] * 10; K[1, 2] = h / 2.0 + y[1] * 10
            D = np.float64([y[2], y[3] if solve_k2 else 0.0, 0, 0, 0])
            best, pp_moved = best3, True
    after = best
    corners_img = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1], [w / 2, 0], [0, h / 2]])
    shift = float(np.max(np.hypot(*(_undist(corners_img, K, D) - corners_img).T)))
    out = {"ok": True, "views": len(views), "points": int(len(allp)), "coverage": round(cover, 2),
           "k1": round(float(D[0]), 5), "k2": round(float(D[1]), 5),
           "before_px": round(before, 3), "after_px": round(after, 3),
           "max_shift_px": round(shift, 1), "image_size": [w, h],
           "principal_px": [round(K[0, 2], 1), round(K[1, 2], 1)], "principal_solved": pp_moved,
           "camera_matrix": K.tolist(), "dist_coeffs": [float(v) for v in D]}
    tips = []
    if before <= 0.3 or after > before * 0.8:
        out["ok"] = False
        out["error"] = (f"畸变很小 (格线拟合误差 {before:.2f}px), 不需要校正, 未写入" if before <= 0.6 else
                        "校正后改善不明显 (格子拱起/反光/没铺平?), 未写入")
    if cover < 0.30:
        tips.append(f"格子只盖住画面 {cover:.0%}, 换到画面边角再点一次叠加会更准")
    if out["ok"] and after > 0.6:
        tips.append("校正后残差仍偏大: 检查格子是否平整、对焦是否清楚")
    out["tip"] = "; ".join(tips)
    return out


def write_intrinsics(K, D, cfg_path=CFG, source=""):
    data = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as fh:
            data = json.load(fh)
    if data.get("camera_matrix") is not None:
        data["prev_intrinsics"] = {"camera_matrix": data["camera_matrix"], "dist_coeffs": data.get("dist_coeffs"),
                                   "source": data.get("intrinsics_source", "")}
    data["camera_matrix"], data["dist_coeffs"] = K, D
    data["intrinsics_source"] = source
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    return cfg_path


def one_shot(files, cfg_path=CFG, write=True):
    views, size = [], None
    for f in files:
        g = cv2.imread(f, cv2.IMREAD_GRAYSCALE)
        if g is None:
            print("读不了", f)
            continue
        if size is not None and g.shape[::-1] != size:
            print("分辨率不同, 跳过", f)
            continue
        c, cr = detect_board(g)
        if c is None:
            print("没找到棋盘格:", f)
            continue
        size = g.shape[::-1]
        views.append((c, cr))
        print(f"{os.path.basename(f)}: {cr[0]}x{cr[1]} 内角点")
    if not views:
        return {"ok": False, "error": "没有一张图找到棋盘格"}
    r = calibrate_views(views, size)
    if r.get("ok") and write:
        r["saved"] = write_intrinsics(r["camera_matrix"], r["dist_coeffs"], cfg_path,
                                      f"one-shot k1={r['k1']} views={r['views']}")
    return r


def make_board_pdf(path, cols=13, rows=9, square_mm=20.0):
    """生成 A4 横版棋盘格 PDF (cols x rows 个方格 -> (cols-1)x(rows-1) 内角点)。"""
    dpi = 300
    W, H = int(297 / 25.4 * dpi), int(210 / 25.4 * dpi)
    img = np.full((H, W), 255, np.uint8)
    s = square_mm / 25.4 * dpi
    x0, y0 = (W - cols * s) / 2, (H - rows * s) / 2
    for r in range(rows):
        for c in range(cols):
            if (r + c) % 2 == 0:
                cv2.rectangle(img, (int(round(x0 + c * s)), int(round(y0 + r * s))),
                              (int(round(x0 + (c + 1) * s)) - 1, int(round(y0 + (r + 1) * s)) - 1), 0, -1)
    from PIL import Image
    Image.fromarray(img).save(path, "PDF", resolution=dpi)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--one", nargs="+", metavar="IMG", help="一键单张校正: 1~几张平铺棋盘格的照片")
    ap.add_argument("--make-board", metavar="PDF", help="生成 A4 打印用棋盘格 PDF")
    ap.add_argument("--images", default="tools/chess_images")
    ap.add_argument("--cols", type=int, default=9)
    ap.add_argument("--rows", type=int, default=6)
    ap.add_argument("--square", type=float, default=20.0)
    a = ap.parse_args()
    if a.make_board:
        print("已生成", make_board_pdf(a.make_board), "(12x8 内角点, 方格 20mm; 打印缩放无所谓)")
        return
    if a.one:
        r = one_shot(a.one)
        if not r.get("ok"):
            raise SystemExit(r.get("error"))
        print(f"k1={r['k1']} k2={r['k2']}  格线直线度 校正前 {r['before_px']}px -> 校正后 {r['after_px']}px, "
              f"画面边角最大修正 {r['max_shift_px']}px  {r['tip']}")
        print("已写入", r["saved"], "(旧内参备份在 prev_intrinsics)")
        return
    r = calibrate(sorted(glob.glob(os.path.join(a.images, "*"))), a.cols, a.rows, a.square)
    if not r["ok"]:
        raise SystemExit(r["error"])
    print(f"RMS 重投影误差 {r['rms']:.3f} px ({r['used']} 张) {r['tip']}")
    print("已写入", r["saved"], "—— 托盘模式下无需其它操作; 点位模式请重新做一次 9 点标定")


if __name__ == "__main__":
    main()
