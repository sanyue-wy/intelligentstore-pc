# debug/ 调试与排障中心

调试目录**按业务模块一一对应组织**。每个模块的实现目录旁都有配套的调试子文件夹：

| 调试文件夹 | 对应模块 | 入口 | 说明 |
| :--- | :--- | :--- | :--- |
| `common/` | *(无对应模块)* | — | 跨模块共用组件：`canvas_view.py` 2D 平台画布、`theme.py` 深色主题 |
| `communication_debug/` | `communication/` | `python main.py --comm` | 串口/JOG/夹爪/32B Hex 报文 |
| `vision_debug/` | `vision/` | `python main.py --vision` | 转调浏览器调参台 :8090 (托盘/畸变/颜色/标定/相机) |
| `message_center_debug/` | `MessegeCenter/` | `python main.py --msg` | 全主题报文监听与事件注入 |
| `monitor_debug/` | `moniter/` | `python main.py --monitor` | 看门狗心跳、CPU/内存、告警中心 |
| `integrated_debug/` | *(全部模块)* | `python main.py` | 五合一全系统综合联调工作台 |

每个子文件夹都是**自包含**的：源码与对应的 `.bat` 启动脚本放在一起，可以整体拷走或替换。

---

## 替换某个模块时怎么做

**把模块实现目录和它的调试文件夹一起替换，上层代码零改动。**

四个模块都是同样的流程：

| 要替换的 | 实现目录 | 契约文件 | 配套调试文件夹 |
| :--- | :--- | :--- | :--- |
| 视觉 | `vision/` | [../vision/interfaces.py](../vision/interfaces.py) | `vision_debug/` |
| 通讯 | `communication/` | [../communication/interfaces.py](../communication/interfaces.py) | `communication_debug/` |
| 消息中心 | `MessegeCenter/` | [../MessegeCenter/interfaces.py](../MessegeCenter/interfaces.py) | `message_center_debug/` |
| 监控 | `moniter/` | [../moniter/interfaces.py](../moniter/interfaces.py) | `monitor_debug/` |

其余目录不需要动。`integrated_debug/` 依赖全部模块，不随任何单一模块替换。

### 通用三步

1. **读该模块的 `interfaces.py`** —— 抽象基类、数据契约（字段语义）、消息总线约定、能力声明。
2. **实现其中的抽象基类。** 上层只依赖基类，不依赖任何具体类。
3. **注册后端：**
   ```python
   from vision import register_vision_backend, VisionBackend
   register_vision_backend(VisionBackend(
       name="my_vision", display_name="新视觉实现",
       service_factory=MyVisionService, detector_factory=MyDetector,
   ))
   ```

四个模块的注册函数对应关系：

| 模块 | 注册函数 | 构造入口 |
| :--- | :--- | :--- |
| 视觉 | `register_vision_backend()` | `create_vision_service(bus, backend=...)` |
| 通讯 | `register_comm_backend()` | `create_comm_client(backend=...)` |
| 消息中心 | `register_message_bus_backend()` | `create_message_bus(backend=...)` |
| 监控 | `register_monitor_backend()` | `create_monitor_service(bus, backend=...)` |

### 各模块必须保证的约定

漏掉这些通常不会立刻报错，而是**静默失效**：

| 模块 | 必须保证 | 漏了会怎样 |
| :--- | :--- | :--- |
| **视觉** | 每 1 秒发 `monitor/heartbeat`（`module_name="vision"`）；检测到目标发 `vision/target` | 3 秒看门狗判 vision 离线，报 code=1001 告警 |
| **通讯** | 提供 7 个同名回调列表（`on_status_callbacks` 等）与 `send_estop`/`send_clear`/`execute_sorting_task` 等同名方法 | `router.attach_communication()` 静默不工作，总线闭环断开 |
| **消息中心** | `publish()` 在锁外分发；订阅者异常必须吞掉；支持 fnmatch 通配（`vision/*`） | 订阅者报错会拖垮整个系统发布 |
| **监控** | 自身 `module_name` 保持 `"monitor"`；订阅所有模块心跳 | 看板上多出一条记录，模块列表与预期不符 |

### 数据契约不可重复定义

`Message` / `VisionTarget` / `ModuleHeartbeat` / `SystemAlert` 的唯一定义处是
[../MessegeCenter/topics.py](../MessegeCenter/topics.py)，其他接口文件都从那里 re-export。

切勿再定义一份 —— `router.py` 通过 `isinstance(payload, VisionTarget)` 判断任务载荷，
一旦出现第二个类，分拣闭环会静默失效（不报错，但任务不再执行）。

### 能力声明

各模块接口文件都定义了 `*Capabilities`（如 `VisionCapabilities`、`CommCapabilities`、
`MonitorCapabilities`），用于让调试界面适配不同实现：

```python
VisionCapabilities(supports_hsv_tuning=False)   # HSV 面板自动隐藏
CommCapabilities(supports_serial_port_listing=False)  # 端口框改为手工输入
```

---

## 常用命令

```bash
python main.py                          # 全系统综合联调 (推荐)
python main.py --comm                   # 仅通讯
python main.py --vision                 # 仅视觉
python main.py --msg                    # 仅消息中心
python main.py --monitor                # 仅监控
```

### 指定模块后端（替换实现后用于验证，无需改代码）

每个模块有独立的后端参数，可单独用也可组合：

```bash
python main.py --vision --vision-backend=my_vision     # 浏览器调参台 (指定视觉后端)
python main.py --comm   --comm-backend=tcp             # 通讯调试器
python main.py --msg    --msg-backend=zmq              # 消息中心调试器
python main.py --monitor --monitor-backend=apm         # 监控调试器

# 综合工作台同时切换多个模块
python main.py --vision-backend=a --comm-backend=b --monitor-backend=c --msg-backend=d
```

`--backend=<名>` 是兼容写法，等同于 `--vision-backend=`。
各模块可用的后端名见对应 `interfaces.py` 的注册表（`list_*_backends()`）。
Windows 下也可直接双击对应 `.bat`（位于各调试子文件夹内），但 `.bat` 不带后端参数，
需要指定后端时用上面的命令行方式。

## 导入约定

`debug/__init__.py` 采用 PEP 562 惰性导出，`import debug` 不会拉起 tkinter / cv2 / PIL / psutil。
以下名字仍可照常使用，底层按需导入：

```python
from debug import run_unified_debugger, run_debug_gui, run_vision_standalone
```

---

## 视觉调参在浏览器里，不在 Tk 里

需要实时画面的调参（HSV 颜色、托盘定位、镜头畸变、9 点/参考点标定、相机曝光白平衡、ROI、点哪去哪、模型置信度）
全部在浏览器调试台 `python3 tools/vision_tool.py web [--arm]`（`http://<板子IP>:8090`）完成，说明见 `vision/GUIDE.md` 与 `tools/GUIDE.md`。

- `python main.py --vision` 直接转调浏览器调试台（旧 Tk 视觉窗口已删除）；
- 综合工作台的「视觉」页只看画面和识别结果，页内按钮可释放相机并拉起浏览器调试台；
- YOLO/BPU 推理在 `vision/yolo_engine.py`，模型转换用根目录 `./convert.sh`。
