"""
官方标准飞牛 fnOS 原生应用打包构建脚本 (MusicTag Native FPK Builder)
========================================================================
遵循飞牛开放平台最新规范 (https://developer.fnnas.com/docs/quick-started/create-application/)
使用官方 fnpack CLI 工具执行标准打包，生成合规的 .fpk 安装包。
"""

import io
import os
import re
import sys
import json
import gzip
import shutil
import struct
import tarfile
import zlib
import subprocess

try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except ImportError:  # 构建机未安装 Pillow 时退化为纯标准库绘制
    HAS_PIL = False

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(PROJECT_ROOT, "build_target", "MusicTagNative")
FNPACK_EXE = os.path.join(PROJECT_ROOT, "tools", "fnpack.exe")
WHEELS_CACHE = os.path.join(PROJECT_ROOT, "wheels_cache")

# 应用版本号的唯一来源：manifest、产物文件名均由此派生
# 可用环境变量 MUSICTAG_APP_VERSION 覆盖（仅用于预发布验证），默认取 manifest.json 中的版本
def _detect_version() -> str:
    env_ver = (os.environ.get("MUSICTAG_APP_VERSION") or "").strip()
    if env_ver:
        return env_ver
    try:
        with open(os.path.join(PROJECT_ROOT, "manifest.json"), "r", encoding="utf-8") as fh:
            ver = (json.load(fh).get("version") or "").strip()
        if ver:
            return ver
    except Exception:  # manifest.json 缺失时回退默认版本
        pass
    return "1.0.2"

APP_VERSION = _detect_version()
# 作者名保留，但不附带外链（按发布要求下线飞牛开发者链接）
MAINTAINER = "Fkosoft"
MAINTAINER_URL = ""
DISTRIBUTOR = "MusicTag"
APP_DISPLAY_NAME = "音乐标签 (MusicTag Native)"
# 最低支持的 fnOS 版本（按官方文档要求：只声明实际测试过的兼容范围）
OS_MIN_VERSION = "0.9.0"
CHANGELOG = "1.0.2：修复离线依赖安装与启动稳定性问题，优化刮削与歌词写入体验。"

def log(msg: str):
    print(f"[BUILD] {msg}")

def ensure_lf(text: str) -> bytes:
    """确保文本以严格的 LF (\\n) 换行符保存为 bytes"""
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")

def _icon_layers(size: int):
    """返回图标各层的 (类型, 半径, 颜色, 描边宽度)，半径算法与 PIL 分支的 ellipse 边界一致。"""
    stroke = max(1.0, size / 64)
    return [
        ("fill", size / 2 - max(1, size / 16), (99, 102, 241, 255), stroke),    # 外圆
        ("fill", size / 2 - max(3, size / 5), (30, 41, 59, 255), stroke),      # 唱片盘体
        ("outline", size / 2 - max(4, size / 4), (51, 65, 85, 255), stroke),   # 唱片纹理
        ("fill", size / 2 - max(5, size / 3), (236, 72, 153, 255), stroke),    # 中心标签
        ("fill", size * 7 / 32, (255, 255, 255, 255), stroke),                 # 中心孔（直径 7/16）
    ]


def _write_png(path: str, width: int, height: int, rgba: bytearray) -> None:
    """用标准库写出 RGBA PNG（逐行 filter=0）。"""
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", header))
        fh.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        fh.write(chunk(b"IEND", b""))


