"""
视觉模块统一配置: vision/vision_config.json  +  环境变量覆盖。

优先级: 环境变量 > vision_config.json > 代码默认值。
赛前只需要改 vision_config.json 一个文件; 临时调试用环境变量, 不改文件。

环境变量 (全部可选):
    IS_VISION_CONFIG     配置文件路径
    IS_VISION_BACKEND    yolo | hsv_color
    IS_CAM_TYPE          uvc | hikrobot | replay | synthetic
    IS_CAM_DEVICE        UVC 设备号或 /dev/videoX
    IS_CAM_SERIAL        工业相机序列号
    IS_CAM_REPLAY        回放路径 (设置后相机类型自动变为 replay)
    IS_CAM_W / IS_CAM_H / IS_CAM_FPS / IS_CAM_FOURCC
    IS_CAM_EXPOSURE      曝光 (微秒)      IS_CAM_GAIN  增益 (dB)     IS_CAM_WB  白平衡色温 (K)
    IS_YOLO_MODEL / IS_YOLO_BACKEND / IS_YOLO_CONF / IS_YOLO_IOU
    IS_PARALLAX_FACTOR   抓取点取物体高度的比例
    IS_SCAN_FRAMES       scan() 默认融合帧数
    IS_VISION_RECORD     1/0  是否保存每次 scan 的原图+标注图
    IS_VISION_RECORD_DIR 保存目录
    IS_COLOR_SOURCE      hsv | model  (A1 颜色来源; 默认 hsv)
    IS_TRAY_WB           1/0         (A6 托盘白基准归一化; 默认关)
    IS_TRAY_WB_GAIN_MIN  托盘白基准增益下限 (A6; 默认 0.7)
    IS_TRAY_WB_GAIN_MAX  托盘白基准增益上限 (A6; 默认 1.4)
    IS_TRAY_WB_MIN_PIXELS 托盘白基准最少白斑像素 (A6; 默认 200, 不足沿用上次增益)
    IS_COMPETITION_LOCK  比赛锁文件路径 (默认 /tmp/is_competition.lock, 见 race_mode())
    IS_REQUIRE_INFO      1/0       (A4 信息维度必填; 默认 0 = 旧行为)
    IS_QR_BUDGET_MS      二维码解码时间预算 (A4; 按整次 scan 计, 默认 500; 0=不解码)
    IS_REQUIRE_INFO_CONF 信息解不出时压到的置信度 (A4; 默认 0.43, 须 < COMMIT_CONF 0.45)
    IS_MATCH_RATIO       多帧融合匹配半径比例 (A5; 默认 0.35)
    IS_MATCH_PX_MIN      多帧融合匹配半径下限 px (A5; 默认 12)
"""
from __future__ import annotations

import copy
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CUR_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(CUR_DIR, "vision_config.json")

DEFAULTS: Dict[str, Any] = {
    "backend": "yolo",
    # A4 信息维度: true 时 category 第三段必须是可解出的信息 (classes.json 的 info / 二维码),
    # 解不出写 "unknown" 并把置信度压到 detect.require_info_conf 触发抖料重扫。默认 false = 旧行为。
    "require_info": False,
    "camera": {
        "type": "uvc", "device": 0, "width": 640, "height": 480, "fps": 30, "fourcc": "MJPG",
        "serial": "", "path": "", "loop": True,
        "exposure_us": None, "gain_db": None, "wb_kelvin": None, "swap_rb": False,
        "stale_s": 2.0,
    },
    "model": {
        "path": "", "backend": "", "conf": 0.40, "iou": 0.50, "max_det": 40,
        "class_conf": {},
    },
    "detect": {
        "refine": True, "decode_qr": True, "parallax_factor": 0.6,
        "min_area_px": 250, "max_area_px": 0, "roi": [], "edge_margin_px": 0,
        # A4 QR 时间预算: 按「整次 scan」计 (不是每件); 0 = 本次 scan 完全不解码
        "qr_budget_ms": 500,
        # A4 require_info=true 且信息解不出时压到的置信度 (须 < COMMIT_CONF 0.45 才会抖料)
        "require_info_conf": 0.43,
    },
    "scan": {"frames": 5, "skip_frames": 1, "min_hit_ratio": 0.5, "match_px": 25,
             # A5 匹配半径自适应: 半径 = max(match_px_min, match_ratio × min(框宽, 框高));
             # match_ratio <= 0 时退回旧的固定 match_px (service.scan() 仍在传该参数)
             "match_ratio": 0.35, "match_px_min": 12,
             "timeout_s": 3.0},
    "record": {"enabled": True, "dir": "records/scans", "keep": 300},
    "color": {
        # A1 颜色来源: hsv(默认, HSV 投票为权威; classes.json 的 model_color 只做冲突检测)
        #              model(应急: 直接用 model_color, 换颜色后失效)
        "source": "hsv",
        # A6 托盘白基准归一化 (默认关; 开启前现场未验证)
        "tray_white_balance": False,
        "wb_gain_min": 0.7, "wb_gain_max": 1.4, "wb_min_pixels": 200,
        # A1 冲突记忆: TTL(秒) + 比赛模式首次冲突压到的置信度 (必须 < COMMIT_CONF 0.45)
        "conflict_ttl_s": 60.0,
        "conflict_conf": 0.43,
    },
}

