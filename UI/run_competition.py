# UI/run_competition.py
# -*- coding: utf-8 -*-
"""
比赛模式入口 —— 开机即待机, 一键启动, 全自动分拣 (2027 工创 · 智能分拣)

生命周期:
    启动 -> 连接下位机 / 打开相机 / 加载模型 (预热) -> 赛前自检 -> 【待机】
    -> 一键启动 (USB 键 / GPIO 键 / 执行页「开始」/ 终端回车) -> 【分拣中】 -> 【已完成】
    -> 自动回到待机, 等第二次运行 (盒子映射/计数自动清零)
    执行页 http://<X5>:8088 : 状态/流程/错误/自检/相机/大屏预览 + 开始/停止本轮/急停/一键重启

每件一个闭环:
    多帧扫描托盘 -> 选最好抓的一件 -> 风险评估 (置信度低/叠放 -> 先抖料重扫, 最多 2 次)
    -> 查/分配储物盒 -> 下位机闭环抓放 -> 成功: 大屏显示 序号/名称/图片/总数 (推送完成才继续)
    失败: 记下位置换一件, 多次失败抖料;  连续空盘 (抖料后仍为空) 判定完成

赛规保护 (PDF 第 10~12 页):
    * 15 秒无动作或无显示判负 -> 动作看门狗: 距上次动作超过 IDLE_GUARD_S (默认 9s) 时先抖料;
      单件超时 SORT_TIMEOUT_S=12s
    * 每盒最多 4 件 / 最多 6 类 -> 超出时执行页告警 (几乎肯定识别错了)
    * 运行中不碰电脑 -> 物理启动键; 比赛模式自动关闭网页调试台 (start_competition.sh)

常用环境变量:
    IS_VISION_BACKEND=yolo     视觉后端 (默认 yolo, 无模型时自动退回 HSV)
    STM_PORT=/dev/ttyACM0      串口 (缺省自动发现)
    ALLOW_MOCK=1               串口打不开时用仿真下位机 (仅联调! 比赛勿开)
    USE_SYNTHETIC_CAM=1        用仿真相机 (仅联调)
    SCAN_FRAMES=5              每次扫描融合帧数
    EXPECTED_TOTAL=12          已知货物总数时填写, 达到即结束 (省去最后的空盘确认)
    START_SOURCES=web,key,gpio,enter   启动来源 (见 UI/start_trigger.py); auto = 不等待
    START_GPIO_PIN=37          GPIO 启动键 (BOARD 编号)
    COMMIT_CONF=0.45           低于此置信度先抖料重扫
    PREFETCH_STAGE=0           机械臂到达该阶段 (4=去放置位 5=放置) 时提前扫描下一件; 0=关闭
                               先确认该阶段机械臂已离开相机视野再开, 每件约省 0.3~0.5s
    IDLE_GUARD_S=9             动作看门狗阈值
    ONESHOT=1                  跑完一轮就退出 (不回待机)
    IS_RUN_PAGE_PORT=8088      执行页端口 (0 = 关闭)
"""

import collections
import io
import logging
import math
import os
import sys
import threading
import time

CUR_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(CUR_DIR)
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from communication import create_comm_client
from vision import create_vision_service
from UI.planner import SorterPlanner, display_name
from UI.controller import CompetitionController
from UI.display_ui import CompetitionUI
from UI.start_trigger import StartTrigger
from UI.run_page import RunPage
from UI.run_report import RunReport
from UI.precheck import run_precheck, summarize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("competition")

