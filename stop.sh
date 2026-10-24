#!/usr/bin/env bash
# 停止本地启动的 MusicTag Native 进程。
# 注意：使用精确匹配 server.py 的进程，避免误杀其它 Python 服务。
set -uo pipefail

if pgrep -f "python3 .*server\.py" >/dev/null 2>&1; then
    pkill -TERM -f "python3 .*server\.py"
    sleep 2
    pkill -KILL -f "python3 .*server\.py" 2>/dev/null || true
    echo "已停止。"
else
    echo "未发现运行中的 MusicTag Native 进程。"
fi