# 比赛模式的唯一判据 = 锁文件存在 (与 UI/run_competition.py 的 LOCK_FILE 一致)。
# 全仓没有 IS_RACE, 不要新增; 其它模块一律调用 race_mode()。
COMPETITION_LOCK = "/tmp/is_competition.lock"


def competition_lock_path() -> str:
    """锁文件路径。IS_COMPETITION_LOCK 可覆盖 (单测用 tmp 路径, 不必碰 /tmp)。"""
    return os.environ.get("IS_COMPETITION_LOCK") or COMPETITION_LOCK


def race_mode() -> bool:
    """当前是否比赛模式。判据只有锁文件; 找不到/读不了都按调试模式处理。"""
    try:
        return os.path.exists(competition_lock_path())
    except OSError:
        return False


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _num(s: str):
    try:
        return int(s)
    except ValueError:
        return float(s)


@dataclass
class VisionConfig:
    backend: str = "yolo"
    require_info: bool = False       # A4: 信息维度是否必填 (默认 false = 旧行为)
    camera: Dict[str, Any] = field(default_factory=dict)
    model: Dict[str, Any] = field(default_factory=dict)
    detect: Dict[str, Any] = field(default_factory=dict)
    scan: Dict[str, Any] = field(default_factory=dict)
    record: Dict[str, Any] = field(default_factory=dict)
    color: Dict[str, Any] = field(default_factory=dict)
    source: str = ""
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"backend": self.backend, "require_info": self.require_info,
                "camera": self.camera, "model": self.model,
                "detect": self.detect, "scan": self.scan, "record": self.record,
                "color": self.color}