def _generate_icons_stdlib(out_paths):
    """纯标准库绘制唱片图标：用像素中心到圆心距离判定各环，4x 超采样抗锯齿。"""
    ss = 4
    for path, size in out_paths:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rings = _icon_layers(size)

        buf = bytearray(size * size * 4)
        for y in range(size):
            for x in range(size):
                acc = [0, 0, 0, 0]
                samples = 0
                for sy in range(ss):
                    for sx in range(ss):
                        px = x + (sx + 0.5) / ss - size / 2
                        py = y + (sy + 0.5) / ss - size / 2
                        dist = (px * px + py * py) ** 0.5
                        cr, cg, cb, ca = 0, 0, 0, 0
                        for kind, radius, (r_, g_, b_, a_), width in rings:
                            if kind == "fill" and dist <= radius:
                                cr, cg, cb, ca = r_, g_, b_, a_
                            elif kind == "outline" and abs(dist - radius) <= width / 2:
                                cr, cg, cb, ca = r_, g_, b_, a_
                        acc[0] += cr
                        acc[1] += cg
                        acc[2] += cb
                        acc[3] += ca
                        samples += 1
                i = (y * size + x) * 4
                buf[i:i + 4] = bytes(v // samples for v in acc)
        _write_png(path, size, size, buf)
        log(f"Generated icon (stdlib): {path} ({size}x{size})")


def generate_icons(out_paths: list[tuple[str, int]]):
    """生成高质量现代音乐标签图标（有 Pillow 用 Pillow，否则用标准库）。"""
    if not HAS_PIL:
        _generate_icons_stdlib(out_paths)
        return
    for path, size in out_paths:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(im)
        
        # 现代渐变外圆 (靛蓝/紫色)
        margin = max(1, size // 16)
        draw.ellipse([margin, margin, size - margin, size - margin], fill=(99, 102, 241, 255))
        
        # 黑色胶黑盘唱片槽
        v_margin = max(3, size // 5)
        draw.ellipse([v_margin, v_margin, size - v_margin, size - v_margin], fill=(30, 41, 59, 255))
        
        # 唱片纹理线条
        groove = max(4, size // 4)
        draw.ellipse([groove, groove, size - groove, size - groove], outline=(51, 65, 85, 255), width=max(1, size // 64))
        
        # 中心标签红粉色
        c_margin = max(5, size // 3)
        draw.ellipse([c_margin, c_margin, size - c_margin, size - c_margin], fill=(236, 72, 153, 255))
        
        # 唱片中心白色小孔
        h_margin = max(7, size * 7 // 16)
        draw.ellipse([h_margin, h_margin, size - h_margin, size - h_margin], fill=(255, 255, 255, 255))
        
        im.save(path, format="PNG")
        log(f"Generated icon: {path} ({size}x{size})")


def _resize_icon(src: str, dst: str, size: int) -> None:
    """把源图标缩放成指定尺寸写到 dst；没有 Pillow 时直接复制原图。"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if HAS_PIL:
        try:
            with Image.open(src) as im:
                im.convert("RGBA").resize((size, size), Image.LANCZOS).save(dst, format="PNG")
            return
        except Exception as exc:  # 源图损坏时退回复制
            log(f"Warning: resize icon failed ({exc}), copying original")
    shutil.copy2(src, dst)


def setup_scaffold():
    log(f"Preparing scaffold at {BUILD_DIR}...")
    if os.path.exists(BUILD_DIR):
        shutil.rmtree(BUILD_DIR)
    
    cmd_dir = os.path.join(BUILD_DIR, "cmd")
    config_dir = os.path.join(BUILD_DIR, "config")
    wizard_dir = os.path.join(BUILD_DIR, "wizard")
    app_dir = os.path.join(BUILD_DIR, "app")
    ui_dir = os.path.join(app_dir, "ui")
    ui_img_dir = os.path.join(ui_dir, "images")
    web_dir = os.path.join(app_dir, "web")
    wheels_dir = os.path.join(app_dir, "wheels")

    for d in [cmd_dir, config_dir, wizard_dir, app_dir, ui_dir, ui_img_dir, web_dir, wheels_dir]:
        os.makedirs(d, exist_ok=True)
        
    # 1. manifest (INI 纯文本无后缀)
    manifest_content = f"""appname               = MusicTagNative
version               = {APP_VERSION}
display_name          = {APP_DISPLAY_NAME}
desc                  = 高性能低内存的原生音乐标签刮削与元数据编辑服务，原生集成各大音乐平台曲库，不依赖Wine虚拟化，内存占用极低。
source                = thirdparty
platform              = all
maintainer            = {MAINTAINER}
maintainer_url        = {MAINTAINER_URL}
distributor           = {DISTRIBUTOR}
desktop_uidir         = ui
desktop_applaunchname = MusicTagNative.Application
service_port          = 8002
ctl_stop              = true
disable_authorization_path = false
os_min_version        = {OS_MIN_VERSION}
changelog             = {CHANGELOG}
"""
    with open(os.path.join(BUILD_DIR, "manifest"), "wb") as f:
        f.write(ensure_lf(manifest_content))
    log(f"Written manifest (version {APP_VERSION})")

    # 2. config/privilege
    privilege_content = """{
    "defaults": {
        "run-as": "package"
    },
    "username": "musictag",
    "groupname": "musictag"
}
"""
    with open(os.path.join(config_dir, "privilege"), "wb") as f:
        f.write(ensure_lf(privilege_content))
    log("Written config/privilege")

    # 3. config/resource
    resource_content = """{
    "data-share": {
        "shares": [
            {
                "name": "musictag"
            },
            {
                "name": "musictag/data"
            }
        ]
    }
}
"""
    with open(os.path.join(config_dir, "resource"), "wb") as f:
        f.write(ensure_lf(resource_content))
    log("Written config/resource")

    # 4. 图标生成 (全大写名称与 ui 图标)
    generate_icons([
        (os.path.join(BUILD_DIR, "ICON.PNG"), 64),
        (os.path.join(BUILD_DIR, "ICON_256.PNG"), 256),
        (os.path.join(ui_img_dir, "icon_64.png"), 64),
        (os.path.join(ui_img_dir, "icon_256.png"), 256),
    ])

    # 5. app/ui/config（桌面入口：独立窗口打开服务端口，端口与 manifest.service_port 一致）
    # 桌面入口：用 type=url 在独立窗口打开应用端口。
    # 之前用 iframe 嵌入时，飞牛桌面容器（NAS 域名 + 反代）与 8002 直连是两个不同的 Host，
    # 部分网络下会表现为「点了应用却连不上」，改为独立窗口更稳。
    ui_config_content = """{
    ".url": {
        "MusicTagNative.Application": {
            "title": "音乐标签 (MusicTag Native)",
            "icon": "images/icon_{0}.png",
            "type": "url",
            "protocol": "http",
            "port": "8002",
            "url": "/",
            "allUsers": true
        }
    }
}
"""
    with open(os.path.join(ui_dir, "config"), "wb") as f:
        f.write(ensure_lf(ui_config_content))
    log("Written app/ui/config")

    # 6. cmd/main (服务启停与运行状态控制；普通字符串，脚本内 ${VAR} 原样写出)
    cmd_main_content = """#!/bin/bash

LOG_FILE="${TRIM_PKGVAR}/info.log"
PID_FILE="${TRIM_PKGVAR}/app.pid"

mkdir -p "${TRIM_PKGVAR}" 2>/dev/null || true

log_msg() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "${LOG_FILE}"
}

# 解释器优先级：install_callback 创建的 venv > fnOS Python 运行时 > 系统 Python
get_python() {
    if [ -x "${TRIM_APPDEST}/venv/bin/python3" ]; then
        echo "${TRIM_APPDEST}/venv/bin/python3"
    elif [ -x "/var/apps/python312/target/bin/python3" ]; then
        echo "/var/apps/python312/target/bin/python3"
    elif command -v python3 >/dev/null 2>&1; then
        command -v python3
    elif [ -x "/usr/bin/python3" ]; then
        echo "/usr/bin/python3"
    else
        echo ""
    fi
}

check_pid() {
    local pid=$1
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        return 0
    else
        return 1
    fi
}

status() {
    if [ -f "${PID_FILE}" ]; then
        local pid
        pid=$(head -n 1 "${PID_FILE}" | tr -d '[:space:]')
        if check_pid "${pid}"; then
            return 0
        else
            rm -f "${PID_FILE}"
        fi
    fi
    return 1
}

# 确保运行目录存在且归应用用户所有（应用以 package 用户运行，
# 安装/升级脚本写入的 var 目录可能属于 root，导致日志写不进去、授权挂载不可读）
ensure_dirs() {
    mkdir -p "${TRIM_PKGVAR}/logs" "${TRIM_PKGVAR}/tmp" "${TRIM_PKGVAR}/plugins" 2>/dev/null || true
    local owner="${TRIM_USERNAME:-}"
    local group="${TRIM_GROUPNAME:-${owner}}"
    if [ -n "${owner}" ]; then
        chown -R "${owner}:${group}" "${TRIM_PKGVAR}" 2>/dev/null || true
        # 授权目录挂载点（shares/ 下的软链接）在个别版本里属主不对，修正读取权限
        chmod -R a+rX "${TRIM_APPDEST}/shares" 2>/dev/null || true
    fi
}

start_process() {
    if status; then
        log_msg "MusicTag Native is already running."
        return 0
    fi

    ensure_dirs

    PY_BIN=$(get_python)
    if [ -z "${PY_BIN}" ]; then
        log_msg "ERROR: 未找到可用的 Python 3 解释器（请先完成安装依赖）。"
        if [ -n "${TRIM_TEMP_LOGFILE}" ]; then
            echo "未找到可用的 Python 3 解释器，请在应用中心安装 Python 3.11/3.12 运行时后重试。" > "${TRIM_TEMP_LOGFILE}"
        fi
        return 1
    fi
    log_msg "Using Python executable: ${PY_BIN}"

    # 安装阶段已创建 venv 并装好依赖，这里只把应用目录加入模块搜索路径
    export PYTHONPATH="${TRIM_APPDEST}:${PYTHONPATH:-}"
    export PYTHONUNBUFFERED=1
    export MUSICTAG_HOST="${MUSICTAG_HOST:-0.0.0.0}"
    export MUSICTAG_PORT="${TRIM_SERVICE_PORT:-8002}"

    log_msg "Starting MusicTag Native server on port ${MUSICTAG_PORT}..."
    cd "${TRIM_APPDEST}" || return 1
    nohup "${PY_BIN}" "${TRIM_APPDEST}/server.py" >> "${LOG_FILE}" 2>&1 &
    local new_pid=$!
    printf "%s" "${new_pid}" > "${PID_FILE}"
    log_msg "MusicTag Native started with PID: ${new_pid}"

    # 等待端口就绪（最多 20 秒），启动失败时清理 pid 并返回非 0
    local probe="${TRIM_APPDEST}/venv/bin/python3"
    [ -x "${probe}" ] || probe="${PY_BIN}"
    local count=0
    while [ $count -lt 40 ]; do
        if ! check_pid "${new_pid}"; then
            rm -f "${PID_FILE}"
            log_msg "ERROR: MusicTag Native exited during startup, see ${LOG_FILE}"
            if [ -n "${TRIM_TEMP_LOGFILE}" ]; then
                echo "应用启动失败，请查看 ${LOG_FILE}" > "${TRIM_TEMP_LOGFILE}"
            fi
            return 1
        fi
        if "${probe}" -c 'import socket, sys
s = socket.socket()
s.settimeout(0.5)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)' "${MUSICTAG_PORT}" >/dev/null 2>&1; then
            log_msg "MusicTag Native is listening on ${MUSICTAG_PORT}."
            return 0
        fi
        sleep 0.5
        count=$((count + 1))
    done
    log_msg "WARNING: 启动后 20 秒内未检测到端口 ${MUSICTAG_PORT}，请查看日志。"
    return 0
}

stop_process() {
    log_msg "Stopping MusicTag Native..."
    if [ -f "${PID_FILE}" ]; then
        local pid
        pid=$(head -n 1 "${PID_FILE}" | tr -d '[:space:]')
        if check_pid "${pid}"; then
            kill -TERM "${pid}" 2>/dev/null
            local count=0
            while check_pid "${pid}" && [ $count -lt 15 ]; do
                sleep 1
                count=$((count + 1))
            done
            if check_pid "${pid}"; then
                kill -KILL "${pid}" 2>/dev/null
            fi
        fi
        rm -f "${PID_FILE}"
        log_msg "MusicTag Native stopped."
    fi
    return 0
}

case "$1" in
    start)
        start_process
        exit $?
        ;;
    stop)
        stop_process
        exit $?
        ;;
    status)
        if status; then
            exit 0
        else
            exit 3
        fi
        ;;
    *)
        echo "Usage: $0 {start|stop|status}"
        exit 1
        ;;
esac
"""
    with open(os.path.join(cmd_dir, "main"), "wb") as f:
        f.write(ensure_lf(cmd_main_content))

    # 7. cmd 生命周期脚本（普通字符串，脚本内 ${VAR} 原样写出）
    install_callback_content = """#!/bin/bash
LOG_FILE="${TRIM_PKGVAR}/install.log"
exec >> "${LOG_FILE}" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Running install_callback..."
echo "[$(date '+%Y-%m-%d %H:%M:%S')] fnOS ${TRIM_SYS_VERSION:-?} / ${TRIM_SYS_ARCH:-?} / app ${TRIM_APPVER:-?}"

# 确保运行数据与配置目录存在（fnOS 以 package 用户运行）
mkdir -p "${TRIM_PKGVAR}/plugins" "${TRIM_PKGVAR}/logs" "${TRIM_PKGVAR}/tmp" "${TRIM_PKGETC}" 2>/dev/null || true
if [ -n "${TRIM_USERNAME}" ]; then
    chown -R "${TRIM_USERNAME}:${TRIM_GROUPNAME:-${TRIM_USERNAME}}" "${TRIM_PKGVAR}" 2>/dev/null || true
    chown -R "${TRIM_USERNAME}:${TRIM_GROUPNAME:-${TRIM_USERNAME}}" "${TRIM_PKGETC}" 2>/dev/null || true
fi

# 解释器选择必须与 wheels/ 中的二进制轮子 ABI 一致（见 .python-abi 标记文件）。
WHEELS_DIR="${TRIM_APPDEST}/wheels"
ABI_FILE="${WHEELS_DIR}/.python-abi"
WANT_ABI=""
if [ -f "${ABI_FILE}" ]; then
    WANT_ABI=$(tr -d '[:space:]' < "${ABI_FILE}")
fi

py_abi() {
    # 输出解释器的 ABI 标签，例如 cp311；Windows 风格的 CR 会被一并去掉
    "$1" -c 'import sys; print("cp%d%d" % sys.version_info[:2])' 2>/dev/null | tr -d '\r\n'
}

py_ok() {
    # 解释器必须能创建 venv，否则优先换下一个候选
    "$1" -c 'import ensurepip' >/dev/null 2>&1
}

step() {
    # 分步打点：卡住时能一眼看出停在哪一步（飞牛安装日志会实时显示）
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] STEP: $1"
}

CANDIDATES=""
for cand in /var/apps/python312/target/bin/python3 /var/apps/python311/target/bin/python3 /usr/bin/python3 /usr/local/bin/python3; do
    [ -x "${cand}" ] && CANDIDATES="${CANDIDATES} ${cand}"
done
if command -v python3 >/dev/null 2>&1; then
    SYS_PY="$(command -v python3)"
    case " ${CANDIDATES} " in
        *" ${SYS_PY} "*) ;;
        *) CANDIDATES="${CANDIDATES} ${SYS_PY}" ;;
    esac
