"""
颜色标定: 由现场采样点自动生成 classes.json 的 hsv 区间 (网页调试台「颜色」页使用)。

用法: 在调试台上依次点击每种颜色的货物 (每种 2~5 个点, 不同位置/朝向), 选颜色名, 点「生成」。
    彩色 (红/橙/黄/绿/浅蓝/蓝/紫...): 按色相中心排序, 相邻两色的中点作为分界 -> hue_bins
    白/黑/灰:                         按采样的 S/V 生成 white_s_max / white_v_min / black_v_max
    浅蓝与蓝色相接近时:                用饱和度区分 -> light_blue_s_max
换场地灯光后 1 分钟重标即可; 也是模型只认形状、颜色交给 HSV 的方案能应对决赛换颜色的原因。
"""
from __future__ import annotations

import copy
from typing import Dict, List

import numpy as np

ACHROMATIC = ("white", "black", "gray")


def sample_hsv(bgr: np.ndarray, u: float, v: float, r: int = 4) -> Dict[str, int]:
    import cv2
    h, w = bgr.shape[:2]
    x, y = int(round(u)), int(round(v))
    patch = bgr[max(0, y - r):min(h, y + r + 1), max(0, x - r):min(w, x + r + 1)]
    if patch.size == 0:
        return {}
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(float)
    # 色相按圆周取中值 (红色跨 0/180)
    ang = hsv[:, 0] * np.pi / 90.0
    hue = (np.degrees(np.arctan2(np.sin(ang).mean(), np.cos(ang).mean())) / 2.0) % 180
    return {"h": int(round(hue)) % 180, "s": int(np.median(hsv[:, 1])), "v": int(np.median(hsv[:, 2]))}


def _circ_mean(hs: List[int]) -> float:
    ang = np.array(hs, float) * np.pi / 90.0
    return (np.degrees(np.arctan2(np.sin(ang).mean(), np.cos(ang).mean())) / 2.0) % 180


def solve_hsv(samples: List[dict], base: dict) -> dict:
    """samples: [{"color": "red", "h":.., "s":.., "v":..}, ...]  base: 现有 classes.json 的 hsv。返回新 hsv。"""
    out = copy.deepcopy(base or {})
    by: Dict[str, List[dict]] = {}
    for s in samples:
        by.setdefault(s["color"], []).append(s)
    chrom = {c: v for c, v in by.items() if c not in ACHROMATIC}
    if len(chrom) < 2:
        raise ValueError("至少需要 2 种彩色的采样点")
    for c, v in chrom.items():
        low = [p for p in v if p["s"] < 40]
        if low:
            raise ValueError(f"{c} 有 {len(low)} 个采样点饱和度很低 (S<40), 像是点到了背景/反光, 请清空重采")

    centers = {c: _circ_mean([p["h"] for p in v]) for c, v in chrom.items()}
    # 浅蓝/蓝 色相太近时: 浅蓝不单独占色相段, 用饱和度区分
    if "light_blue" in centers and "blue" in centers:
        d = abs(centers["light_blue"] - centers["blue"])
        s_lb = np.median([p["s"] for p in chrom["light_blue"]])
        s_b = np.median([p["s"] for p in chrom["blue"]])
        out["light_blue_s_max"] = int(round((s_lb + s_b) / 2))
        if min(d, 180 - d) < 10:
            centers.pop("light_blue")
    order = sorted(centers.items(), key=lambda kv: kv[1])
    for (n1, c1), (n2, c2) in zip(order, order[1:] + order[:1]):
        d = (c2 - c1) % 180
        if n1 != n2 and min(d, 180 - d) < 5:
            raise ValueError(f"{n1} 与 {n2} 的色相几乎相同 ({c1:.0f} / {c2:.0f}), 相机无法区分, 检查白平衡或采样")
    bins = []
    n = len(order)
    for i, (name, c) in enumerate(order):
        prev_c = order[i - 1][1] - (180 if i == 0 else 0)
        next_c = order[(i + 1) % n][1] + (180 if i == n - 1 else 0)
        lo = int(np.floor((prev_c + c) / 2.0)) + 1
        hi = int(np.floor((c + next_c) / 2.0))
        if lo < 0:                                   # 跨 0: 拆成两段
            bins.append([name, lo + 180, 179])
            bins.append([name, 0, hi])
        elif hi > 179:
            bins.append([name, lo, 179])
            bins.append([name, 0, hi - 180])
        else:
            bins.append([name, lo, hi])
    out["hue_bins"] = sorted(bins, key=lambda b: b[1])

    min_chrom_s = min(p["s"] for v in chrom.values() for p in v)
    if "white" in by:
        out["white_s_max"] = int(np.clip(max(p["s"] for p in by["white"]) + 15, 15, 120))
        out["white_v_min"] = int(np.clip(min(p["v"] for p in by["white"]) - 25, 80, 230))
    out["white_s_max"] = int(min(out.get("white_s_max", 45), max(10, min_chrom_s - 8)))
    if "black" in by:
        out["black_v_max"] = int(np.clip(max(p["v"] for p in by["black"]) + 15, 20, 120))
    out["_标定"] = f"{len(samples)} 个采样点自动生成"
    return out
