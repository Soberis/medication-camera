# medication-camera

RK3588 双 Gemini 335 RGB 录像与 medication-video-platform V2 上传客户端。
相机硬件基线保持为 SDK v2.9.3、FW 1.8.10、1920×1080 MJPG / 30 FPS。
本轮不改固件、USB、标定，不运行算法，也不上传 Depth。

```text
config/edge.json
  → C++ 双摄并发录像（按 SN 固定机位）
  → FFmpeg 转 H.264 / yuv420p MP4
  → 采集目录和不可变上传清单
  → Python V2 manifest + multipart 直传 + CaptureGroup 收件结束
  → 平台媒体检查、页面查看
```

## 1. 在工控机更新、编译

先在工控机的仓库目录检查 `git status --short`。有本地修改时先保留，不要 reset 或覆盖。
以下操作由操作者执行；拉取、编译不会启动相机。

```bash
cd /home/firefly/projects/medication-camera
git pull --ff-only
cmake -S . -B build \
  -DORBBEC_SDK_PATH=/opt/orbbec/OrbbecSDK_v2.9.3_202607151523_2f6561c_linux_arm64
cmake --build build -j2
python3 --version
ffmpeg -version
```

如实际仓库位于其他目录，替换第一行。依赖是 C++17 编译器、CMake、OpenCV
（core/imgcodecs/videoio）、现有 Orbbec SDK、Python 3.8+、带 libx264 的 FFmpeg。
Python 客户端没有第三方运行时依赖。Ubuntu 缺少依赖时可安装
`build-essential cmake libopencv-dev ffmpeg python3`，不需要重新安装已验证的 SDK。

## 2. 让工控机访问本地平台

平台项目先按自身 `scripts/start-local.ps1` 启动 Core API、对象存储和 Media Worker。
首次测试只拍摄测试物品，使用隔离开发项目；不要使用真实患者数据。

**首轮可使用 SSH 反向隧道，无需把数据库或对象存储开放到局域网。**
在运行平台的 Windows 电脑单独打开终端执行，并保持该连接：

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R 18080:127.0.0.1:8080 -R 19000:127.0.0.1:19000 root@192.168.5.5
```

此示例假定平台 API 位于本机 8080、平台的 `S3_ENDPOINT=http://localhost:19000`，
且对象存储实际监听 19000。工控机配置 `platform_base_url=http://127.0.0.1:18080`。
预签名 URL 中的 `localhost:19000` 会通过第二条隧道抵达 Windows 对象存储。
如果你的平台使用默认 9000，第二条参数改为 `-R 9000:127.0.0.1:9000`；
**远端监听端口必须与 S3_ENDPOINT/签名 URL 中的端口一致。** 不要修改返回的签名 URL。
API 端口不同也应相应修改。SSH 登录密码由 SSH 交互输入，不放进配置或脚本。

长期部署可改用 LAN/HTTPS：API 和对象存储都必须可从工控机访问，平台签名时就应使用
可达的对象存储地址。仅改 API 地址不够。此仓库不会改动平台的监听、防火墙或凭据。

## 3. 配置机位与平台 ID（首次部署）

```bash
cp config/edge.example.json config/edge.json
```

编辑 `config/edge.json`：

- `platform_base_url`：根据上面的隧道或 LAN 方案填写。示例文件的 `192.168.5.10` 只是占位地址。
- `project_id`：平台项目的 UUID。平台默认 local synthetic 项目为
  `00000000-0000-0000-0000-000000000001`，如部署改过则使用实际值。
- `device_id`：该工控机的稳定部署编号；不是相机 SN。
- `camera_number=1` 固定到 `CP0F463000J6`，`camera_number=2` 固定到 `CP0F463000L6`。
- `camera_role`、`mount_position`：按实际安装位置填写；示例的 OVERVIEW/HANDS 是待核对配置，
  不是已经确认的物理位置。相机编号不因枚举顺序或缺失而变化。
- `duration_seconds`：首轮建议 10 秒；当前允许 1～3600 秒。
- `allow_http=true` 仅用于受控本地联调，HTTPS 部署设为 false。

管理员一次性配置 CameraChannel / CameraSet。令牌从当前平台开发环境取得，
不要把医院凭据或生产密钥带入本地测试。Bash 可用隐藏输入：

```bash
read -rsp 'Platform admin token: ' PLATFORM_ADMIN_TOKEN; echo
export PLATFORM_ADMIN_TOKEN
python3 tools/edge.py provision --config config/edge.json
unset PLATFORM_ADMIN_TOKEN
```

此命令只配置平台逻辑机位，不启动相机。生成的 `camera_channel_id`、`camera_set_id`
写回未跟踪的本地配置，令牌不保存。也可以由平台管理员在其他机器完成配置后填写准确 ID。
同一 CameraSet 版本不可变；变更成员或角色时提高 `configuration_version` 后重新配置。

## 4. 拍摄并准备上传