fi

# 两轮筛选：第一轮要求 ABI 匹配且能建 venv；第二轮放宽为“能建 venv 的解释器”
PY_BIN=""
for cand in ${CANDIDATES}; do
    [ -x "${cand}" ] || continue
    if [ -n "${WANT_ABI}" ] && [ "$(py_abi "${cand}")" != "${WANT_ABI}" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] skip ${cand}: ABI $(py_abi "${cand}") != ${WANT_ABI}"
        continue
    fi
    if py_ok "${cand}"; then
        PY_BIN="${cand}"
        break
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] skip ${cand}: 缺少 ensurepip（无法创建 venv）"
done
if [ -z "${PY_BIN}" ]; then
    for cand in ${CANDIDATES}; do
        [ -x "${cand}" ] || continue
        if py_ok "${cand}"; then
            PY_BIN="${cand}"
            break
        fi
    done
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Wheels ABI: ${WANT_ABI:-<未标记>} | Python detected: ${PY_BIN}"
if [ -n "${PY_BIN}" ] && [ -n "${WANT_ABI}" ] && [ "$(py_abi "${PY_BIN}")" != "${WANT_ABI}" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: 未找到 ${WANT_ABI} 解释器，离线轮子可能不可用，将尝试联网安装。"
fi

fail() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $1" >&2
    if [ -n "${TRIM_TEMP_LOGFILE}" ]; then
        echo "$1" > "${TRIM_TEMP_LOGFILE}"
    fi
    exit 1
}

