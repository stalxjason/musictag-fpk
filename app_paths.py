"""
飞牛 fnOS 应用路径解析 (App Paths)
====================================
严格遵循飞牛应用框架规范：
    https://developer.fnnas.com/docs/core-concepts/framework/
    https://developer.fnnas.com/docs/core-concepts/environment-variables/

- 可写运行数据必须放在 TRIM_PKGVAR（/var/apps/<appname>/var），
  而不是安装目录 TRIM_APPDEST（/var/apps/<appname>/target）。
- 配置放在 TRIM_PKGETC（/var/apps/<appname>/etc）。
- 本地开发 / Windows 环境自动回退到用户数据目录。

可通过环境变量 MUSICTAG_STATE_DIR 强制指定状态根目录（测试用）。
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger("MusicTagPaths")

APP_NAME = (os.environ.get("TRIM_APPNAME") or "MusicTagNative").strip() or "MusicTagNative"

# 源码 / 安装目录（只读）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
BUNDLED_PLUGINS_DIR = os.path.join(BASE_DIR, "Lyrico-Plugins")

# 音频扩展名（扫描、写入、接口校验共用）
AUDIO_EXTS = (".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".wav")


def _env_dir(name: str) -> str:
    value = (os.environ.get(name) or "").strip().strip("'\"")
    return value


def _user_data_dir() -> str:
    if sys.platform == "win32":
        base = _env_dir("LOCALAPPDATA") or _env_dir("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_NAME)
    if sys.platform == "darwin":
        return os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
    base = _env_dir("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, APP_NAME.lower())


def _ensure_writable_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except Exception as exc:  # pragma: no cover - 取决于部署环境
        logger.warning("目录不可写 %s: %s", path, exc)
        return False


def _pick_dir(*candidates: str) -> str:
    """按顺序选择第一个可写目录，全部失败则抛出明确错误。"""
    for cand in candidates:
        if not cand:
            continue
        if _ensure_writable_dir(cand):
            return os.path.abspath(cand)
    raise RuntimeError(
        "无法找到可写的运行数据目录。飞牛系统请确认 TRIM_PKGVAR 可用；"
        "本地运行可设置 MUSICTAG_STATE_DIR 指向可写目录。"
    )


# 状态根目录：优先显式指定 -> fnOS var -> 用户数据目录/var
STATE_DIR = _pick_dir(
    _env_dir("MUSICTAG_STATE_DIR"),
    _env_dir("TRIM_PKGVAR"),
    os.path.join(_user_data_dir(), "var"),
)

# 配置目录：fnOS etc -> 状态目录
CONFIG_DIR = _pick_dir(
    _env_dir("MUSICTAG_CONFIG_DIR"),
    _env_dir("TRIM_PKGETC"),
    os.path.join(STATE_DIR, "config"),
)

PLUGINS_DIR = _pick_dir(os.path.join(STATE_DIR, "plugins"))
LOG_DIR = _pick_dir(os.path.join(STATE_DIR, "logs"))
TMP_DIR = _pick_dir(os.path.join(STATE_DIR, "tmp"))

CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CACHE_FILE = os.path.join(STATE_DIR, "library_cache.json")
TOKEN_FILE = os.path.join(STATE_DIR, "auth_token")
LOG_FILE = os.path.join(LOG_DIR, "app.log")


def state_path(*parts: str) -> str:
    """状态目录下的绝对路径（不创建文件）。"""
    return os.path.join(STATE_DIR, *parts)


def ensure_layout() -> None:
    """确保运行目录结构存在（幂等）。"""
    for directory in (STATE_DIR, CONFIG_DIR, PLUGINS_DIR, LOG_DIR, TMP_DIR):
        os.makedirs(directory, exist_ok=True)


def describe() -> dict:
    return {
        "base_dir": BASE_DIR,
        "state_dir": STATE_DIR,
        "config_dir": CONFIG_DIR,
        "config_file": CONFIG_FILE,
        "cache_file": CACHE_FILE,
        "plugins_dir": PLUGINS_DIR,
        "log_file": LOG_FILE,
        "fnos": bool(os.environ.get("TRIM_PKGVAR")),
    }