**以下 `record` 命令会启动真实相机，由操作者执行。**

```bash
python3 tools/edge.py record --config config/edge.json
```

命令输出唯一的 `Task: .../spool/<UUID>`。两路并发录像，完成后顺序转码；
仅当两台录像和转码全部成功，才生成 `upload-state.json`。没有自动上传。

```text
spool/<UUID>/
├── task.json                 # 采集组 ID、采集开始时间、冻结配置
├── recording-config.json
├── recording/
│   ├── camera_1.raw.mp4       # 原始 OpenCV 录像，保留
│   ├── camera_2.raw.mp4
│   └── capture-result.json   # 仅成功时存在；帧数、超时、首帧主机 UTC、耗时
├── camera_1.mp4              # H.264 上传文件
├── camera_2.mp4
├── metadata.json
├── event-log.ndjson
├── manifest.json             # external-video-ingest-v2
└── upload-state.json         # 身份、文件摘要、平台返回 ID；不保存令牌或签名 URL
```

如只有转码/准备失败，修复环境后可继续同一目录，不必重拍：

```bash
python3 tools/edge.py prepare spool/<UUID>
```

如录像失败，该目录保留诊断材料且不可上传；修复后重新执行 `record` 创建新任务。
不要手工伪造完成标记。两台缺任一台会明确失败，本轮不实现缺路降级上传。

## 5. 上传与检查

使用具备项目上传权限的令牌，通常对应平台 local 环境的 `DEV_BEARER_TOKEN`：

```bash
read -rsp 'Platform upload token: ' PLATFORM_TOKEN; echo
export PLATFORM_TOKEN
python3 tools/edge.py upload spool/<UUID>
python3 tools/edge.py status spool/<UUID>
unset PLATFORM_TOKEN
```

`upload` 成功表示文件和数据组收件完成；`status` 显示各机位的媒体状态。
`status` 在收件 COMPLETE 且两路均 INGESTED 时退出 0，尚未就绪退出 2，请稍后重查；
请求/配置失败退出 1。最后在平台页面打开对应数据组，确认两段视频可以播放。

完整协议（统一前缀 `/api/v1`）：

1. POST `/projects/{projectId}/external-video-ingest-manifests/v2`，初始不结束收件。
2. POST `/upload/init`，绑定 manifest，声明完整文件清单的规范摘要。
3. PUT `/upload/{batchId}/files/{clientFileId}`，先注册全部 4 个文件。
4. 每文件创建 multipart；申请 Part URL、直传对象存储、确认 Part、完成 File。
5. 完成 Batch，组装 CaptureSession，核对所有视频绑定。
6. POST 数据组 `receipt/finalize`，检查 COMPLETE 与机位集合。

上传失败后重复相同 `upload` 命令。任务复用相同 group key、batch 幂等键和 file ID；
服务器协调已传但未确认的分片，内存中一次只读取 8 MiB，不整段加载大视频。
响应丢失、进程退出后可续传；平台 batch 已 EXPIRED/ABORTED/FAILED 时停止，保留文件供人工处理，
不自动新建重复组。文件大小或 SHA-256 改变时拒绝上传。
单个任务有进程锁，系统会在进程退出时释放；不要手工删除正在使用的 `.lock`。

## 验证与当前边界

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖分片和控制面响应丢失恢复、幂等重试、摘要冲突、过期任务、机位配置、进程锁、
令牌隔离，以及 FFmpeg 合成视频转码。媒体测试需要 FFmpeg/ffprobe。

真实服务合成验收（**只对隔离的 local/test 平台执行**，会创建合成 CameraSet、数据组和文件）：

```bash
export RUN_CAMERA_UPLOAD_E2E=1
export CAMERA_TEST_BASE_URL=http://localhost:28080
# 通过环境设置 PLATFORM_ADMIN_TOKEN、PLATFORM_TOKEN；不从命令参数传令牌。
python3 tests/e2e_upload.py
```

该工具不接收源视频路径、不连接相机，自动生成两段 1080p 合成视频，测试传完首个 Part
但未确认时中断，再恢复并重复提交，最终等待 Media Probe。合成证据保存在 `.runtime/`。

当前明确边界：

- C++ 需在具备 OrbbecSDK / OpenCV 的 ARM 工控机实际编译与验收；Python/合成测试不能代替硬件结果。
- 录像使用 30 FPS CFR，掉帧会使媒体时长短于实际采集时长；完成标记保留实际帧数和主机耗时。
  `sync_status=UNKNOWN`，`time_offset_ms=0` 是未测量占位，不是硬件同步或精确对齐证明。
- 未实现常驻服务、自动重连、磁盘自动清理、硬件编码、Depth 上传或 AI。原始及转码文件都保留，
  长时间采集前确认空间；本轮先用短片验收。
- 开发平台的本地 bearer 身份是联调机制；正式部署的设备认证与凭据轮换须按平台部署方案另行配置。