[ -n "${PY_BIN}" ] || fail "未找到可用的 Python 3 解释器，请在应用中心安装 Python 3.11/3.12 运行时后重试。"

# 创建独立 venv（失败必须报错，不允许静默失败）
# venv 放在代码目录里；飞牛升级会替换代码目录，所以必须检查解释器是否真的可用，
# 不能只看文件是否存在（旧 venv 的软链接可能已失效，导致一路卡在 pip 安装）。
VENV_DIR="${TRIM_APPDEST}/venv"
VENV_OK=0
if "${VENV_DIR}/bin/python3" -c 'import sys' >/dev/null 2>&1 && [ -x "${VENV_DIR}/bin/pip" ]; then
    VENV_OK=1
fi
if [ "${VENV_OK}" -ne 1 ]; then
    step "创建 venv (${PY_BIN})"
    rm -rf "${VENV_DIR}" 2>/dev/null || true
    "${PY_BIN}" -m venv "${VENV_DIR}" || fail "创建 Python 虚拟环境失败（请确认 ${PY_BIN} 带有 ensurepip）"
    [ -x "${VENV_DIR}/bin/pip" ] || fail "venv 创建后缺少 pip：${VENV_DIR}/bin/pip"
    step "venv 就绪"
else
    step "复用已有 venv"