def load_vision_config(path: Optional[str] = None) -> VisionConfig:
    path = path or os.environ.get("IS_VISION_CONFIG") or DEFAULT_CONFIG_PATH
    data: Dict[str, Any] = {}
    warnings: List[str] = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            warnings.append(f"配置文件解析失败, 使用默认值: {path}: {e}")
    merged = _deep_merge(DEFAULTS, data)

    env = os.environ
    cam, mdl, det, scn, rec, col = (merged["camera"], merged["model"], merged["detect"],
                                    merged["scan"], merged["record"], merged["color"])
    if env.get("IS_VISION_BACKEND"):
        merged["backend"] = env["IS_VISION_BACKEND"]
    if env.get("IS_CAM_TYPE"):
        cam["type"] = env["IS_CAM_TYPE"]
    if env.get("IS_CAM_REPLAY"):
        cam["type"], cam["path"] = "replay", env["IS_CAM_REPLAY"]
    if env.get("IS_CAM_DEVICE"):
        d = env["IS_CAM_DEVICE"]
        cam["device"] = int(d) if d.isdigit() else d
    for key, name in (("IS_CAM_W", "width"), ("IS_CAM_H", "height"), ("IS_CAM_FPS", "fps"),
                      ("IS_CAM_EXPOSURE", "exposure_us"), ("IS_CAM_GAIN", "gain_db"),
                      ("IS_CAM_WB", "wb_kelvin")):
        if env.get(key):
            cam[name] = _num(env[key])
    for key, name in (("IS_CAM_FOURCC", "fourcc"), ("IS_CAM_SERIAL", "serial")):
        if env.get(key):
            cam[name] = env[key]
    for key, name in (("IS_YOLO_MODEL", "path"), ("IS_YOLO_BACKEND", "backend")):
        if env.get(key):
            mdl[name] = env[key]
    for key, name in (("IS_YOLO_CONF", "conf"), ("IS_YOLO_IOU", "iou")):
        if env.get(key):
            mdl[name] = float(env[key])
    if env.get("IS_PARALLAX_FACTOR"):
        det["parallax_factor"] = float(env["IS_PARALLAX_FACTOR"])
    if env.get("IS_SCAN_FRAMES"):
        scn["frames"] = int(env["IS_SCAN_FRAMES"])
    if env.get("IS_VISION_RECORD"):
        rec["enabled"] = env["IS_VISION_RECORD"] not in ("0", "false", "False", "")
    if env.get("IS_VISION_RECORD_DIR"):
        rec["dir"] = env["IS_VISION_RECORD_DIR"]
    if env.get("IS_COLOR_SOURCE"):
        col["source"] = env["IS_COLOR_SOURCE"].strip().lower()
    if env.get("IS_TRAY_WB"):
        col["tray_white_balance"] = env["IS_TRAY_WB"] not in ("0", "false", "False", "")
    if env.get("IS_TRAY_WB_GAIN_MIN"):
        col["wb_gain_min"] = _num(env["IS_TRAY_WB_GAIN_MIN"])
    if env.get("IS_TRAY_WB_GAIN_MAX"):
        col["wb_gain_max"] = _num(env["IS_TRAY_WB_GAIN_MAX"])
    if env.get("IS_TRAY_WB_MIN_PIXELS"):
        col["wb_min_pixels"] = int(float(env["IS_TRAY_WB_MIN_PIXELS"]))
    if env.get("IS_REQUIRE_INFO"):
        merged["require_info"] = env["IS_REQUIRE_INFO"] not in ("0", "false", "False", "")
    if env.get("IS_QR_BUDGET_MS"):
        det["qr_budget_ms"] = _num(env["IS_QR_BUDGET_MS"])
    if env.get("IS_REQUIRE_INFO_CONF"):
        det["require_info_conf"] = float(env["IS_REQUIRE_INFO_CONF"])
    if env.get("IS_MATCH_RATIO"):
        scn["match_ratio"] = _num(env["IS_MATCH_RATIO"])
    if env.get("IS_MATCH_PX_MIN"):
        scn["match_px_min"] = _num(env["IS_MATCH_PX_MIN"])

    # 基本校验
    if not 0.05 <= float(mdl["conf"]) <= 0.95:
        warnings.append(f"model.conf={mdl['conf']} 不合理, 改为 0.40")
        mdl["conf"] = 0.40
    if int(scn["frames"]) < 1:
        scn["frames"] = 1
    if col.get("source") not in ("hsv", "model"):
        warnings.append(f"color.source={col.get('source')!r} 无效 (只能是 hsv|model), 改为 hsv")
        col["source"] = "hsv"
    if float(col.get("wb_gain_min", 0.7) or 0) > float(col.get("wb_gain_max", 1.4) or 0):
        warnings.append(f"color.wb_gain_min ({col.get('wb_gain_min')}) > color.wb_gain_max "
                        f"({col.get('wb_gain_max')}): 托盘白基准增益限幅写反了, 归一化不会生效")
    if float(det.get("qr_budget_ms", 500) or 0) < 0:
        warnings.append("detect.qr_budget_ms 不能为负, 改为 0 (本次 scan 不解码)")
        det["qr_budget_ms"] = 0
    if float(det.get("require_info_conf", 0.43) or 0) >= 0.45:
        warnings.append("detect.require_info_conf >= COMMIT_CONF(0.45): 信息解不出时不会触发抖料重扫")
    roi = det.get("roi") or []
    if roi and (len(roi) < 3 or any(len(p) != 2 for p in roi)):
        warnings.append("detect.roi 需要 >=3 个 [u,v] 点, 已忽略")
        det["roi"] = []
    for w in warnings:
        logger.warning("视觉配置: %s", w)
    return VisionConfig(backend=merged["backend"],
                        require_info=bool(merged.get("require_info", False)),
                        camera=cam, model=mdl, detect=det,
                        scan=scn, record=rec, color=col,
                        source=path if os.path.exists(path) else "(默认值)",
                        warnings=warnings)
