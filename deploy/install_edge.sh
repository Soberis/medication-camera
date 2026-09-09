#!/bin/bash
set -euo pipefail

# ==============================================================================
# RK3588 边缘端系统服务一键安装脚本 (medication-camera)
# ==============================================================================

PROJECT_DIR="/home/firefly/projects/medication-camera"
SPOOL_DIR="/data/spool"

echo "=== [1/4] 创建本地缓冲池目录: ${SPOOL_DIR} ==="
mkdir -p "${SPOOL_DIR}/recording"
mkdir -p "${SPOOL_DIR}/ready"
mkdir -p "${SPOOL_DIR}/uploading"
mkdir -p "${SPOOL_DIR}/completed"
chmod -R 777 "${SPOOL_DIR}"

echo "=== [2/4] 安装 systemd 系统服务文件 ==="
cp "${PROJECT_DIR}/deploy/medication-record.service" /etc/systemd/system/
cp "${PROJECT_DIR}/deploy/medication-uploader.service" /etc/systemd/system/

echo "=== [3/4] 重载 systemd 守护进程并启用自启 ==="
systemctl daemon-reload
systemctl enable medication-record.service
systemctl enable medication-uploader.service

echo "=== [4/4] 启动服务 ==="
systemctl restart medication-record.service
systemctl restart medication-uploader.service

echo ""
echo "=================================================================="
echo "✅ 边缘端开机自启与批量上传流水线服务已成功安装并启动！"
echo "  • 查看录像服务日志: journalctl -u medication-record -f"
echo "  • 查看上传服务日志: journalctl -u medication-uploader -f"
echo "  • 检查就绪切片池:   ls -la ${SPOOL_DIR}/ready"
echo "=================================================================="
