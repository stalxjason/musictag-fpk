#!/usr/bin/env bash
# 非 fnOS 环境（Debian/Ubuntu 等）的本地部署脚本。
# 注意：fnOS 上请使用应用中心的 .fpk 安装包，不要使用本脚本。
set -euo pipefail

INSTALL_DIR="${MUSICTAG_INSTALL_DIR:-/opt/musictag-native}"
SERVICE_USER="${MUSICTAG_USER:-musictag}"
PORT="${PORT:-8002}"

if [ "$(id -u)" -ne 0 ]; then
    echo "请使用 root 运行（需要创建用户与写入 systemd）。" >&2
    exit 1
fi

echo "🎵 安装 MusicTag Native 到 ${INSTALL_DIR} ..."
mkdir -p "${INSTALL_DIR}"

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "1. 创建专用系统用户 ${SERVICE_USER} ..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

echo "2. 复制程序文件 ..."
cp -f server.py scraper_engine.py tag_engine.py lyrico_bridge.py lyrico_host.js \
      app_paths.py security.py requirements.txt "${INSTALL_DIR}/"
rm -rf "${INSTALL_DIR}/web" "${INSTALL_DIR}/Lyrico-Plugins"
cp -rf web Lyrico-Plugins "${INSTALL_DIR}/"

echo "3. 创建虚拟环境并安装依赖 ..."
python3 -m venv "${INSTALL_DIR}/venv"
if [ -d wheels_cache ]; then
    "${INSTALL_DIR}/venv/bin/pip" install --no-index --find-links wheels_cache -r "${INSTALL_DIR}/requirements.txt"
else
    "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
fi

echo "4. 准备运行数据目录 ..."
mkdir -p "/var/lib/musictag"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "/var/lib/musictag" "${INSTALL_DIR}"

echo "5. 写入 systemd 服务 ..."
cat > /etc/systemd/system/musictag.service <<EOF
[Unit]
Description=MusicTag Native Music Tagger
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=MUSICTAG_STATE_DIR=/var/lib/musictag
Environment=PORT=${PORT}
Environment=PYTHONUNBUFFERED=1
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/server.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable musictag
systemctl restart musictag

echo "=========================================================="
echo "✅ 安装完成，访问 http://<主机IP>:${PORT}"
echo "   状态目录: /var/lib/musictag（含访问令牌 auth_token）"
echo "   查看日志: journalctl -u musictag -f"
echo "=========================================================="
