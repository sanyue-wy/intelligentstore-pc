#!/usr/bin/env python3
"""
相机内参 / 畸变标定 (棋盘格), 结果写入 vision/calib_config.json 的
camera_matrix / dist_coeffs, CoordinateCalibrator 会自动做畸变校正。

推荐在浏览器里做: python3 tools/vision_tool.py web -> 「畸变」页, 边看画面边拍。
命令行: 打印 9x6 内角点棋盘格 (方格 20mm), 在托盘区域各位置/倾角拍 15~25 张
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", default="tools/chess_images")
    ap.add_argument("--cols", type=int, default=9)
    ap.add_argument("--rows", type=int, default=6)
    ap.add_argument("--square", type=float, default=20.0)
    a = ap.parse_args()
    r = calibrate(sorted(glob.glob(os.path.join(a.images, "*"))), a.cols, a.rows, a.square)
    if not r["ok"]:
        raise SystemExit(r["error"])
    print(f"RMS 重投影误差 {r['rms']:.3f} px ({r['used']} 张) {r['tip']}")
    print("已写入", r["saved"], "—— 托盘模式下无需其它操作; 点位模式请重新做一次 9 点标定")


if __name__ == "__main__":
    main()