fi

# pip 缓存目录默认在 ${HOME}/.cache（应用用户不可写），显式改到应用数据目录
export PIP_CACHE_DIR="${TRIM_PKGVAR}/tmp/pip-cache"
export PIP_DISABLE_PIP_VERSION_CHECK=1
mkdir -p "${PIP_CACHE_DIR}" 2>/dev/null || true

# 依赖安装：先离线 wheels，失败再回退联网；两者都失败则安装失败
if [ -d "${WHEELS_DIR}" ]; then
    step "离线安装依赖 ($(ls "${WHEELS_DIR}"/*.whl 2>/dev/null | wc -l) 个 wheel)"
    if ! "${VENV_DIR}/bin/pip" install --no-index --find-links "${WHEELS_DIR}" -r "${TRIM_APPDEST}/requirements.txt"; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: 离线安装失败，回退联网安装。"
        step "联网安装依赖"
        "${VENV_DIR}/bin/pip" install -r "${TRIM_APPDEST}/requirements.txt" || fail "离线与联网依赖安装均失败"
    fi
else
    step "联网安装依赖（包内没有 wheels）"
    "${VENV_DIR}/bin/pip" install -r "${TRIM_APPDEST}/requirements.txt" || fail "依赖安装失败"
fi
step "依赖安装完成"

