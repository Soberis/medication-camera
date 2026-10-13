# medication-camera-edge

配药视觉质检体系中的 **RK3588 边缘端音视频采集与直传流水线**。与云端平台 [medication-video-platform](https://github.com/Soberis/medication-video-platform/tree/dev) 无缝对接。

专为医院药房、静配中心等严苛现场环境打造，支持 **开机无人值守定长切片录制** 与 **外部 IO 按钮 / USB 脚踏板 / 局域网 HTTP 事件驱动录制**，并提供断网无损暂存、定量/超时批量上传、磁盘空间自维护及开机 systemd 守护体系。

---

## 关联项目生态

本项目是配药视觉质检与具身操作数据体系中的边缘现场采集流水线：
- **边缘采集端**：**medication-camera-edge**（本项目）— RK3588 边缘工控机多机位音视频同步录制、本地缓冲池维护及批量直传流水线。
- **公开示范挖掘端**：[medication-dataset-tools](https://github.com/Soberis/medication-dataset-tools/tree/dev) — 第一人称及双手配药实操网络视频跨平台检索、量化质检与去重工具集。
- **云端中心平台**：[medication-video-platform](https://github.com/Soberis/medication-video-platform/tree/dev) — Java Core API + React 标注管理端，负责数据接收归组、MediaPipe 手部分析、标注任务调度与数据集导出。
- **动作推理节点**：[medication-action-worker](https://github.com/Soberis/medication-action-worker/tree/dev) — RTX 4090 GPU 节点，基于 Qwen 视觉大语言模型进行有时序边界的动作候选推理。

---

## 目录
- [1. 核心特性与系统架构](#1-核心特性与系统架构)
- [2. 快速部署指南（RK3588 工控机）](#2-快速部署指南rk3588-工控机)
- [3. 配置参数详解](#3-配置参数详解)
- [4. 录像模式与触发控制](#4-录像模式与触发控制)
  - [模式 A：持续循环切片录制（默认）](#模式-a持续循环切片录制默认)
  - [模式 B：GPIO 硬件引脚按键触发](#模式-bgpio-硬件引脚按键触发)
  - [模式 C：USB 脚踏板 / 扫码枪按键触发](#模式-cusb-脚踏板--扫码枪按键触发)
  - [模式 D：局域网 REST HTTP API 触发](#模式-d局域网-rest-http-api-触发)
- [5. 运维与排障命令](#5-运维与排障命令)
- [6. 本地自动化测试与模拟模式](#6-本地自动化测试与模拟模式)

---

## 1. 核心特性与系统架构

### 架构拓扑与数据流向

```text
[Gemini 335 相机 / GPIO / 脚踏板]
               │
               ▼
┌────────────────────────────────────────────────────────┐
│  medication-record 服务 (tools/loop_recorder.py)       │
│   • 驱动 Orbbec C++ 录像内核或 FFmpeg 合成录制          │
│   • Web 标准化转码 (H.264 / yuv420p / +faststart)      │
│   • 原子移入就绪池 (/data/spool/ready)                 │
└────────────────────────────────────────────────────────┘
               │  /data/spool/ready (解耦缓冲池)
               ▼
┌────────────────────────────────────────────────────────┐
│  medication-uploader 服务 (tools/batch_uploader.py)    │
│   • 3D 阈值触发：定量(N段) / 定容(X MB) / 超时(Y 秒)    │
│   • 分片直传 (S3 Multipart) + 原子入库 CaptureGroup    │
│   • 断网自动重试、事务安全回退、磁盘空间过期清理 (FIFO)│
└────────────────────────────────────────────────────────┘
               │  HTTP / S3
               ▼
[medication-video-platform 配药质检云平台]
```

> **数据下游流转**：切片上传入库后，由 [medication-video-platform](https://github.com/Soberis/medication-video-platform/tree/dev) 自动进行媒体质检（Media Probe）与 MediaPipe 手部姿态检测，并通过消息队列派发给 [medication-action-worker](https://github.com/Soberis/medication-action-worker/tree/dev) 完成 Qwen 动作时序候选识别，最终在 Web 工作台实现自动草稿铺设/人机协同标注与 HDF5 训练集导出。

### 本地缓冲池（Spooling Pool）状态机
- `/data/spool/recording/`：正在录制的切片临时工作目录。
- `/data/spool/ready/`：录制且转码完成、元数据验证完毕的待上传切片。
- `/data/spool/uploading/`：当前正由上传进程搬运并提交至云平台的切片批次。
- `/data/spool/completed/`：已成功入库并归档的历史切片（按保留策略自动淘汰）。

---

## 2. 快速部署指南（RK3588 工控机）

### 2.1 依赖环境要求
- **系统**：Ubuntu 20.04 / 22.04 LTS (aarch64 / ARM64)
- **依赖库**：`build-essential cmake libopencv-dev ffmpeg python3`
- **硬件 SDK**：Orbbec SDK v2.9.3（预装于 `/opt/orbbec/...`）

### 2.2 编译底层 C++ 录像内核
在 RK3588 终端执行：

```bash
cd /home/firefly/projects/medication-camera-edge

# 1. 编译 C++ 录像程序
cmake -S . -B build \
  -DORBBEC_SDK_PATH=/opt/orbbec/OrbbecSDK_v2.9.3_202607151523_2f6561c_linux_arm64
cmake --build build -j$(nproc)

# 2. 验证生成产物
./build/medication_camera
# 正常应输出: Usage: medication_camera CONFIG.json NEW_OUTPUT_DIRECTORY
```

### 2.3 一键安装开机自启系统服务
运行自带的部署脚本即可自动创建缓冲池目录并注册为 systemd 系统守护服务：

```bash
sudo bash deploy/install_edge.sh
```

脚本将自动激活并启动两个后台独立服务：
- `medication-record.service`：音视频切片录制守护进程。
- `medication-uploader.service`：定量批量上传守护进程。

---

## 3. 配置参数详解

核心配置文件位于 `config/edge_continuous.json`：

```json
{
  "platform_base_url": "http://192.168.5.100:8080",
  "project_id": "f6f7c6b9-05c9-4582-b7d3-f623791a00d3",
  "device_id": "rk3588-hospital-01",
  "spool_dir": "/data/spool",
  "duration_seconds": 180,
  "batch_count": 5,
  "batch_size_mb": 500,
  "max_delay_seconds": 1800,
  "poll_interval": 5,
  "retention_hours": 48,
  "min_disk_free_gb": 10,
  "username": "admin",
  "password": "admin12345678",
  "trigger": {
    "mode": "loop",
    "max_duration_seconds": 300,
    "gpio": {
      "chip": 1,
      "line": 12,
      "active_low": true,
      "debounce_ms": 50
    },
    "usb_hid": {
      "key_code": 57,
      "debounce_ms": 100
    },
    "http": {
      "host": "0.0.0.0",
      "port": 8090
    }
  },
  "cameras": [
    {
      "camera_number": 1,
      "serial": "auto",
      "camera_role": "OVERVIEW",
      "mount_position": "camera-1"
    }
  ]
}
```

### 关键配置项说明

| 参数项 | 类型 | 默认值 | 作用说明 |
| :--- | :--- | :--- | :--- |
| `platform_base_url` | String | `http://<IP>:8080` | 云平台后端 API 基础地址 |
| `project_id` | UUID | - | 视频归属项目的 UUID（进入 Web 端项目列表即可获取） |
| `device_id` | String | `rk3588-hospital-01` | 工控机唯一设备标识符 |
| `duration_seconds` | Integer | `180` | 单切片时长（秒）。3 分钟即为 180 秒；快速联调可设为 15 |
| `batch_count` | Integer | `5` | **数量阈值**：就绪切片积攒满 N 段立即触发批量上传（设为 1 则每段录完立即传） |
| `batch_size_mb` | Integer | `500` | **容量阈值**：就绪切片累计体积达 X MB 立即触发上传 |
| `max_delay_seconds`| Integer | `1800` | **超时保底**：首个就绪切片等待超过 Y 秒强制触发上传 |
| `retention_hours` | Integer | `48` | 已完成切片在本地磁盘保留时间（小时），超时自动清理 |
| `min_disk_free_gb` | Integer | `10` | 磁盘最低安全剩余空间（GB），低于此阈值执行 FIFO 紧急释放 |

---

## 4. 录像模式与触发控制

在 `config/edge_continuous.json` 中配置 `"trigger": { "mode": "..." }` 即可按需切换：

### 模式 A：持续循环切片录制（默认）
- **配置**：`"mode": "loop"`
- **行为**：工控机开机后持续无缝录制 3 分钟定长切片，切片转码完成后原子送入缓冲池，随后立即录制下一段。无需现场人工干预。

### 模式 B：GPIO 硬件引脚按键触发
- **配置**：`"mode": "gpio"`
- **行为**：医护人员在配药台按下硬件自复位按键开启录像，配药完毕再次按下保存切片并就绪上传。
- **硬件保护**：
  - 内置 50ms 软件防抖，彻底消除物理按键杂波。
  - 内置 `max_duration_seconds` 防呆保护机制（如 5 分钟超时自动截断保存，防止误忘按停导致录像持续占满磁盘）。

### 模式 C：USB 脚踏板 / 扫码枪按键触发
- **配置**：`"mode": "usb_hid"`
- **行为**：操作者踩下 USB 医疗脚踏板（如输入 Space/空格键或回车键）启动录像，再次踩下结束录像。

### 模式 D：局域网 REST HTTP API 触发
- **配置**：`"mode": "http"`
- **行为**：提供轻量级局域网 HTTP 控制接口（默认监听 `8090` 端口），可供医院 HIS 系统、配药台平板或第三方中控联动：
  - **启动录制**：
    ```bash
    curl -X POST http://192.168.5.5:8090/api/trigger/start
    ```
  - **停止录制并保存**：
    ```bash
    curl -X POST http://192.168.5.5:8090/api/trigger/stop
    ```
  - **查看当前录制状态**：
    ```bash
    curl http://192.168.5.5:8090/api/trigger/status
    ```

---

## 5. 运维与排障命令

### 5.1 服务状态与实时日志
```bash
# 查看录像与上传服务运行状态
sudo systemctl status medication-record
sudo systemctl status medication-uploader

# 实时追踪录像日志
journalctl -u medication-record -f

# 实时追踪批量上传日志
journalctl -u medication-uploader -f
```

### 5.2 查看缓冲池中切片积攒情况
```bash
# 查看待上传切片
ls -lh /data/spool/ready

# 查看已上传切片归档
ls -lh /data/spool/completed
```

### 5.3 重启或停止服务
```bash
# 重启录像流水线（修改配置后）
sudo systemctl restart medication-record
sudo systemctl restart medication-uploader

# 停用开机自启
sudo systemctl disable medication-record medication-uploader
```

---

## 6. 本地自动化测试与模拟模式

在未连接物理摄像头或非 ARM 开发机（如 Windows / Linux x86）上，可直接利用 `--synthetic` 模式和单元测试验证全流水线闭环：

### 运行全量单元测试套件
```bash
# 包含触发器子系统、防抖逻辑、HTTP 控制、流式转码、多维阈值上传等 18 项自动化测试
python -m unittest discover -s tests -p "test_*.py" -v
```

### 手动单步模拟录像与上传
```bash
# 1. 模拟录制 2 段 5 秒切片
python tools/loop_recorder.py \
    --duration 5 \
    --spool /data/spool \
    --synthetic \
    --max-segments 2

# 2. 模拟单次批量上传
python tools/batch_uploader.py \
    --config config/edge_continuous.json \
    --once
```
