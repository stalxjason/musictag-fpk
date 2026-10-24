#!/usr/bin/env bash
# 本地前台启动（开发/调试用）。fnOS 上由应用框架托管，无需使用本脚本。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8002}"
export PORT
echo "🎵 启动 MusicTag Native（端口 ${PORT}）..."
exec python3 server.py
