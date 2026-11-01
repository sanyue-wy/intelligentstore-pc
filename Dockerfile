# intelligentstoring — STM32 智能分拣系统上位机 (RDK X5 / arm64 / Ubuntu 22.04 / Python 3.10)
# 板子上不需要 build: provision_intelligentstoring.sh 直接用 ubuntu:22.04 并在容器里装同样的依赖。
# 板子不能联网时, 在笔记本 WSL 里:
#   docker buildx build --platform linux/arm64 -t is-x5:22.04 . && docker save is-x5:22.04 | gzip > is.tgz
#   拷到板子: docker load < is.tgz && IMAGE=is-x5:22.04 ./is.sh migrate
# BPU 运行库不打进镜像, 运行时由 deploy/bpu_bridge.sh 从宿主只读挂载 (随宿主系统版本)。
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    DISPLAY=:0

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        python3 python3-pip python3-tk python3-dev ca-certificates libgl1 libglib2.0-0 \
        openssh-client xauth x11-utils x11-xserver-utils procps \
        fonts-wqy-zenhei fonts-wqy-microhei fontconfig \
    && rm -rf /var/lib/apt/lists/*

# numpy 必须 < 2 (宿主 hobot_dnn 按 numpy 1.x 编译); opencv 4.11 是最后支持 numpy 1.x 的版本
# 版本与根目录 requirements.txt / requirements-export.txt 一致
RUN printf 'numpy==1.26.4\nopencv-python==4.11.0.86\nopencv-python-headless==4.11.0.86\nultralytics<9\nonnx<1.19\nonnxruntime<1.23\n' > /etc/pip-constraints.txt \
    && pip install --no-cache-dir -c /etc/pip-constraints.txt numpy opencv-python pillow psutil pyserial onnxruntime \
    && pip install --no-cache-dir -c /etc/pip-constraints.txt --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install --no-cache-dir -c /etc/pip-constraints.txt ultralytics onnx

# 项目源码硬编码 "Segoe UI"(Windows 字体) -> 映射到文泉驿微米黑
RUN printf '%s\n' \
      '<?xml version="1.0"?>' \
      '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">' \
      '<fontconfig>' \
      '  <alias binding="same"><family>Segoe UI</family><prefer><family>WenQuanYi Micro Hei</family></prefer></alias>' \
      '  <match target="pattern">' \
      '    <test qual="any" name="family"><string>Segoe UI</string></test>' \
      '    <edit name="family" mode="assign" binding="strong"><string>WenQuanYi Micro Hei</string></edit>' \
      '  </match>' \
      '</fontconfig>' \
      > /etc/fonts/conf.d/99-segoe-alias.conf \
    && fc-cache -f > /dev/null

WORKDIR /app
CMD ["sleep", "infinity"]