SHOT_DIR = os.environ.get("SORT_SHOT_DIR", "/tmp/sort_shots")
SCAN_FRAMES = int(os.environ.get("SCAN_FRAMES", 0)) or None        # None = vision_config.json
EXPECTED_TOTAL = int(os.environ.get("EXPECTED_TOTAL", 0))
MAX_EMPTY_SHAKES = int(os.environ.get("MAX_EMPTY_SHAKES", 2))
MAX_FAILS_BEFORE_SHAKE = 3
# 失败上限 (防止最后一件卡在不可达位置时无限循环, 抖料又骗过 15s 判负)
MAX_FAILS_PER_SPOT = int(os.environ.get("MAX_FAILS_PER_SPOT", 3))      # 同一位置失败几次后永久跳过
MAX_CONSEC_FAILS = int(os.environ.get("MAX_CONSEC_FAILS", 8))          # 连续失败 (中间无成功) 几次结束本轮
MAX_ROUND_FAILS = int(os.environ.get("MAX_ROUND_FAILS", 20))           # 本轮累计失败几次结束本轮
MAX_BLOCKED_SHAKES = int(os.environ.get("MAX_BLOCKED_SHAKES", 2))      # 剩下的全是被跳过的件时抖料几次后结束
SPOT_RADIUS_PX = 25.0
MAX_UNCERTAIN_RETRY = int(os.environ.get("MAX_UNCERTAIN_RETRY", 2))
COMMIT_CONF = float(os.environ.get("COMMIT_CONF", 0.45))
PREFETCH_STAGE = int(os.environ.get("PREFETCH_STAGE", 0))
IDLE_GUARD_S = float(os.environ.get("IDLE_GUARD_S", 9.0))
ONESHOT = os.environ.get("ONESHOT", "0") == "1"
RUN_PAGE_PORT = int(os.environ.get("IS_RUN_PAGE_PORT", 8088))
LOCK_FILE = "/tmp/is_competition.lock"
EXIT_RESTART = 75          # start_competition.sh 看到这个退出码就重启程序


def _save_crop(frame, target, idx: int) -> str:
    import cv2
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        h, w = frame.shape[:2]
        if target.bbox:
            x1, y1, x2, y2 = [int(v) for v in target.bbox]
        else:
            cx, cy, r = int(target.u_pixel), int(target.v_pixel), 60
            x1, y1, x2, y2 = cx - r, cy - r, cx + r, cy + r
        pad = 20
        x1, y1, x2, y2 = max(0, x1 - pad), max(0, y1 - pad), min(w, x2 + pad), min(h, y2 + pad)
        if x2 <= x1 or y2 <= y1:
            return ""
        path = os.path.join(SHOT_DIR, f"goods_{idx:03d}.jpg")
        cv2.imwrite(path, frame[y1:y2, x1:x2])
        return path
    except Exception:
        return ""


class _EventHandler(logging.Handler):
    """把各模块的 WARNING/ERROR 日志同步到执行页的「流程/错误」栏。"""

    def __init__(self, app):
        super().__init__(logging.WARNING)
        self.app = app

    def emit(self, record):
        if record.name == "competition":       # event() 自己记的日志, 不重复
            return
        try:
            self.app.event(f"{record.name}: {record.getMessage()}",
                           "error" if record.levelno >= logging.ERROR else "warn", log_it=False)
        except Exception:
            pass



def _count_spot_fail(spot_fails: list, u: float, v: float, radius: float) -> int:
    """同一位置 (半径 radius 像素内) 的失败次数 +1, 返回累计次数。"""
    for rec in spot_fails:
        if math.hypot(rec[0] - u, rec[1] - v) < radius:
            rec[2] += 1
            return rec[2]
    spot_fails.append([u, v, 1])
    return 1