# 安装后自检：确认关键依赖可导入
"${VENV_DIR}/bin/python3" -c "import fastapi, uvicorn, httpx, mutagen, zhconv" || fail "依赖自检失败，关键模块无法导入"

# 预先生成访问令牌（0600），便于安装日志中直接给出访问地址
if [ ! -f "${TRIM_PKGVAR}/auth_token" ]; then
    "${VENV_DIR}/bin/python3" -c "import secrets, pathlib, os; p=pathlib.Path(os.environ['TRIM_PKGVAR'])/'auth_token'; p.write_text(secrets.token_urlsafe(32), encoding='utf-8'); os.chmod(p, 0o600)" || true
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] install_callback completed successfully. 访问地址: http://<NAS地址>:${TRIM_SERVICE_PORT:-8002}"
exit 0
"""
    upgrade_callback_content = install_callback_content.replace(
        "Running install_callback...", "Running upgrade_callback..."
    ).replace(
        "install_callback completed successfully", "upgrade_callback completed successfully"
    )

    uninstall_callback_content = """#!/bin/bash
LOG_FILE="${TRIM_PKGVAR}/uninstall.log"
exec >> "${LOG_FILE}" 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Running uninstall_callback..."

# 仅清理运行时进程与 PID；用户曲库与插件配置保留在 TRIM_PKGVAR 中
PID_FILE="${TRIM_PKGVAR}/app.pid"
if [ -f "${PID_FILE}" ]; then
    PID=$(head -n 1 "${PID_FILE}" | tr -d '[:space:]')
    if [ -n "${PID}" ] && kill -0 "${PID}" 2>/dev/null; then
        kill -TERM "${PID}" 2>/dev/null
        sleep 2
        kill -KILL "${PID}" 2>/dev/null || true
    fi
    rm -f "${PID_FILE}"
