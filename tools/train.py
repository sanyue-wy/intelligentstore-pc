#!/usr/bin/env python3
"""
YOLOv8 训练 (PC / 云 GPU 上运行) —— 针对分拣货物的超参

识别率主要取决于数据, 不是网络:
  * 用比赛同款相机、同高度、同灯光拍; 每类 >= 150 个实例, 覆盖各种朝向、倒伏、贴边、相互挨着;
  * 背景放进托盘本身 + 几张空托盘图 (无标注) 抑制误检;
  * 验证集要用"另一次重新摆放"拍的图, 不要从同一批连拍里随机切, 否则指标虚高。
超参要点:
  * hsv_h=0: 颜色交给 HSV 判定时, 色相增强无害但没必要; 若类别里含颜色, 必须为 0,
    否则会把红/橙、蓝/浅蓝互相"增强"成对方, 直接毁掉颜色区分;
  * degrees=180 / flipud: 货物在托盘里方向完全随机;
  * yolov8s 比 yolov8n 小目标识别明显更好, X5 BPU 上 480x640 推理仍只要十几 ms,
    分拣节拍是秒级, 推理不是瓶颈 —— 优先保精度。
"""
import os

from ultralytics import YOLO

HERE = os.path.dirname(os.path.abspath(__file__))

if __name__ == "__main__":
    model = YOLO("yolov8s.pt")
    model.train(
        data=os.path.join(HERE, "data.yaml"), imgsz=640, epochs=200, batch=16, patience=50,
        hsv_h=0.0, hsv_s=0.35, hsv_v=0.35,
        degrees=180, translate=0.1, scale=0.3, shear=0.0, perspective=0.0005,
        flipud=0.5, fliplr=0.5, mosaic=1.0, close_mosaic=20, mixup=0.0,
        cos_lr=True, seed=0,
    )
    metrics = model.val()
    print(metrics)
    print("下一步: python3 tools/convert_model.py", os.path.join(str(model.trainer.save_dir), "weights", "best.pt"))