class CompetitionApp:
    def __init__(self):
        self.state = "checking"          # checking | standby | running | finished | error | restarting
        self.hint = ""
        self.t_round = 0.0
        self.elapsed_s = 0.0
        self.total_done = 0
        self.items = []
        self.events = collections.deque(maxlen=300)
        self.t0 = time.time()
        self.precheck = []
        self.scan_n = 0
        self.exit_code = 0
        self.quit = threading.Event()
        self.stop_round = threading.Event()
        self.planner = None
        self.report = None
        self._jpg_cache = (-1, None)
        self._png_cache = (None, None)

        self.client = create_comm_client()
        self.vision = None
        self.ui = CompetitionUI()
        self.trigger = StartTrigger()
        self.controller = CompetitionController(self.client)
        logging.getLogger().addHandler(_EventHandler(self))

    # ================================================================== 执行页接口
    def event(self, msg, level="info", log_it=True):
        self.events.append({"t": round(time.time() - self.t0, 1), "level": level, "msg": msg})
        if log_it:
            log.log({"info": logging.INFO, "warn": logging.WARNING}.get(level, logging.ERROR), msg)
        if self.report is not None and level != "info":
            self.report.event(msg, level)

    def state_dict(self):
        if self.state == "running":
            self.elapsed_s = time.time() - self.t_round
        bad = [i for i in self.precheck if i["level"] == "error"]
        st = self.ui.state
        return {"state": self.state, "hint": self.hint, "elapsed_s": round(self.elapsed_s, 1),
                "total_done": self.total_done, "items": self.items[-40:], "events": list(self.events)[-80:],
                "precheck": self.precheck, "precheck_ok": not bad,
                "precheck_summary": summarize(self.precheck) if self.precheck else "",
                "slots": self.planner.slots_summary() if self.planner else {},
                "warnings": list(self.planner.warnings) if self.planner else [],
                "screen_seq": f"{st['seq']}|{st['stage']}|{st['name']}", "scan_n": self.scan_n,
                "start_sources": self.trigger.describe()}

    def frame_jpeg(self):
        if self._jpg_cache[0] == self.scan_n and self._jpg_cache[1]:
            return self._jpg_cache[1]
        if self.vision is None:
            return None
        import cv2
        img, _ = self.vision.get_latest_frame_and_targets()
        if img is None:
            return None
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            return None
        self._jpg_cache = (self.scan_n, jpg.tobytes())
        return self._jpg_cache[1]

    def screen_png(self):
        st = self.ui.state
        key = (st["seq"], st["name"], st["total"], st["stage"], st["image_path"])
        if self._png_cache[0] == key:
            return self._png_cache[1]
        try:
            buf = io.BytesIO()
            self.ui.render().save(buf, "PNG")
            self._png_cache = (key, buf.getvalue())
            return self._png_cache[1]
        except Exception:
            return None

    def request(self, action):
        if action == "start":
            if self.state not in ("standby", "finished"):
                return False, f"当前状态 {self.state}, 不能开始"
            return (True, "已开始") if self.trigger.fire("web") else (False, "未就绪, 稍后再按")
        if action == "stop":
            if self.state != "running":
                return False, "不在运行中"
            self.stop_round.set()
            self.controller.cancel_flag = True
            self.event("执行页: 停止本轮", "warn")
            return True, "正在停止 (当前动作结束后)"
        if action == "estop":
            try:
                self.client.send_estop()
            except Exception as e:
                return False, f"急停发送失败: {e}"
            self.stop_round.set()
            self.controller.cancel_flag = True
            self.event("执行页: 急停", "error")
            return True, "已急停。排除问题后点「一键重启」"
        if action == "restart":
            self.event("执行页: 一键重启", "warn")
            self.exit_code = EXIT_RESTART
            self.state = "restarting"
            self.stop_round.set()
            self.controller.cancel_flag = True
            self.quit.set()
            return True, "程序重启中, 约 10 秒后回到待机"
        if action == "precheck":
            threading.Thread(target=self.refresh_precheck, daemon=True).start()
            return True, "自检刷新中"
        return False, f"未知操作 {action}"

    def refresh_precheck(self):
        try:
            self.precheck = run_precheck(self.vision, self.client, self.ui, self.trigger)
        except Exception as e:
            self.precheck = [{"name": "自检", "ok": False, "detail": str(e), "level": "error"}]
        return self.precheck

    # ================================================================== 启动
    def _connect(self) -> bool:
        port = os.environ.get("STM_PORT") or None          # None -> 自动发现
        for attempt in range(1, 4):
            try:
                if self.client.connect(port, 115200):
                    return True
            except Exception as exc:
                log.warning("串口连接异常 (%s)", exc)
            log.warning("串口连接失败, %d/3 次", attempt)
            time.sleep(1.5)
        if os.environ.get("ALLOW_MOCK") == "1":
            self.event("改用仿真下位机 (ALLOW_MOCK=1, 仅联调)", "warn")
            return self.client.connect_mock()
        return False

    def boot(self):
        self.ui.reset("启动中")
        self.trigger.start()
        page = RunPage(self, RUN_PAGE_PORT) if RUN_PAGE_PORT else None
        if page:
            page.start()
        self.page = page

        self.vision = create_vision_service(backend=os.environ.get("IS_VISION_BACKEND", "yolo"),
                                            continuous_detect=False)
        self.vision.set_camera_source(use_synthetic=os.environ.get("USE_SYNTHETIC_CAM", "0") == "1",
                                      device_id=0)
        self.vision.start()
        det = self.vision.detector
        self.event("视觉: " + (det.engine.describe() if hasattr(det, "engine") else "HSV 颜色检测"))
        # 预热: 第一次推理 (BPU 加载/内存分配) 放在待机前, 开赛第一件不慢
        try:
            fr = self.vision.get_fresh_frames(1, timeout_s=3.0)
            if fr:
                t = time.perf_counter()
                self.vision.detect_now(fr[0])
                self.event(f"模型预热完成 {(time.perf_counter() - t) * 1e3:.0f} ms")
        except Exception:
            pass

        while not self._connect():
            self.state, self.hint = "error", "下位机未连接 (检查 USB 线/下位机电源), 每 3 秒自动重试"
            self.ui.reset("下位机未连接")
            self.refresh_precheck()
            if self.quit.wait(3.0):
                return False
        try:
            self.client.send_clear()
        except Exception:
            pass
        self.event(f"下位机: {getattr(self.client, 'link_description', '')}")
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
        return True

    # ================================================================== 主循环
    def run(self) -> int:
        try:
            if not self.boot():
                return self.exit_code
            last = ""
            while not self.quit.is_set():
                if not last:
                    self.state = "checking"
                self.refresh_precheck()
                bad = [i["name"] for i in self.precheck if i["level"] == "error"]
                self.state = "finished" if last else "standby"
                self.hint = (last + " " if last else "") + \
                    (f"自检未通过: {'、'.join(bad)} —— 仍可开始。" if bad else "就绪。") + \
                    f"启动方式: {self.trigger.describe()}"
                if not last:                      # 刚比完保持大屏上的最终结果, 供裁判查看
                    self.ui.reset("自检异常" if bad else "就绪")
                self.event("待机, 等待一键启动 (" + self.trigger.describe() + ")")
                self.trigger.arm()
                if not self.trigger.wait(stop=self.quit):
                    break
                last = self.run_round()
                if ONESHOT:
                    break
                self.quit.wait(float(os.environ.get("FINAL_HOLD_S", "3")))
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return self.exit_code

    def run_round(self):
        self.stop_round.clear()
        self.controller.cancel_flag = False
        self.planner = planner = SorterPlanner()
        planner.commit_conf = COMMIT_CONF
        self.items, self.total_done = [], 0
        eng = getattr(self.vision.detector, "engine", None)
        self.report = report = RunReport(model=eng.describe() if eng else "HSV")
        self.state, self.hint = "running", f"由 {self.trigger.fired_by} 启动, 双手离开设备"
        self.t_round = time.time()
        self.controller.last_motion = time.time()
        self.event(f"=== 开始分拣 ({self.trigger.fired_by}) ===")
        self.ui.update_status(0, "开始分拣", 0, stage="分拣中")

        result = "完成"
        empty_rounds = uncertain = 0
        fail_spots = []
        spot_fails = []                 # [[u, v, 次数]] 同一位置累计失败 (抖料后货物没动也算同一位置)
        blocked = []                    # 永久跳过的位置
        consec_fails = round_fails = blocked_shakes = 0
        prefetched = None
        screen_warned = False
        try:
            while not self.stop_round.is_set():
                if EXPECTED_TOTAL and self.total_done >= EXPECTED_TOTAL:
                    break
                # ---------- 动作看门狗 (赛规 15s 无动作判负)
                if time.time() - self.controller.last_motion > IDLE_GUARD_S:
                    self.event(f"动作看门狗: {IDLE_GUARD_S:.0f}s 无动作, 抖料保持动作", "warn")
                    self.controller.shake_tray()

                if prefetched is not None:
                    targets, frame = prefetched
                    prefetched = None
                else:
                    targets, frame = self.vision.scan(SCAN_FRAMES)
                self.scan_n += 1
                ann, _ = self.vision.get_latest_frame_and_targets()
                scan_img = report.save_scan_image(ann, self.scan_n, raw=frame, targets=targets)

                if not targets:
                    empty_rounds += 1
                    if empty_rounds > MAX_EMPTY_SHAKES:
                        break
                    self.event(f"托盘无可识别货物, 抖料后重扫 ({empty_rounds}/{MAX_EMPTY_SHAKES})")
                    self.controller.shake_tray()
                    continue
                empty_rounds = 0

                t = planner.pick_next(targets, skip=fail_spots, blocked=blocked, blocked_radius_px=SPOT_RADIUS_PX)
                if t is None:
                    blocked_shakes += 1
                    if blocked_shakes > MAX_BLOCKED_SHAKES:
                        result = "剩余货物无法抓取"
                        self.event(f"剩下 {len(targets)} 件都在反复失败的位置, 抖料 {MAX_BLOCKED_SHAKES} 次仍无变化, 结束本轮", "error")
                        break
                    self.event(f"剩下的货物都在反复失败的位置, 抖料尝试挪动 ({blocked_shakes}/{MAX_BLOCKED_SHAKES})", "warn")
                    blocked.clear()             # 抖料后货物可能已挪位, 给一次机会; 仍在原地会很快再次被跳过
                    self.controller.shake_tray()
                    continue
                why = planner.assess(t, targets)
                if why and uncertain < MAX_UNCERTAIN_RETRY:
                    uncertain += 1
                    self.event(f"暂不抓 {display_name(t)}: {why}, 抖料重扫 ({uncertain}/{MAX_UNCERTAIN_RETRY})")
                    self.controller.shake_tray()
                    continue
                if why:
                    self.event(f"{why}, 已重扫 {uncertain} 次, 仍然抓取", "warn")
                uncertain = 0

                name = display_name(t)
                slot = planner.slot_for(t)
                seq = self.total_done + 1
                shot = _save_crop(frame, t, seq)
                self.event(f"抓取 {name} conf={t.confidence:.2f} mm=({t.x_mm:.1f},{t.y_mm:.1f}) -> {slot + 1} 号盒")

                pre = {"thread": None, "result": None}
                on_stage = None
                if PREFETCH_STAGE > 0:
                    def on_stage(stage, pre=pre):
                        if pre["thread"] is None and PREFETCH_STAGE <= stage < 7:
                            def job():
                                pre["result"] = self.vision.scan(SCAN_FRAMES)
                            pre["thread"] = threading.Thread(target=job, daemon=True)
                            pre["thread"].start()

                res = self.controller.sort_one(t.x_mm, t.y_mm, slot, on_stage=on_stage)
                item = dict(seq=seq if res.success else None, label=name, category=t.category,
                            conf=t.confidence, slot=slot + 1, u=t.u_pixel, v=t.v_pixel,
                            x_mm=t.x_mm, y_mm=t.y_mm, elapsed_s=round(res.elapsed_s, 2), scan=scan_img,
                            result="ok" if res.success else ("estop" if res.fatal else
                                                             "grip_fail" if res.grip_fail else "fail"))
                if res.success:
                    self.total_done += 1
                    planner.mark_done(t)
                    fail_spots.clear()
                    consec_fails = blocked_shakes = 0
                    self.ui.update_status(seq=self.total_done, name=name, total=self.total_done,
                                          image_path=shot, stage="分拣中")
                    if not self.ui.flush(1.0) and not screen_warned:
                        screen_warned = True
                        self.event("大屏推送未确认 (大屏服务 :8756 不可达? 本轮不再重复提示)", "warn")
                    item["shot"] = shot
                    self.event(f"✔ 第 {self.total_done} 件 {name} 完成, 用时 {res.elapsed_s:.1f}s")
                    if pre["thread"] is not None:
                        pre["thread"].join(timeout=4.0)
                        r = pre["result"]
                        if r is not None and r[1] is not None:
                            prefetched = r
                elif res.fatal:
                    result = "急停"
                    self.event("急停, 结束本轮", "error")
                    self.items.append(report.item(**item))
                    break
                else:
                    self.event(f"✘ {name} 失败: {res.message}", "warn")
                    consec_fails += 1
                    round_fails += 1
                    if not getattr(res, "slot_fail", False):
                        fail_spots.append((t.u_pixel, t.v_pixel))
                        n = _count_spot_fail(spot_fails, t.u_pixel, t.v_pixel, SPOT_RADIUS_PX)
                        if n >= MAX_FAILS_PER_SPOT:
                            blocked.append((t.u_pixel, t.v_pixel))
                            self.event(f"{name} 在同一位置失败 {n} 次, 本轮不再抓这里", "warn")
                    if pre["thread"] is not None:
                        pre["thread"].join(timeout=4.0)
                    if consec_fails >= MAX_CONSEC_FAILS or round_fails >= MAX_ROUND_FAILS:
                        result = "失败过多"
                        self.event(f"连续失败 {consec_fails} 次 / 本轮累计 {round_fails} 次, 结束本轮 "
                                   "(检查下位机、标定或夹爪)", "error")
                        self.items.append(report.item(**item))
                        break
                    if len(fail_spots) >= MAX_FAILS_BEFORE_SHAKE:
                        self.controller.shake_tray()
                        fail_spots.clear()
                self.items.append(report.item(**item))
                for w in planner.warnings:
                    report.warn_once(w)
            if self.stop_round.is_set() and result == "完成":
                result = "停止"
        except Exception as e:
            result = "异常"
            log.exception("比赛流程异常")
            self.event(f"流程异常: {e}", "error")
        self.elapsed_s = time.time() - self.t_round
        self.event(f"=== 分拣结束 ({result}): 成功 {self.total_done} 件, 用时 {self.elapsed_s:.1f}s ===")
        for k, v in planner.slots_summary().items():
            log.info("  %s 号盒: %s x%d", k, v["label"], v["count"])
        report.finish(result, self.total_done, planner.slots_summary())
        self.ui.update_status(seq=self.total_done, name="全部完成" if result == "完成" else f"本轮{result}",
                              total=self.total_done, stage="已完成")
        self.state = "finished"
        self.hint = f"本轮{result}: {self.total_done} 件 {self.elapsed_s:.1f}s。再次一键启动开始下一轮 (盒子与计数清零)。"
        return self.hint

    def shutdown(self):
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
        if getattr(self, "page", None):
            self.page.stop()
        self.ui.close()
        if self.vision is not None:
            self.vision.stop()
        try:
            self.client.disconnect()
        except Exception:
            pass


def main():
    log.info("=== 比赛模式启动 ===")
    app = CompetitionApp()
    code = app.run()
    sys.exit(code)


if __name__ == "__main__":
    main()