fi
exit 0
"""
    simple_stub = "#!/bin/bash\nexit 0\n"

    scripts = {
        "install_init": simple_stub,
        "install_callback": install_callback_content,
        "uninstall_init": simple_stub,
        "uninstall_callback": uninstall_callback_content,
        "upgrade_init": simple_stub,
        "upgrade_callback": upgrade_callback_content,
        "config_init": simple_stub,
        "config_callback": simple_stub,
    }
    for name, content in scripts.items():
        with open(os.path.join(cmd_dir, name), "wb") as f:
            f.write(ensure_lf(content))
    log("Written all cmd lifecycle scripts")

    # 8. 复制应用源码（缺失文件必须报错，避免产出缺模块的坏包）
    src_files = [
        "server.py",
        "scraper_engine.py",
        "tag_engine.py",
        "lyrico_bridge.py",
        "lyrico_host.js",
        "app_paths.py",
        "security.py",
        "requirements.txt"
    ]
    missing = [f for f in src_files if not os.path.isfile(os.path.join(PROJECT_ROOT, f))]
    if missing:
        raise FileNotFoundError(f"缺少必要源文件: {', '.join(missing)}")
    for f in src_files:
        src = os.path.join(PROJECT_ROOT, f)
        dst = os.path.join(app_dir, f)
        shutil.copy2(src, dst)
        log(f"Copied {f} to app/")

    # 复制 Web 静态资源（index.html + 其他静态资源，避免漏包导致 404）
    if not os.path.isdir(os.path.join(PROJECT_ROOT, "web")):
        raise FileNotFoundError("缺少 web/ 目录")
    if os.path.exists(web_dir):
        shutil.rmtree(web_dir)
    shutil.copytree(
        os.path.join(PROJECT_ROOT, "web"),
        web_dir,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )
    log("Copied web/ to app/web/")

    # 桌面入口图标：飞牛桌面只认 ui/images/icon_{64,256}.png（见 app/ui/config），
    # 这里用 web/icon.png 覆盖自动生成的占位图标，保证桌面图标与应用内一致
    source_icon = os.path.join(PROJECT_ROOT, "web", "icon.png")
    if os.path.isfile(source_icon):
        for size, name in ((64, "icon_64.png"), (256, "icon_256.png")):
            target = os.path.join(ui_img_dir, name)
            _resize_icon(source_icon, target, size)
            log(f"Wrote desktop icon {name} from web/icon.png")
        shutil.copy2(source_icon, os.path.join(BUILD_DIR, "ICON_256.PNG"))
        log("Wrote package ICON_256.PNG from web/icon.png")
    else:
        log("Warning: web/icon.png not found, desktop icon uses generated placeholder")

    # 复制 Lyrico 插件目录
    plugins_src = os.path.join(PROJECT_ROOT, "Lyrico-Plugins")
    plugins_dst = os.path.join(app_dir, "Lyrico-Plugins")
    if os.path.exists(plugins_src):
        if os.path.exists(plugins_dst):
            shutil.rmtree(plugins_dst)
        shutil.copytree(
            plugins_src,
            plugins_dst,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "node_modules"),
        )
        log("Copied Lyrico-Plugins to app/Lyrico-Plugins/")
    else:
        log("Warning: Lyrico-Plugins 目录不存在，包内将不含内置插件")

    # 复制离线 wheels（安装阶段由 install_callback 离线安装，不再解压 vendor 重复占体积）
    if os.path.exists(WHEELS_CACHE):
        abi_tags = set()
        for whl in os.listdir(WHEELS_CACHE):
            if whl.endswith(".whl"):
                shutil.copy2(os.path.join(WHEELS_CACHE, whl), os.path.join(wheels_dir, whl))
                match = re.search(r"-(cp\d{3})-cp", os.path.basename(whl))
                if match:
                    abi_tags.add(match.group(1))
        # 记录离线轮子的 ABI，供 install_callback 挑选匹配的解释器
        if len(abi_tags) == 1:
            abi_tag = abi_tags.pop()
            with open(os.path.join(wheels_dir, ".python-abi"), "wb") as f:
                f.write(ensure_lf(abi_tag + "\n"))
            log(f"Bundled offline wheels for install-time pip install (ABI {abi_tag})")
        elif abi_tags:
            raise RuntimeError(
                f"wheels_cache 中存在多个 ABI ({', '.join(sorted(abi_tags))}) 的二进制轮子，"
                "离线安装结果不可预测，请只保留一个 Python 版本的轮子。"
            )
        else:
            log("Bundled offline wheels (pure Python only, no ABI pin)")
    else:
        log("Warning: wheels_cache not found; package will require network for pip install")

def _read_fpk_members(fpk_path: str) -> dict[str, bytes]:
    """读取 fnpack 产出的 .fpk（外层 gzip + 内层 tar），返回 {成员名: 内容}。"""
    with open(fpk_path, "rb") as fh:
        blob = gzip.decompress(fh.read())
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as tar:
        for m in tar.getmembers():
            if m.isfile():
                members[m.name] = tar.extractfile(m).read()
    return members


def verify_fpk(fpk_path: str) -> None:
    """打包后自检：核对官方要求的结构、版本号与关键脚本，发现问题直接失败。"""
    log(f"Verifying {os.path.basename(fpk_path)} ...")
    members = _read_fpk_members(fpk_path)

    errors: list[str] = []
    required = [
        "manifest",
        "config/privilege",
        "config/resource",
        "ICON.PNG",
        "ICON_256.PNG",
        "app.tgz",
        "cmd/main",
        "cmd/install_init",
        "cmd/install_callback",
        "cmd/uninstall_init",
        "cmd/uninstall_callback",
        "cmd/upgrade_init",
        "cmd/upgrade_callback",
        "cmd/config_init",
        "cmd/config_callback",
    ]
    for name in required:
        if name not in members:
            errors.append(f"缺少成员: {name}")

    if "manifest" in members:
        text = members["manifest"].decode("utf-8")
        if f"version               = {APP_VERSION}" not in text and not re.search(
            rf"^version\s*=\s*{re.escape(APP_VERSION)}\s*$", text, re.M
        ):
            errors.append(f"manifest 版本号不是 {APP_VERSION}")
        if re.search(r"^\s*appname\s*=\s*MusicTagNative\s*$", text, re.M) is None:
            errors.append("manifest appname 不是 MusicTagNative")

    for json_name in ("config/privilege", "config/resource"):
        if json_name in members:
            try:
                json.loads(members[json_name].decode("utf-8"))
            except Exception as exc:
                errors.append(f"{json_name} 不是合法 JSON: {exc}")

    for script in members:
        if script.startswith("cmd/") and not members[script].startswith(b"#!/bin/bash"):
            errors.append(f"{script} 缺少 #!/bin/bash shebang")
        if script.startswith("cmd/") and b"\r\n" in members[script]:
            errors.append(f"{script} 含 CRLF 换行")

    if "app.tgz" in members:
        try:
            inner = gzip.decompress(members["app.tgz"])
        except Exception as exc:
            errors.append(f"app.tgz 不是合法 gzip: {exc}")
        else:
            app_files: dict[str, bytes] = {}
            with tarfile.open(fileobj=io.BytesIO(inner), mode="r:") as tar:
                for m in tar.getmembers():
                    if m.isfile():
                        app_files[m.name] = tar.extractfile(m).read()
            for need in (
                "server.py",
                "web/index.html",
                "ui/config",
                "ui/images/icon_64.png",
                "ui/images/icon_256.png",
                "requirements.txt",
            ):
                if not any(n.endswith(need) for n in app_files):
                    errors.append(f"app.tgz 缺少 {need}")
            server_py = next((n for n in app_files if n.endswith("server.py")), None)
            if server_py and f'APP_VERSION = "{APP_VERSION}"'.encode() not in app_files[server_py]:
                errors.append(f"包内 server.py 的 APP_VERSION 与 {APP_VERSION} 不一致")

    if errors:
        for err in errors:
            log(f"❌ {err}")
        raise SystemExit("打包自检失败，产物不符合飞牛官方规范。")
    log(f"✅ 自检通过：{len(members)} 个包内成员，版本 {APP_VERSION}")


def build_with_fnpack():
    log("Invoking official fnpack.exe build...")
    res = subprocess.run([FNPACK_EXE, "build", "-d", BUILD_DIR], cwd=PROJECT_ROOT, capture_output=True, text=True)
    print("STDOUT:\n", res.stdout)
    if res.stderr:
        print("STDERR:\n", res.stderr)
    if res.returncode != 0:
        log(f"Build failed with exit code: {res.returncode}")
        sys.exit(res.returncode)

    # 查找产出的 fpk 文件
    fpk_out = os.path.join(PROJECT_ROOT, "MusicTagNative.fpk")
    target_fpk = os.path.join(PROJECT_ROOT, f"MusicTagNative-{APP_VERSION}.fpk")
    if not os.path.exists(fpk_out):
        raise SystemExit("未找到 fnpack 产出 MusicTagNative.fpk")
    if os.path.exists(target_fpk):
        os.remove(target_fpk)
    shutil.move(fpk_out, target_fpk)
    log(f"Output FPK: {target_fpk} ({os.path.getsize(target_fpk)} bytes)")

    verify_fpk(target_fpk)
    log("Success! 可按飞牛官方流程在应用中心「手动安装」上传该包。")


if __name__ == "__main__":
    setup_scaffold()
    build_with_fnpack()
