"""
飞牛 NAS 原生音乐刮削服务 - 后端 Web API 服务 (FastAPI + Uvicorn)
===================================================================
1. 内存占用极低 (< 30MB)，基于异步 FastAPI + Uvicorn 原生运行；
2. 提供音乐目录扫描、内嵌封面实时预览流、单曲在线多源匹配、标签安全写入；
3. 支持带实时进度反馈的后台异步批量刮削流水线；
4. 提供开箱即用的现代化自适应单页 Web 控制台。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import urlsplit

# 将同级 vendor 目录置于 sys.path 顶层（仅开发环境存在；生产使用 venv）
_vendor_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if os.path.isdir(_vendor_dir) and _vendor_dir not in sys.path:
    sys.path.insert(0, _vendor_dir)

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

import security
from app_paths import (
    AUDIO_EXTS,
    BASE_DIR,
    BUNDLED_PLUGINS_DIR,
    CACHE_FILE,
    CONFIG_FILE,
    LOG_DIR,
    LOG_FILE,
    PLUGINS_DIR,
    STATE_DIR,
    TMP_DIR,
    TOKEN_FILE,
    WEB_DIR,
    describe as describe_paths,
    ensure_layout,
)
from scraper_engine import MusicCandidate, MusicScraperEngine
from tag_engine import AudioMetadata, AudioTagEngine, to_simplified_chinese, sanitize_filename

# 保证 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ensure_layout()

# 日志配置：控制台 + 轮转文件（状态目录，遵循 fnOS var 目录规范）
#
# 注意：scraper_engine / tag_engine 在导入时已调用过 logging.basicConfig()，
# 先导入的子模块会抢先注册根 handler，导致这里的 basicConfig 变成空操作（日志只进控制台、不落盘）。
# 因此这里不依赖 basicConfig，而是直接挂载 handler（reconfigure 模式，兼容被二次导入/测试环境）。
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 2
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
_log_handlers: List[logging.Handler] = [logging.StreamHandler()]
try:
    _log_handlers.append(
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        )
    )
except OSError:
    pass
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATEFMT,
    handlers=_log_handlers,
    force=True,
)
# uvicorn 自带 logger 默认不向上传递，统一接管后即可一并进入 app.log（排查 5xx / 启动失败时很有用）
for _uvicorn_logger in ("uvicorn", "uvicorn.error"):
    _uv_log = logging.getLogger(_uvicorn_logger)
    _uv_log.handlers = []
    _uv_log.propagate = True
# 访问日志保留在控制台（飞牛 info.log），但不写进状态目录的 app.log，避免日志刷屏
logging.getLogger("uvicorn.access").setLevel(logging.INFO)
logging.getLogger("uvicorn.access").propagate = False
logger = logging.getLogger("MusicTagServer")

APP_VERSION = "1.0.4"
PROCESS_STARTED_AT = time.time()
# 前端统一追加审计日志的记录器；单条日志最长长度（防止恶意超长内容撑爆日志盘）
CLIENT_LOG_NAME = "MusicTagClient"
CLIENT_LOG_MAX_CHARS = 2000
# 日志查看接口单次最多返回的行数
LOG_MAX_LINES = 5000

# -----------------------------------------------------------------------------
# 访问令牌与安全中间件
# -----------------------------------------------------------------------------
AUTH_TOKEN = security.load_or_create_token(TOKEN_FILE)
AUTH_COOKIE = "musictag_token"
# 无需令牌即可访问的路径（首页需要下发令牌，图标用于 <img>）
PUBLIC_PATHS = {"/", "/index.html", "/icon.png", "/favicon.ico", "/api/health"}
# 视为反代回源地址的主机名（只有这些 Host 才允许采信 X-Forwarded-Host）
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

def _env_host() -> str:
    return (os.environ.get("MUSICTAG_HOST") or "0.0.0.0").strip() or "0.0.0.0"


def _env_port() -> int:
    """端口优先级：MUSICTAG_PORT > TRIM_SERVICE_PORT > PORT > 8002（配置文件由 main 再兜底）。"""
    for name in ("MUSICTAG_PORT", "TRIM_SERVICE_PORT", "PORT"):
        raw = (os.environ.get(name) or "").strip()
        if raw.isdigit() and 1 <= int(raw) <= 65535:
            return int(raw)
    return 8002


def _request_token(request: Request) -> Optional[str]:
    token = request.headers.get("x-app-token")
    if token:
        return token.strip()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(AUTH_COOKIE)


def _effective_host(request: Request) -> str:
    """取客户端实际访问的主机名（兼容 Nginx / 飞牛远程访问 / Cloudflare Tunnel 等反代）。

    仅当头 Host 是本机回环地址时才信任 X-Forwarded-Host，
    避免攻击者直连服务时伪造该头绕过同源校验。
    """
    host = request.headers.get("host", "")
    forwarded_host = request.headers.get("x-forwarded-host", "")
    if not forwarded_host:
        return host
    hostname = host.rsplit(":", 1)[0].strip("[]").lower()
    if not hostname:
        return host
    if hostname in _LOOPBACK_HOSTS:
        candidate = forwarded_host.split(",")[0].strip()
        if candidate:
            return candidate
    return host


def _same_origin(request: Request) -> bool:
    """有 Origin 头时必须与 Host 一致；Sec-Fetch-Site 不允许跨站。"""
    site = (request.headers.get("sec-fetch-site") or "").lower()
    if site and site not in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    if not origin:
        return True
    try:
        netloc = urlsplit(origin).netloc
    except ValueError:
        return False
    host = _effective_host(request)
    return bool(host) and netloc.lower() == host.lower()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    paths = describe_paths()
    logger.info(
        "应用启动 | 版本 %s | 代码目录 %s | 状态目录 %s",
        APP_VERSION,
        paths["base_dir"],
        paths["state_dir"],
    )
    # 启动自检：把监听地址、授权挂载与音乐库现状写进日志，排查「打不开 / 目录为空」时不用再猜
    try:
        logger.info(
            "监听 %s:%s | 音乐库 %s",
            _env_host(),
            _env_port(),
            load_config().get("music_dir") or "<未设置>",
        )
        roots = _candidate_share_roots()
        logger.info("授权挂载候选目录: %s", ", ".join(roots) if roots else "<未发现 shares/ 目录>")
        shared = _discover_shared_entries()
        logger.info(
            "已识别授权目录: %s",
            ", ".join("%s -> %s" % (item["name"], item["target"]) for item in shared)
            if shared
            else "<无，请在飞牛「访问权限」中添加音乐目录>",
        )
        free_port = _probe_listen(_env_host(), _env_port())
        if not free_port:
            logger.warning(
                "端口 %s 已被其它进程占用（或本机无法绑定），访问会失败，请检查是否有残留进程",
                _env_port(),
            )
    except Exception as exc:  # 自检失败不能影响启动
        logger.warning("启动自检异常: %s", exc)
    yield
    try:
        tag_engine.close()
    except Exception:
        pass
    try:
        from lyrico_bridge import lyrico_bridge

        lyrico_bridge.shutdown()
    except Exception:
        pass


app = FastAPI(title="飞牛 NAS 原生音乐刮削服务", version=APP_VERSION, lifespan=_lifespan)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_PATHS:
        supplied = _request_token(request)
        if not security.token_equals(AUTH_TOKEN, supplied):
            return JSONResponse(
                {"detail": "未授权：缺少或错误的访问令牌", "code": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
        return JSONResponse({"detail": "跨站请求被拒绝"}, status_code=403)

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: https: http:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    )
    return response

# -----------------------------------------------------------------------------
# 配置模型与读写
# -----------------------------------------------------------------------------
KNOWN_SOURCES = ["itunes", "netease", "qqmusic", "kuwo", "musicbrainz"]


class AppConfig(BaseModel):
    """持久化配置。读取时忽略未知字段，写入时严格校验。"""

    model_config = ConfigDict(extra="ignore")

    music_dir: str = ""
    sources: List[str] = Field(default_factory=lambda: list(KNOWN_SOURCES))
    min_score: float = Field(default=85.0, ge=0.0, le=100.0)
    auto_save_lrc: bool = True
    auto_embed_cover: bool = True
    port: int = Field(default=8002, ge=1, le=65535)
    enabled_lyrico_plugins: List[str] = Field(default_factory=list)
    request_timeout: float = Field(default=8.0, ge=1.0, le=60.0)
    plugin_execution_enabled: bool = True

    @field_validator("sources")
    @classmethod
    def _validate_sources(cls, value: List[str]) -> List[str]:
        allowed = set(KNOWN_SOURCES)
        cleaned: List[str] = []
        for item in value or []:
            if item in allowed and item not in cleaned:
                cleaned.append(item)
        if not cleaned:
            raise ValueError("至少需要启用一个数据源")
        return cleaned

    @field_validator("enabled_lyrico_plugins")
    @classmethod
    def _validate_plugins(cls, value: List[str]) -> List[str]:
        cleaned: List[str] = []
        for item in value or []:
            key = security.normalize_plugin_key(item)
            if key and key not in cleaned:
                cleaned.append(key)
        if len(cleaned) > 64:
            raise ValueError("启用的插件数量过多")
        return cleaned


class ConfigUpdate(BaseModel):
    """配置更新请求（未知字段一律拒绝，避免配置污染）。"""

    model_config = ConfigDict(extra="forbid")

    music_dir: Optional[str] = None
    sources: Optional[List[str]] = None
    min_score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    auto_save_lrc: Optional[bool] = None
    auto_embed_cover: Optional[bool] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    enabled_lyrico_plugins: Optional[List[str]] = None
    request_timeout: Optional[float] = Field(default=None, ge=1.0, le=60.0)
    plugin_execution_enabled: Optional[bool] = None


def get_default_music_dir() -> str:
    """根据运行平台探测默认音乐目录（不创建目录）。"""
    if sys.platform == "win32":
        for candidate in (r"D:\Music", os.path.expanduser(r"~\Music")):
            if os.path.isdir(candidate):
                return candidate
        return ""

    candidates: List[str] = []
    # 1) 飞牛「访问权限」授权的目录（应用启动时挂载到 shares/）
    for env_name in ("TRIM_DATA_ACCESSIBLE_PATHS", "TRIM_DATA_SHARE_PATHS"):
        for item in re.split(r"[:\n;,]+", os.environ.get(env_name) or ""):
            item = item.strip()
            if item:
                candidates.append(item)
    try:
        candidates.extend(item["target"] for item in _discover_shared_entries())
    except Exception:
        pass
    # 2) 常规音乐库位置
    #    注意：不要再把 <应用>/shares/musictag(/data) 作为候选，那是应用私有 data-share，
    #    里面不会有用户的音乐，选中它只会让首次扫描结果为 0 首。
    candidates.extend(
        [
            "/vol1/Music",
            "/vol1/music",
            "/vol2/Music",
            "/vol2/music",
            os.path.join(_env_path("TRIM_PKGVAR") or "", "music"),
        ]
    )
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return candidate
    return "/vol1" if os.path.isdir("/vol1") else ""


def load_config() -> Dict[str, Any]:
    """读取配置：非法字段回退默认值；目录不可用时不静默重置用户路径。"""
    cfg = AppConfig().model_dump()
    raw = security.read_json(CONFIG_FILE, {})
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key not in cfg:
                continue
            try:
                cfg = AppConfig(**{**cfg, key: value}).model_dump()
            except (ValidationError, ValueError, TypeError) as exc:
                logger.warning("忽略非法配置项 %s: %s", key, exc)

    if not cfg["music_dir"]:
        cfg["music_dir"] = get_default_music_dir()
    if not os.path.exists(CONFIG_FILE) and not cfg["enabled_lyrico_plugins"]:
        # 首次运行：默认启用随包内置的插件（可在界面中关闭）
        cfg["enabled_lyrico_plugins"] = [
            key for key in ("apple", "lrcshare", "musicbrainz", "kugou") if key in _bundled_plugin_keys()
        ]
    if cfg["music_dir"] and not os.path.isdir(cfg["music_dir"]):
        logger.warning("音乐目录当前不可用: %s（保留配置，不自动改写）", cfg["music_dir"])
    return cfg


def save_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """原子写入配置；失败时抛出异常，由调用方返回 500（不静默失败）。"""
    validated = AppConfig(**cfg).model_dump()
    security.atomic_write_json(CONFIG_FILE, validated, mode=0o600)
    return validated


# 初始化核心引擎
scraper_engine = MusicScraperEngine(timeout=8.0)
tag_engine = AudioTagEngine(
    timeout=10.0,
    cache_file=CACHE_FILE,
    state_dir=STATE_DIR,
)


# 全局内存缓存
class GlobalState:
    scanned_files: List[AudioMetadata] = []
    scanned_dir: str = ""

    scan_lock = asyncio.Lock()
    batch_lock = asyncio.Lock()
    batch_task: Optional[asyncio.Task] = None

    # 批量刮削任务状态
    batch_running: bool = False
    batch_total: int = 0
    batch_current: int = 0
    batch_success: int = 0
    batch_skipped: int = 0
    batch_failed: int = 0
    batch_current_file: str = ""
    batch_logs: List[str] = []
    batch_stop_requested: bool = False


state = GlobalState()


# -----------------------------------------------------------------------------
# 请求数据模型
# -----------------------------------------------------------------------------
class MatchRequest(BaseModel):
    filepath: str = Field(min_length=1, max_length=4096)
    title: Optional[str] = Field(default=None, max_length=300)
    artist: Optional[str] = Field(default=None, max_length=300)
    query: Optional[str] = Field(default=None, max_length=300)
    sources: Optional[List[str]] = None


class ConvertZhRequest(BaseModel):
    text: Optional[str] = None
    texts: Optional[List[str]] = None
    fields: Optional[Dict[str, str]] = None


class RenameRequest(BaseModel):
    filepath: str = Field(min_length=1, max_length=4096)
    new_filename: str = Field(min_length=1, max_length=255)
    sync_lrc: bool = True


class WriteTagRequest(BaseModel):
    filepath: str = Field(min_length=1, max_length=4096)
    candidate: Dict[str, Any]
    embed_cover: bool = True
    save_lrc: bool = True
    convert_to_simplified: bool = False
    rename_file: bool = False
    new_filename: Optional[str] = Field(default=None, max_length=255)


class BatchScrapeRequest(BaseModel):
    dir_path: Optional[str] = None
    min_score: float = Field(default=85.0, ge=0.0, le=100.0)
    dry_run: bool = False
    overwrite_existing: bool = False
    filepaths: Optional[List[str]] = None
    # 'skip_complete' | 'fill_missing' | 'overwrite_all'
    overwrite_mode: Literal["skip_complete", "fill_missing", "overwrite_all"] = "skip_complete"
    convert_to_simplified: bool = False


class AlbumApplyItem(BaseModel):
    filepath: str = Field(min_length=1, max_length=4096)
    track_number: Optional[int] = Field(default=None, ge=0, le=999)
    total_tracks: Optional[int] = Field(default=None, ge=0, le=999)
    title: Optional[str] = Field(default=None, max_length=300)
    artist: Optional[str] = Field(default=None, max_length=300)
    source: Optional[str] = Field(default=None, max_length=32)
    source_id: Optional[str] = Field(default=None, max_length=128)
    new_filename: Optional[str] = Field(default=None, max_length=255)


class AlbumApplyRequest(BaseModel):
    album_name: str = Field(min_length=1, max_length=300)
    album_artist: Optional[str] = Field(default=None, max_length=300)
    cover_url: Optional[str] = Field(default=None, max_length=4096)
    year: Optional[str] = Field(default=None, max_length=10)
    date: Optional[str] = Field(default=None, max_length=20)
    genre: Optional[str] = Field(default=None, max_length=100)
    company: Optional[str] = Field(default=None, max_length=200)
    embed_cover: bool = True
    save_lrc: bool = True
    overwrite_existing: bool = True
    convert_to_simplified: bool = False
    rename_files: bool = False
    rename_pattern: Optional[str] = Field(default="track-title", max_length=50)
    items: List[AlbumApplyItem] = Field(max_length=500)


class OrganizePreviewRequest(BaseModel):
    filepaths: Optional[List[str]] = None
    pattern: str = Field(default="standard", max_length=200)
    convert_to_simplified: bool = False
    conflict_mode: Literal["rename", "skip", "overwrite"] = "rename"
    sync_lrc: bool = True
    clean_empty_dirs: bool = True


class OrganizeExecuteRequest(BaseModel):
    filepaths: Optional[List[str]] = None
    pattern: str = Field(default="standard", max_length=200)
    convert_to_simplified: bool = False
    conflict_mode: Literal["rename", "skip", "overwrite"] = "rename"
    sync_lrc: bool = True
    clean_empty_dirs: bool = True


# -----------------------------------------------------------------------------
# 路径与上传辅助
# -----------------------------------------------------------------------------
def _current_roots() -> List[str]:
    return security.build_roots(load_config().get("music_dir", ""))


def _require_allowed_audio(filepath: str) -> str:
    """校验音频路径位于授权根目录内，返回规范化后的路径。"""
    if not filepath or not isinstance(filepath, str):
        raise HTTPException(status_code=400, detail="文件路径不能为空")
    target = os.path.abspath(filepath)
    if not security.is_allowed_audio(target, _current_roots()):
        if not os.path.isfile(target):
            raise HTTPException(status_code=404, detail="文件不存在或不是受支持的音频文件")
        raise HTTPException(status_code=403, detail="该路径不在音乐库授权目录内")
    return target


def _require_allowed_dir(path: str) -> str:
    target = os.path.abspath(path or "")
    if not target or not os.path.isdir(target):
        raise HTTPException(status_code=404, detail="目录不存在")
    if not security.is_allowed_path(target, _current_roots()):
        raise HTTPException(status_code=403, detail="该目录不在授权范围内")
    return target


async def _read_limited_body(request: Request, max_bytes: int) -> bytes:
    """流式读取请求体并限制大小，避免内存耗尽。"""
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > max_bytes:
                raise HTTPException(status_code=413, detail="上传内容超过大小限制")
        except ValueError:
            raise HTTPException(status_code=400, detail="非法的 Content-Length")
    chunks: List[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="上传内容超过大小限制")
        chunks.append(chunk)
    return b"".join(chunks)


def _find_plugin_manifest(src_dir: str, max_depth: int = 3) -> Optional[str]:
    """在解压目录中浅层查找 manifest.json，避免遍历超大目录。"""
    direct = os.path.join(src_dir, "manifest.json")
    if os.path.isfile(direct):
        return direct
    for root, dirs, files in os.walk(src_dir):
        depth = os.path.relpath(root, src_dir).count(os.sep)
        if depth >= max_depth:
            dirs[:] = []
            continue
        if "manifest.json" in files:
            return os.path.join(root, "manifest.json")
    return None


def _bundled_plugin_keys() -> set:
    try:
        return {
            name
            for name in os.listdir(BUNDLED_PLUGINS_DIR)
            if os.path.isdir(os.path.join(BUNDLED_PLUGINS_DIR, name))
        }
    except OSError:
        return set()


def _bundled_plugin_ids() -> set:
    """内置插件 manifest 中声明的 id 集合（用于拒绝冒用内置身份的上传）。"""
    ids = set()
    try:
        for name in os.listdir(BUNDLED_PLUGINS_DIR):
            manifest_path = os.path.join(BUNDLED_PLUGINS_DIR, name, "manifest.json")
            if not os.path.isfile(manifest_path):
                continue
            try:
                with open(manifest_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict) and data.get("id"):
                    ids.add(str(data["id"]).strip().lower())
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return ids


# -----------------------------------------------------------------------------
# REST API 端点
# -----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """提供单文件 Web 控制台，并注入本次会话的访问令牌。"""
    html_path = os.path.join(WEB_DIR, "index.html")
    try:
        with open(html_path, "r", encoding="utf-8") as fh:
            html = fh.read()
    except OSError:
        raise HTTPException(status_code=500, detail="Web 控制台资源缺失，请检查安装完整性")

    token_script = f'<script>window.__APP_TOKEN__="{AUTH_TOKEN}";</script>'
    if "</head>" in html:
        html = html.replace("</head>", token_script + "</head>", 1)
    else:
        html = token_script + html

    response = HTMLResponse(html)
    response.set_cookie(
        AUTH_COOKIE,
        AUTH_TOKEN,
        httponly=True,
        samesite="strict",
        path="/",
        max_age=7 * 24 * 3600,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
async def health_check():
    """健康检查（公开，不包含任何敏感信息；供前端日志页展示服务运行状态）。"""
    return {
        "status": "ok",
        "version": APP_VERSION,
        "uptime_seconds": int(time.time() - PROCESS_STARTED_AT),
        "memory_mb": _read_memory_mb(),
    }


@app.get("/icon.png")
@app.get("/favicon.ico")
async def get_app_icon():
    """返回应用图标（优先 Web 目录图标，其次安装包自带的 256px 图标）。"""
    # 注意：按 fnOS 打包规范，ICON_256.PNG / ICON.PNG 位于包根目录；
    # 开发态图标位于仓库根目录与 web/ 目录。
    candidate_paths = [
        os.path.join(WEB_DIR, "icon.png"),
        os.path.join(BASE_DIR, "icon.png"),
        os.path.join(BASE_DIR, "ICON_256.PNG"),
        os.path.join(BASE_DIR, "ICON.PNG"),
    ]
    for path in candidate_paths:
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                return Response(content=fh.read(), media_type="image/png")
    return Response(status_code=404)


# 应用共享目录下这些名字属于应用自身资源（飞牛框架的 share/ 资源目录等），
# 不算用户授权的共享文件夹。注意：不要加入 "data"，用户完全可以授权一个名为 data 的文件夹。
_SHARE_RESERVED_NAMES = frozenset({
    "bin", "lib", "lib64", "share", "shares", "var", "etc", "tmp", "log", "logs",
    "app", "cmd", "config", "target", "wizard", "ui", "venv", "node_modules",
    "__pycache__", "web", "python", "python3", "usr",
})


def _env_path(name: str) -> str:
    """读取飞牛框架注入的路径环境变量（去掉可能的引号与空白）。"""
    return (os.environ.get(name) or "").strip().strip("'\"")


def _is_share_name_user_dir(name: str) -> bool:
    """shares/ 下的条目名是否可能是用户授权文件夹。"""
    value = (name or "").strip()
    if not value or value.startswith("."):
        return False
    return value.lower() not in _SHARE_RESERVED_NAMES


def _candidate_share_roots() -> List[str]:
    """可能存放「访问权限」授权挂载的应用共享目录（去重，只保留真实存在的目录）。

    只有飞牛应用框架注入 TRIM_APPDEST / TRIM_PKGAPPDIR 时才扫描应用目录下的
    shares/、share/（TRIM_PKGAPPDIR 是包根目录，不能整目录扫描，否则会混进
    app/cmd/config 等应用自身目录）。
    """
    if not os.environ.get("TRIM_APPDEST") and not os.environ.get("TRIM_PKGAPPDIR"):
        return []
    app_dirs = [d for d in (_env_path("TRIM_APPDEST"), _env_path("TRIM_PKGAPPDIR")) if d]
    app_dirs.extend(["/var/apps/MusicTagNative/target", "/var/apps/MusicTagNative"])
    roots: List[str] = []
    seen = set()
    for app_dir in app_dirs:
        for tail in ("shares", "share"):
            root = os.path.join(app_dir, tail)
            key = os.path.normcase(os.path.abspath(root))
            if key not in seen and os.path.isdir(root):
                seen.add(key)
                roots.append(root)
    return roots


def _discover_shared_entries() -> List[Dict[str, Any]]:
    """列出应用共享目录下真正的授权挂载项。

    飞牛在应用启动时把「访问权限」授权的文件夹挂载到 <应用目录>/shares/<名字>，
    shares/ 本身也可能是一个指向框架共享根目录的链接。
    返回 [{"name": 显示名, "link": 链接路径, "target": 真实目录, "readable": bool}]。
    应用私有 data-share（<存储>/@appshare/<应用名>）不是用户音乐目录，需要排除。
    """
    app_name = os.path.basename(_env_path("TRIM_APPDEST")) or "MusicTagNative"
    internal_roots = set()
    for app_dir in (_env_path("TRIM_APPDEST"), _env_path("TRIM_PKGAPPDIR")):
        if app_dir:
            internal_roots.add(os.path.normcase(os.path.realpath(app_dir)))

    result: List[Dict[str, Any]] = []
    seen_targets = set()
    for share_root in _candidate_share_roots():
        try:
            entries = sorted(os.scandir(share_root), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            name = str(entry.name)
            try:
                is_link = os.path.islink(entry.path)
            except OSError:
                is_link = False
            target = os.path.realpath(entry.path) if is_link else entry.path
            if not _is_share_name_user_dir(name):
                continue
            if name.lower() == app_name.lower():
                continue
            if "@appshare" in target.replace("\\", "/").lower():
                continue
            if not os.path.isdir(target):
                continue
            try:
                with os.scandir(target):
                    pass
                readable = True
            except OSError:
                readable = False
            key = os.path.normcase(target)
            if key in seen_targets or key in internal_roots:
                continue
            seen_targets.add(key)
            result.append({"name": name, "link": entry.path, "target": target, "readable": readable})
    return result


def get_accessible_paths() -> List[Dict[str, Any]]:
    """获取系统与飞牛 fnOS 授权的可访问目录列表（按路径去重，授权目录优先）。"""
    results: List[Dict[str, Any]] = []
    seen = set()

    def add_path(p: str, p_type: str, desc: str):
        if not p:
            return
        p = p.strip().strip("'\"")
        key = os.path.normcase(os.path.abspath(p)) if p else ""
        if not p or key in seen:
            return
        seen.add(key)
        exists = os.path.exists(p)
        base = os.path.basename(p.rstrip("/\\"))
        results.append({
            "name": base or p,
            "path": p,
            "type": p_type,
            "desc": desc,
            "exists": exists,
            "is_dir": os.path.isdir(p) if exists else False,
            "readable": os.access(p, os.R_OK) if exists else False,
            "writable": os.access(p, os.W_OK) if exists else False,
        })

    # 1. 飞牛「访问权限」授权的文件夹（应用启动时挂载到 <应用目录>/shares/）
    for shared in _discover_shared_entries():
        add_path(shared["target"], "authorized", "飞牛已授权文件夹 (%s)" % shared["name"])
        add_path(shared["link"], "share", "飞牛共享空间 (%s)" % shared["name"])

    # 2. 框架环境变量里的授权目录（部分版本直接注入）
    for env_var in ("TRIM_DATA_ACCESSIBLE_PATHS", "TRIM_DATA_SHARE_PATHS", "FNOS_ACCESSIBLE_PATHS"):
        val = _env_path(env_var)
        if val:
            for item in re.split(r"[:\n;,]+", val):
                add_path(item, "authorized", "飞牛系统已授权文件夹")

    # 3. 探测飞牛 NAS 硬盘存储空间 (/vol1 ~ /vol8, /volume1 ~ /volume8)
    if sys.platform != "win32":
        for vol_prefix in ("/vol", "/volume"):
            for i in range(1, 9):
                vp = "%s%d" % (vol_prefix, i)
                if os.path.isdir(vp):
                    is_readable = os.access(vp, os.R_OK)
                    add_path(
                        vp, "volume",
                        "飞牛存储空间 %d" % i + (" (读写正常)" if is_readable else " (需在飞牛设置中授权子目录)"),
                    )

    # 4. Windows 本地开发环境
    if sys.platform == "win32" and os.path.isdir(r"D:\Music"):
        add_path(r"D:\Music", "authorized", "本地测试音乐库 (D:\\Music)")

    return results


def _read_memory_mb() -> float:
    """读取当前进程常驻内存（MB）。

    优先使用 Linux 的 /proc（fnOS 目标环境，无额外依赖）；
    Windows / macOS 开发环境回退到 psutil（可选，未安装时返回 0）。
    """
    if os.path.exists("/proc/self/status"):
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        return round(int(line.split()[1]) / 1024.0, 1)
        except (OSError, ValueError, IndexError):
            pass
    try:
        import psutil  # type: ignore  # 仅开发环境可选依赖

        return round(psutil.Process().memory_info().rss / (1024 * 1024), 1)
    except Exception:
        return 0.0


@app.get("/api/config")
async def get_config_api():
    """获取当前系统配置与运行状态"""
    cfg = load_config()
    memory_mb = _read_memory_mb()

    return {
        "config": cfg,
        "authorized_paths": get_accessible_paths(),
        "roots": _current_roots(),
        "runtime": {
            "python_version": sys.version.split()[0],
            "memory_mb": memory_mb,
            "os": sys.platform,
            "version": APP_VERSION,
            "paths": describe_paths(),
        },
    }


@app.post("/api/config")
async def update_config_api(payload: ConfigUpdate):
    """更新系统配置（严格校验字段与取值，写盘失败返回 500）。"""
    current = load_config()
    updates = payload.model_dump(exclude_none=True)

    music_dir = updates.get("music_dir")
    if music_dir is not None:
        music_dir = music_dir.strip()
        if music_dir and not os.path.isabs(music_dir):
            raise HTTPException(status_code=400, detail="音乐目录必须是绝对路径")
        if music_dir and os.path.isdir(music_dir) and not security.is_allowed_path(
            music_dir, _current_roots()
        ):
            raise HTTPException(status_code=403, detail="音乐目录不在系统授权范围内")
        updates["music_dir"] = music_dir

    merged = {**current, **updates}
    try:
        saved = save_config(merged)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"配置校验失败: {exc}")
    except OSError as exc:
        logger.error("写入配置失败: %s", exc)
        raise HTTPException(status_code=500, detail="配置写入失败，请检查应用数据目录权限")
    return {"status": "ok", "config": saved}


@app.get("/api/fs/authorized")
async def get_authorized_paths_api(refresh: bool = False):
    """返回飞牛 fnOS 已授权给应用的目录列表。

    飞牛应用框架会把「访问权限」中新授权的文件夹软链接到应用目录的 shares/ 下，
    刷新页面时带上 refresh=true 可绕过浏览器缓存，确保看到最新授权目录。
    """
    return JSONResponse(
        {"authorized": get_accessible_paths()},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


# -------------------------------------------------------------------------
# Lyrico 外部生态扩展插件管理 API (查询、启用/禁用、上传安装、本地导入、删除、热重载)
# -------------------------------------------------------------------------
def install_plugin_from_dir(src_dir: str, replace: bool = False) -> Dict[str, Any]:
    """从目录安装 Lyrico 插件。

    安全约定：
    - 插件 key 只来自 manifest.id，不接受上传文件名（防止覆盖内置插件）；
    - 内置插件不可被覆盖；
    - 先在插件目录内暂存，校验通过后再原子替换，失败自动回滚。
    """
    from lyrico_bridge import lyrico_bridge

    manifest_path = _find_plugin_manifest(src_dir)
    if not manifest_path:
        raise ValueError("未检测到 manifest.json，不是合法的 Lyrico 插件包")

    manifest_dir = os.path.dirname(manifest_path)
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError(f"解析 manifest.json 失败: {exc}")
    if not isinstance(manifest, dict):
        raise ValueError("manifest.json 内容必须为对象")

    raw_id = str(manifest.get("id") or manifest.get("name") or "").strip()
    # 取 ID 的最后一段作为 key，并跳过 source/plugin 等通用后缀
    parts = [p for p in raw_id.split(".") if p]
    while len(parts) > 1 and parts[-1].lower() in ("source", "plugin"):
        parts.pop()
    suffix = parts[-1] if parts else raw_id
    plugin_key = security.normalize_plugin_key(suffix)
    if not plugin_key:
        raise ValueError("插件 manifest.json 缺少合法 id（仅允许字母、数字、- 和 _）")

    # 二次防护：任何与内置插件同名的 key 一律拒绝（含内置插件自身 manifest 声明的 key）
    bundled_keys = _bundled_plugin_keys()
    bundled_ids = _bundled_plugin_ids()
    if plugin_key in bundled_keys or raw_id.lower() in bundled_ids:
        raise PermissionError("内置插件不允许通过上传覆盖或替换")

    entry = str(manifest.get("entry") or "source.js")
    if os.path.isabs(entry) or ".." in entry.split("/"):
        raise ValueError("插件入口路径非法")
    if not os.path.isfile(os.path.join(manifest_dir, entry)):
        raise ValueError(f"插件入口文件不存在: {entry}")

    include_dirs = manifest.get("includeDirs") or []
    if not isinstance(include_dirs, list) or len(include_dirs) > 20:
        raise ValueError("includeDirs 字段非法")

    if plugin_key in _bundled_plugin_keys():
        raise PermissionError("内置插件不允许通过上传覆盖")

    plugins_dir = lyrico_bridge.plugins_dir
    dest_dir = os.path.join(plugins_dir, plugin_key)
    if not security.is_within(dest_dir, plugins_dir):
        raise ValueError("插件目标路径越界")
    if os.path.exists(dest_dir) and not replace:
        raise ValueError(f"插件 {plugin_key} 已存在，请先删除后再导入")

    staging = tempfile.mkdtemp(prefix=f".staging_{plugin_key}_", dir=plugins_dir)
    backup_dir: Optional[str] = None
    try:
        shutil.copytree(manifest_dir, staging, dirs_exist_ok=True)
        if not os.path.isfile(os.path.join(staging, entry)):
            raise ValueError("插件复制后入口文件缺失")
        if os.path.exists(dest_dir):
            backup_dir = f"{dest_dir}.bak-{int(time.time())}"
            os.replace(dest_dir, backup_dir)
        os.replace(staging, dest_dir)
        staging = ""
        if backup_dir:
            shutil.rmtree(backup_dir, ignore_errors=True)
            backup_dir = None
    except Exception:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
        if backup_dir and os.path.isdir(backup_dir) and not os.path.exists(dest_dir):
            os.replace(backup_dir, dest_dir)
        raise

    lyrico_bridge.discover_plugins()

    cfg = load_config()
    enabled_plugins = set(cfg.get("enabled_lyrico_plugins", []))
    enabled_plugins.add(plugin_key)
    cfg["enabled_lyrico_plugins"] = sorted(enabled_plugins)
    save_config(cfg)

    p_info = lyrico_bridge.plugins.get(plugin_key, {})
    return {
        "status": "success",
        "key": plugin_key,
        "name": p_info.get("name", manifest.get("name", plugin_key)),
        "version": p_info.get("version", manifest.get("versionName", "1.0.0")),
        "author": p_info.get("author", manifest.get("author", "未知")),
        "description": p_info.get("description", manifest.get("description", "")),
        "message": f"插件【{p_info.get('name', plugin_key)}】导入成功并已启用",
    }


@app.get("/api/plugins/lyrico")
async def list_lyrico_plugins_api():
    """获取所有已加载的 Lyrico 扩展插件状态与物理存储目录"""
    from lyrico_bridge import lyrico_bridge
    cfg = load_config()
    enabled_plugins = cfg.get("enabled_lyrico_plugins", ["apple", "lrcshare", "musicbrainz", "kugou"])

    plugins = lyrico_bridge.discover_plugins()
    for p in plugins:
        p["enabled"] = p["key"] in enabled_plugins
        p["icon_url"] = f"/api/plugins/lyrico/icon/{p['key']}" if p.get("has_icon") else "/icon.png"

    return {
        "is_available": lyrico_bridge.is_available,
        "node_path": lyrico_bridge.node_path,
        "plugins_dir": lyrico_bridge.plugins_dir,
        "plugins": plugins
    }


MAX_PLUGIN_UPLOAD_BYTES = 5 * 1024 * 1024


class LocalPluginInstall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)


class PluginToggleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    enabled: bool = False


class PluginKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64)


@app.post("/api/plugins/lyrico/upload")
async def upload_lyrico_plugin(request: Request, filename: str = Query("plugin.zip", max_length=128)):
    """上传并导入 Lyrico 插件（限制大小、拒绝路径穿越与符号链接）。"""
    body = await _read_limited_body(request, MAX_PLUGIN_UPLOAD_BYTES)
    if len(body) < 20:
        raise HTTPException(status_code=400, detail="上传内容为空或不是有效的 ZIP 文件")

    temp_dir = tempfile.mkdtemp(prefix="lyrico_upload_", dir=TMP_DIR)
    try:
        security.safe_extract_zip(body, temp_dir)
        return install_plugin_from_dir(temp_dir)
    except security.UnsafeArchiveError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("上传插件异常")
        raise HTTPException(status_code=500, detail="导入插件失败，请查看日志")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.post("/api/plugins/lyrico/install_local")
async def install_local_lyrico_plugin(payload: LocalPluginInstall):
    """从 NAS 已授权目录（目录或 ZIP 文件）导入 Lyrico 插件。"""
    src_path = os.path.abspath(payload.path.strip())
    if not security.is_allowed_path(src_path, _current_roots()):
        raise HTTPException(status_code=403, detail="该路径不在授权范围内")
    if not os.path.exists(src_path):
        raise HTTPException(status_code=404, detail="指定的本地目录或文件不存在")

    if os.path.isfile(src_path):
        if os.path.getsize(src_path) > MAX_PLUGIN_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="插件压缩包超出大小限制")
        with open(src_path, "rb") as fh:
            data = fh.read(MAX_PLUGIN_UPLOAD_BYTES + 1)
        temp_dir = tempfile.mkdtemp(prefix="lyrico_local_", dir=TMP_DIR)
        try:
            security.safe_extract_zip(data, temp_dir)
            return install_plugin_from_dir(temp_dir)
        except security.UnsafeArchiveError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    if os.path.isdir(src_path):
        try:
            return install_plugin_from_dir(src_path)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    raise HTTPException(status_code=400, detail="不支持的本地路径格式")


@app.post("/api/plugins/lyrico/delete")
async def delete_lyrico_plugin(payload: PluginKeyRequest):
    """删除已安装的第三方插件（内置插件受保护）。"""
    from lyrico_bridge import lyrico_bridge

    plugin_key = security.normalize_plugin_key(payload.key)
    if not plugin_key:
        raise HTTPException(status_code=400, detail="非法的插件标识")
    if plugin_key in _bundled_plugin_keys():
        raise HTTPException(status_code=403, detail="内置插件不允许删除")
    target_dir = os.path.join(lyrico_bridge.plugins_dir, plugin_key)
    if not security.is_within(target_dir, lyrico_bridge.plugins_dir):
        raise HTTPException(status_code=400, detail="越权路径，禁止删除")
    if not os.path.isdir(target_dir):
        raise HTTPException(status_code=404, detail="插件目录不存在")

    try:
        shutil.rmtree(target_dir)
    except OSError as exc:
        logger.error("删除插件失败 (%s): %s", target_dir, exc)
        raise HTTPException(status_code=500, detail="删除插件目录失败")

    cfg = load_config()
    enabled_plugins = set(cfg.get("enabled_lyrico_plugins", []))
    enabled_plugins.discard(plugin_key)
    cfg["enabled_lyrico_plugins"] = sorted(enabled_plugins)
    try:
        save_config(cfg)
    except OSError as exc:
        logger.error("删除插件后写入配置失败: %s", exc)

    plugins = lyrico_bridge.discover_plugins()
    return {
        "status": "success",
        "message": f"插件 {plugin_key} 已删除",
        "remaining_count": len(plugins),
    }


@app.post("/api/plugins/lyrico/reload")
async def reload_lyrico_plugins():
    """重新扫描插件目录并热重载。"""
    from lyrico_bridge import lyrico_bridge

    cfg = load_config()
    enabled_plugins = set(cfg.get("enabled_lyrico_plugins", []))
    plugins = lyrico_bridge.discover_plugins()
    for item in plugins:
        item["enabled"] = item["key"] in enabled_plugins
        item["icon_url"] = (
            f"/api/plugins/lyrico/icon/{item['key']}" if item.get("has_icon") else "/icon.png"
        )
    return {
        "status": "success",
        "plugins_dir": lyrico_bridge.plugins_dir,
        "count": len(plugins),
        "plugins": plugins,
    }


@app.post("/api/plugins/lyrico/toggle")
async def toggle_lyrico_plugin_api(payload: PluginToggleRequest):
    """开启或禁用指定 Lyrico 插件。"""
    from lyrico_bridge import lyrico_bridge

    plugin_key = security.normalize_plugin_key(payload.key)
    if not plugin_key:
        raise HTTPException(status_code=400, detail="非法的插件标识")
    if plugin_key not in lyrico_bridge.plugins:
        raise HTTPException(status_code=404, detail="插件不存在")

    cfg = load_config()
    enabled_plugins = set(cfg.get("enabled_lyrico_plugins", []))
    if payload.enabled:
        enabled_plugins.add(plugin_key)
    else:
        enabled_plugins.discard(plugin_key)
    cfg["enabled_lyrico_plugins"] = sorted(enabled_plugins)
    try:
        save_config(cfg)
    except OSError as exc:
        logger.error("更新插件开关失败: %s", exc)
        raise HTTPException(status_code=500, detail="配置写入失败")

    lyrico_bridge.plugins[plugin_key]["enabled"] = payload.enabled
    return {
        "status": "ok",
        "key": plugin_key,
        "enabled": payload.enabled,
        "enabled_plugins": sorted(enabled_plugins),
    }


@app.get("/api/plugins/lyrico/icon/{plugin_key}")
async def get_lyrico_plugin_icon(plugin_key: str):
    """获取 Lyrico 插件图标（失败时回退到应用图标）。"""
    from lyrico_bridge import lyrico_bridge

    key = security.normalize_plugin_key(plugin_key)
    p_info = lyrico_bridge.plugins.get(key) if key else None
    if p_info:
        icon_path = os.path.join(p_info["folder_path"], "icon.png")
        if security.is_within(icon_path, lyrico_bridge.plugins_dir) and os.path.isfile(icon_path):
            with open(icon_path, "rb") as fh:
                data = fh.read(512 * 1024)
            return Response(content=data, media_type=security.detect_image_mime(data) or "image/png")

    for candidate in (
        os.path.join(BASE_DIR, "ICON_256.PNG"),
        os.path.join(BASE_DIR, "icon.png"),
        os.path.join(WEB_DIR, "icon.png"),
    ):
        if os.path.isfile(candidate):
            with open(candidate, "rb") as fh:
                return Response(content=fh.read(), media_type="image/png")

    raise HTTPException(status_code=404, detail="无可用图标")


@app.get("/api/fs/browse")
async def browse_fs(path: str = ""):
    """目录选择器：只在授权根目录范围内浏览。"""
    cfg = load_config()
    roots = security.build_roots(cfg.get("music_dir", ""))
    authorized = get_accessible_paths()
    target = (path or "").strip()

    if not target:
        items = []
        seen = set()
        for root in roots:
            key = os.path.normcase(root)
            if key in seen:
                continue
            seen.add(key)
            label = os.path.basename(root.rstrip("/\\")) or root
            items.append(
                {
                    "name": label,
                    "path": root,
                    "is_dir": True,
                    "accessible": os.access(root, os.R_OK),
                    "is_root": True,
                }
            )
        items.sort(key=lambda item: item["name"].lower())
        return {"current": "", "parent": "", "items": items, "authorized": authorized, "roots": roots}

    target = os.path.abspath(target)
    if not os.path.isdir(target):
        raise HTTPException(status_code=404, detail="目录不存在")
    if not security.is_allowed_path(target, roots):
        raise HTTPException(status_code=403, detail="该目录不在授权范围内")

    parent = os.path.dirname(target)
    if not security.is_allowed_path(parent, roots) or os.path.normcase(parent) == os.path.normcase(target):
        parent = ""

    items = []
    permission_error = False
    try:
        with os.scandir(target) as iterator:
            for entry in iterator:
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                accessible = True
                try:
                    with os.scandir(entry.path):
                        pass
                except PermissionError:
                    accessible = False
                except OSError:
                    pass
                items.append(
                    {
                        "name": entry.name,
                        "path": entry.path,
                        "is_dir": True,
                        "accessible": accessible,
                    }
                )
    except PermissionError:
        permission_error = True

    items.sort(key=lambda item: (not item.get("accessible", True), item["name"].lower()))
    return {
        "current": target,
        "parent": parent,
        "items": items,
        "authorized": authorized,
        "permission_error": permission_error,
    }


@app.get("/api/music/scan")
async def scan_music(path: Optional[str] = None, refresh: bool = False, force: bool = False):
    """扫描音乐目录（串行执行，避免并发扫描破坏缓存）。"""
    t_req_start = time.time()
    cfg = load_config()
    raw_path = (path if path is not None else cfg.get("music_dir", "")).strip()
    dir_path = os.path.abspath(raw_path) if raw_path else ""

    if path is not None and dir_path and not security.is_allowed_path(dir_path, _current_roots()):
        raise HTTPException(status_code=403, detail="该目录不在授权范围内")

    if not dir_path or not os.path.isdir(dir_path):
        return {
            "dir_path": dir_path,
            "total": 0,
            "not_found": True,
            "message": f"音乐目录未找到: {dir_path}" if dir_path else "尚未设置音乐目录",
            "stats": {"missing_cover": 0, "missing_lyric": 0, "missing_album": 0, "complete": 0},
            "files": [],
        }

    from_memory = False
    async with state.scan_lock:
        same_dir = bool(state.scanned_dir) and os.path.normcase(state.scanned_dir) == os.path.normcase(dir_path)
        if not refresh and not force and state.scanned_files and same_dir:
            files = state.scanned_files
            from_memory = True
        else:
            try:
                loop = asyncio.get_running_loop()
                files = await loop.run_in_executor(None, tag_engine.scan_directory, dir_path, True, force)
                state.scanned_files = files
                state.scanned_dir = dir_path
            except PermissionError:
                logger.warning("目录访问权限不足: %s", dir_path)
                return {
                    "dir_path": dir_path,
                    "total": 0,
                    "permission_denied": True,
                    "not_found": False,
                    "message": (
                        f"飞牛系统权限受限: 无法读取目录 {dir_path}。请在飞牛【应用中心】➔"
                        "【音乐标签】➔【设置】➔【访问权限】中添加此文件夹（读写权限）。"
                    ),
                    "stats": {"missing_cover": 0, "missing_lyric": 0, "missing_album": 0, "complete": 0},
                    "files": [],
                }

    total = len(files)
    missing_cover = sum(1 for m in files if not m.has_cover)
    missing_lyric = sum(1 for m in files if not m.has_lyric and not m.has_lrc_file)
    missing_album = sum(1 for m in files if not m.album)
    complete_count = sum(
        1 for m in files if m.has_cover and (m.has_lyric or m.has_lrc_file) and m.album and m.artist
    )
    scraped_count = sum(1 for m in files if m.scraped)

    elapsed_ms = round((time.time() - t_req_start) * 1000, 1)

    return {
        "dir_path": dir_path,
        "total": total,
        "from_memory": from_memory,
        "elapsed_ms": elapsed_ms,
        "stats": {
            "missing_cover": missing_cover,
            "missing_lyric": missing_lyric,
            "missing_album": missing_album,
            "complete": complete_count,
            "scraped": scraped_count,
        },
        "files": [m.to_dict() for m in files],
    }


def _extract_cover_bytes(filepath: str) -> Tuple[bytes, str]:
    """提取内嵌封面；MIME 以实际文件头为准（防止伪造 MIME 引发 XSS）。"""
    ext = os.path.splitext(filepath)[1].lower()
    data: Optional[bytes] = None
    declared = ""

    if ext == ".mp3" or ext == ".wav":
        from mutagen.id3 import APIC
        from mutagen.mp3 import MP3
        from mutagen.wave import WAVE

        audio = MP3(filepath) if ext == ".mp3" else WAVE(filepath)
        if audio.tags:
            for frame in audio.tags.values():
                if isinstance(frame, APIC):
                    data, declared = frame.data, frame.mime or ""
                    break
    elif ext == ".flac":
        from mutagen.flac import FLAC

        audio = FLAC(filepath)
        if audio.pictures:
            data, declared = audio.pictures[0].data, audio.pictures[0].mime or ""
    elif ext in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4, MP4Cover

        audio = MP4(filepath)
        covr = audio.tags.get("covr") if audio.tags else None
        if covr:
            cov = covr[0]
            data = bytes(cov)
            declared = (
                "image/png" if getattr(cov, "imageformat", None) == MP4Cover.FORMAT_PNG else "image/jpeg"
            )
    elif ext == ".ogg":
        import base64

        from mutagen.flac import Picture
        from mutagen.oggvorbis import OggVorbis

        audio = OggVorbis(filepath)
        mbp = audio.tags.get("METADATA_BLOCK_PICTURE") if audio.tags else None
        if mbp:
            picture = Picture(base64.b64decode(mbp[0]))
            data, declared = picture.data, picture.mime or ""
        elif audio.tags and audio.tags.get("COVERART"):
            data = base64.b64decode(audio.tags["COVERART"][0])

    if not data:
        raise LookupError("无内嵌封面")
    detected = security.detect_image_mime(data)
    if not detected:
        raise ValueError(f"封面数据不是受支持的图片格式（声明为 {declared or '未知'}）")
    return data, detected


@app.get("/api/music/cover")
async def get_music_cover(filepath: str):
    """从本地音频文件提取内嵌封面图片流。"""
    target = _require_allowed_audio(filepath)
    loop = asyncio.get_running_loop()
    try:
        data, mime = await loop.run_in_executor(None, _extract_cover_bytes, target)
    except LookupError:
        raise HTTPException(status_code=404, detail="无内嵌封面")
    except ValueError as exc:
        logger.warning("封面格式不受支持 (%s): %s", target, exc)
        raise HTTPException(status_code=415, detail="内嵌封面格式不受支持")
    except Exception:
        logger.exception("提取封面失败: %s", target)
        raise HTTPException(status_code=500, detail="提取封面失败")
    return Response(content=data, media_type=mime, headers={"Cache-Control": "private, max-age=60"})


@app.get("/api/music/lyric")
async def get_music_lyric(filepath: str):
    """读取歌词文本（优先同名 .lrc，其次内嵌歌词）。"""
    target = _require_allowed_audio(filepath)
    loop = asyncio.get_running_loop()
    lyric_text = await loop.run_in_executor(None, tag_engine.read_lyric_text, target)
    return {"filepath": target, "lyric": lyric_text}


# -----------------------------------------------------------------------------
# 网页播放（音频流）
#   支持 HTTP Range，浏览器才能拖动进度条 / 分段加载；只允许已授权音乐库内的音频文件。
# -----------------------------------------------------------------------------
STREAM_MIME = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
}
STREAM_CHUNK_SIZE = 256 * 1024


def _parse_range_header(header: str, size: int) -> Optional[Tuple[int, int]]:
    """解析单段 Range 头，返回 (start, end) 闭区间；不合法返回 None。"""
    match = re.match(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", header or "")
    if not match:
        return None
    raw_start, raw_end = match.group(1), match.group(2)
    if not raw_start and not raw_end:
        return None
    if not raw_start:
        # "bytes=-500"：最后 500 字节
        suffix = int(raw_end)
        if suffix <= 0:
            return None
        return max(0, size - suffix), size - 1
    start = int(raw_start)
    end = int(raw_end) if raw_end else size - 1
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


def _open_audio_stream(path: str):
    """打开音频文件供流式读取（在线程池中执行，避免阻塞事件循环）。"""
    return open(path, "rb")


@app.get("/api/music/stream")
async def stream_music(filepath: str, request: Request):
    """以流式方式返回音频内容，支持 Range 断点/拖动播放。"""
    target = _require_allowed_audio(filepath)
    try:
        size = os.path.getsize(target)
    except OSError:
        raise HTTPException(status_code=404, detail="音频文件不存在")
    ext = os.path.splitext(target)[1].lower()
    media_type = STREAM_MIME.get(ext, "application/octet-stream")

    base_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=0, no-cache",
        # 播放接口依赖 Range 请求，禁止中间层缓存整文件
        "X-Content-Type-Options": "nosniff",
    }

    range_header = request.headers.get("range")
    if not range_header:
        loop = asyncio.get_running_loop()
        fh = await loop.run_in_executor(None, _open_audio_stream, target)

        async def _full_iter():
            try:
                while True:
                    chunk = await loop.run_in_executor(None, fh.read, STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
            finally:
                fh.close()

        return StreamingResponse(
            _full_iter(),
            media_type=media_type,
            headers={**base_headers, "Content-Length": str(size)},
        )

    start, end = (0, size - 1)
    parsed = _parse_range_header(range_header, size)
    if parsed is None:
        # 不支持的 Range（如多段）统一按 416 处理，浏览器会自动回退到整段请求
        return Response(
            status_code=416,
            headers={**base_headers, "Content-Range": f"bytes */{size}"},
        )
    start, end = parsed
    length = end - start + 1

    loop = asyncio.get_running_loop()
    fh = await loop.run_in_executor(None, _open_audio_stream, target)
    await loop.run_in_executor(None, fh.seek, start)
    remaining = length

    async def _range_iter():
        nonlocal remaining
        try:
            while remaining > 0:
                chunk = await loop.run_in_executor(None, fh.read, min(STREAM_CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            fh.close()

    return StreamingResponse(
        _range_iter(),
        status_code=206,
        media_type=media_type,
        headers={
            **base_headers,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
        },
    )


# -----------------------------------------------------------------------------
# 歌词手动打标：解析现有 LRC / 保存打标结果
# -----------------------------------------------------------------------------
# LRC 时间标签（三段式必须优先匹配，否则 [mm:ss:xx] 会被当成 [mm:ss.xx]）：
#   三段式 [hh:mm:ss.xx] / [mm:ss:xx]；两段式 [mm:ss] / [mm:ss.xx]
_LRC_TAG_RE = re.compile(r"^\[(\d{1,3}):(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]\s*(.*)$")
_LRC_TAG_2_RE = re.compile(r"^\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]\s*(.*)$")
# 其余 [xx:yy] 形式的内容行（如纯文本歌词里恰好带时间）不算标签
_MAX_LRC_LINE_CHARS = 2000


def _lrc_format_time(seconds: float) -> str:
    """把秒数格式化为 LRC 常用的 [mm:ss.xx] 内的时间部分（分秒分位以下四舍五入）。"""
    seconds = max(0.0, float(seconds))
    whole = int(seconds)
    centis = int(round((seconds - whole) * 100))
    if centis >= 100:
        whole += 1
        centis = 0
    return f"{whole // 60:02d}:{whole % 60:02d}.{centis:02d}"


def _lrc_parse_text(seconds: Any) -> Optional[float]:
    """兼容 "mm:ss.xx" / "mm:ss.xxx" / "hh:mm:ss" / 纯秒数字符串的时间输入。"""
    if seconds is None:
        return None
    if isinstance(seconds, (int, float)):
        return max(0.0, float(seconds))
    text = str(seconds).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    parts = text.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return None
    return None


def build_lrc_text(
    lines: List[Dict[str, Any]],
    meta_lines: Optional[List[str]] = None,
    offset_ms: int = 0,
) -> Tuple[str, List[Tuple[float, str]]]:
    """根据带时间（或待打标）的歌词行生成标准 LRC 文本。

    返回 (lrc_text, 未打标行列表)。
    未打标行不写时间标签，作为纯文本行保留（播放器会忽略，但内容不丢）。
    时间相同的多行合并到同一时间标签，便于对唱/和声。
    """
    buckets: List[Tuple[float, List[str]]] = []
    pending: List[Tuple[float, str]] = []
    untimed: List[str] = []

    for item in lines or []:
        text = "" if item.get("text") is None else str(item.get("text"))
        text = text.replace("\r", " ").replace("\n", " ")[:_MAX_LRC_LINE_CHARS]
        if len(text) > 800:
            raise ValueError("单行歌词过长")
        seconds = _lrc_parse_text(item.get("time"))
        if seconds is None:
            if text.strip():
                untimed.append(text)
            continue
        pending.append((seconds, text))

    pending.sort(key=lambda pair: pair[0])
    for seconds, text in pending:
        if buckets and abs(buckets[-1][0] - seconds) < 0.005:
            buckets[-1][1].append(text)
        else:
            buckets.append((seconds, [text]))

    out: List[str] = []
    has_offset_meta = False
    for meta in meta_lines or []:
        meta = str(meta).strip()
        if meta.startswith("[") and meta.endswith("]"):
            out.append(meta)
            if re.match(r"\[offset:", meta, re.I):
                has_offset_meta = True
    if offset_ms and not has_offset_meta:
        clamped = max(-99_999_999, min(99_999_999, int(offset_ms)))
        out.append(f"[offset:{clamped}]")
    for seconds, texts in buckets:
        for text in texts:
            out.append(f"[{_lrc_format_time(seconds)}]{text}" if text else f"[{_lrc_format_time(seconds)}]")
    out.extend(untimed)
    if not out:
        return "", untimed
    return "\n".join(out) + "\n", untimed


def parse_lrc_text(text: str) -> Tuple[str, List[Dict[str, Any]], List[str], int]:
    """解析 LRC 文本。

    返回 (format, lines, meta_lines, offset_ms)：
      - format: lrc（含时间标签）| plain（纯文本，需要打标）
      - lines: [{"time": 秒数或 None, "text": 歌词, "tags": [原始时间前缀]}]
      - meta_lines: [ti] [ar] 等元信息行（原样保留写回）
    """
    raw_lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    parsed: List[Dict[str, Any]] = []
    meta_lines: List[str] = []
    offset_ms = 0
    has_timing = False

    for raw in raw_lines[:20_000]:
        line = raw.strip()
        if not line:
            parsed.append({"time": None, "text": "", "tags": []})
            continue

        # 一行可能带多个时间标签：[00:01.00][00:05.00]歌词
        tags: List[float] = []
        rest = line
        while True:
            match3 = _LRC_TAG_RE.match(rest)
            match2 = None if match3 else _LRC_TAG_2_RE.match(rest)
            if not match3 and not match2:
                break
            try:
                if match3:
                    a, b, c, frac3, remainder = match3.groups()
                    if frac3 is not None:
                        # [hh:mm:ss.xx]：第三段带小数，按「时:分:秒」解释
                        seconds = int(a) * 3600 + int(b) * 60 + int(c) + int(frac3) / (
                            1000.0 if len(frac3) == 3 else 100.0
                        )
                    else:
                        # [mm:ss:xx]：第三段是百分秒（旧版 LRC 常见写法）
                        seconds = int(a) * 60 + int(b) + int(c) / 100.0
                else:
                    a, b, frac2, remainder = match2.groups()
                    seconds = int(a) * 60 + int(b)
                    if frac2:
                        seconds += int(frac2) / (1000.0 if len(frac2) == 3 else 100.0)
            except ValueError:
                break
            tags.append(seconds)
            rest = remainder
            if len(tags) > 20:
                break

        if tags:
            has_timing = True
            text_part = rest.strip()
            for idx, seconds in enumerate(tags):
                parsed.append({
                    "time": round(seconds, 2),
                    "text": text_part if idx == len(tags) - 1 else text_part,
                    "tags": [_lrc_format_time(seconds)],
                })
        elif re.fullmatch(r"\[[a-zA-Z#]+:[^\]]*\]", line):
            # 不含时间标签的 [ti:] [ar:] [offset:] 等元信息行，原样保留
            meta_lines.append(line)
            offset_match = re.fullmatch(r"\[offset:\s*(-?\d+)\s*\]", line, re.I)
            if offset_match:
                offset_ms = int(offset_match.group(1))
        else:
            parsed.append({"time": None, "text": line, "tags": []})

    # 去掉首尾空行，压缩连续空行，避免编辑器看到大量空行
    while parsed and not parsed[0]["text"]:
        parsed.pop(0)
    while parsed and not parsed[-1]["text"]:
        parsed.pop()
    compact: List[Dict[str, Any]] = []
    for item in parsed:
        if not item["text"] and compact and not compact[-1]["text"]:
            continue
        compact.append(item)

    return ("lrc" if has_timing else "plain"), compact, meta_lines, offset_ms


def _lyric_sidecar_path(filepath: str) -> str:
    return os.path.splitext(filepath)[0] + ".lrc"


def _read_sidecar_lrc(filepath: str) -> Tuple[bool, str]:
    """读取同名 .lrc（多编码兼容），返回 (是否存在, 内容)。"""
    lrc_path = _lyric_sidecar_path(filepath)
    if not os.path.isfile(lrc_path):
        return False, ""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            with open(lrc_path, "r", encoding=enc) as fh:
                return True, fh.read()
        except (OSError, UnicodeError):
            continue
    return True, ""


def _resolve_lyric_source(filepath: str) -> str:
    """歌词来源：sidecar（外置 .lrc）| embedded（内嵌）| none。"""
    has_sidecar, sidecar_text = _read_sidecar_lrc(filepath)
    if has_sidecar and sidecar_text.strip():
        return "sidecar"
    if tag_engine.read_lyric_text(filepath).strip():
        return "embedded"
    return "none"


class _LyricLine(BaseModel):
    # 多余字段一律忽略：前端会把读取到的行（含 tags 等展示用字段）原样回传
    model_config = ConfigDict(extra="ignore")

    time: Optional[float] = Field(default=None, ge=0, le=24 * 3600)
    text: str = Field(default="", max_length=800)


class LyricSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filepath: str = Field(min_length=1, max_length=4096)
    lines: List[_LyricLine] = Field(default_factory=list, max_length=2000)
    meta_lines: List[str] = Field(default_factory=list, max_length=20)
    offset_ms: int = Field(0, ge=-99_999, le=99_999)
    # 外置 .lrc 与内嵌歌词的写入开关
    write_sidecar: bool = True
    write_embedded: bool = True
    convert_to_simplified: bool = False
    # 用户在页面里手工编辑过歌词文本时，未打标行也允许原样保留
    keep_untimed: bool = True


@app.get("/api/music/lyric/lyrics")
async def get_lyric_for_edit(filepath: str):
    """读取歌词用于手动打标：返回结构化行、来源与文件信息。"""
    target = _require_allowed_audio(filepath)
    loop = asyncio.get_running_loop()

    def _load() -> Dict[str, Any]:
        has_sidecar, sidecar_text = _read_sidecar_lrc(target)
        source = "none"
        text = ""
        if has_sidecar and sidecar_text.strip():
            source, text = "sidecar", sidecar_text
        else:
            embedded = tag_engine.read_lyric_text(target)
            if embedded.strip():
                source, text = "embedded", embedded
        fmt, lines, meta_lines, offset_ms = parse_lrc_text(text)
        meta = tag_engine.read_metadata(target)
        return {
            "filepath": target,
            "lrc_path": _lyric_sidecar_path(target),
            "has_sidecar": has_sidecar,
            "source": source,
            "format": fmt,
            "lines": lines,
            "meta_lines": meta_lines,
            "offset_ms": offset_ms,
            "file_meta": meta.to_dict() if meta else None,
        }

    return await loop.run_in_executor(None, _load)


@app.post("/api/music/lyric/save")
async def save_lyric(req: LyricSaveRequest):
    """保存手动打标结果：重新生成 LRC，并写入外置 .lrc / 内嵌歌词。"""
    target = _require_allowed_audio(req.filepath)

    cleaned_lines: List[Dict[str, Any]] = []
    for line in req.lines:
        text = line.text.replace("\r", " ").replace("\n", " ").strip()
        cleaned_lines.append({"time": line.time, "text": text})
        if len(text) > 800:
            raise HTTPException(status_code=400, detail="单行歌词超过 800 字")

    if not any(line["time"] is not None for line in cleaned_lines):
        raise HTTPException(status_code=400, detail="还没有打标的歌词行，请先为至少一行打上时间")

    if req.convert_to_simplified:
        for line in cleaned_lines:
            line["text"] = to_simplified_chinese(line["text"])

    def _render() -> Tuple[str, List[str]]:
        lrc_text, untimed = build_lrc_text(cleaned_lines, req.meta_lines, req.offset_ms)
        if not req.keep_untimed:
            # 只保留已打标行
            timed = [line for line in cleaned_lines if line["time"] is not None]
            lrc_text, _ = build_lrc_text(timed, req.meta_lines, req.offset_ms)
            untimed = []
        return lrc_text, untimed

    try:
        lrc_text, untimed = await asyncio.get_running_loop().run_in_executor(None, _render)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not lrc_text.strip():
        raise HTTPException(status_code=400, detail="歌词内容为空")

    loop = asyncio.get_running_loop()

    def _write_all() -> Dict[str, Any]:
        result: Dict[str, Any] = {"sidecar": False, "embedded": False, "lrc_path": _lyric_sidecar_path(target)}

        if req.write_sidecar:
            lrc_path = _lyric_sidecar_path(target)
            tmp_path = lrc_path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as fh:
                    fh.write(lrc_text if lrc_text.endswith("\n") else lrc_text + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, lrc_path)
                result["sidecar"] = True
            except OSError as exc:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                logger.warning("保存外置歌词失败 (%s): %s", lrc_path, exc)

        if req.write_embedded:
            from scraper_engine import MusicCandidate

            meta = tag_engine.read_metadata(target)
            info = meta.to_dict() if meta else {}
            cand = MusicCandidate(
                source="manual",
                source_id="",
                title=str(info.get("title") or ""),
                artist=str(info.get("artist") or ""),
                album=str(info.get("album") or ""),
                album_artist=str(info.get("album_artist") or ""),
                track_number=info.get("track_number"),
                total_tracks=info.get("total_tracks"),
                year=str(info.get("year") or ""),
                genre=str(info.get("genre") or ""),
                lyric=lrc_text,
                extra={"preserve_cover": True, "preserve_lyric": False},
            )
            # 只写歌词：不嵌入封面、不重写外置文件（外置已单独处理）、不改其它字段
            # 注意 write_tags 会把 candidate 的其它字段一并写入，这里已用当前元数据回填保持一致
            try:
                ok = tag_engine.write_tags(
                    target, cand, embed_cover=False, save_lrc=False, cover_bytes=None
                )
                result["embedded"] = bool(ok)
            except Exception as exc:  # pragma: no cover - 取决于文件格式
                logger.exception("写入内嵌歌词失败 (%s): %s", target, exc)
                result["embedded"] = False
        return result

    result = await loop.run_in_executor(None, _write_all)
    if not result["sidecar"] and not result["embedded"]:
        raise HTTPException(status_code=500, detail="歌词写入失败，请检查文件权限或查看运行日志")

    logger.info(
        "📜 歌词已保存 | %s | 外置=%s 内嵌=%s | 已打标 %s 行，未打标 %s 行",
        os.path.basename(target),
        result["sidecar"],
        result["embedded"],
        sum(1 for line in cleaned_lines if line["time"] is not None),
        len(untimed),
    )

    # 同步刷新缓存中的歌词状态（表格徽章）
    def _refresh_cache() -> None:
        updated = tag_engine.read_metadata(target)
        if not updated:
            return
        for idx, item in enumerate(state.scanned_files):
            if item.filepath == target:
                state.scanned_files[idx] = updated
                break

    try:
        await loop.run_in_executor(None, _refresh_cache)
    except Exception:
        logger.warning("刷新歌词状态缓存失败: %s", target)

    return {
        "status": "success",
        "filepath": target,
        "lrc_path": result["lrc_path"],
        "written_sidecar": result["sidecar"],
        "written_embedded": result["embedded"],
        "timed_lines": sum(1 for line in cleaned_lines if line["time"] is not None),
        "untimed_lines": len(untimed),
        "lrc_preview": lrc_text[:4000],
    }



def _resolve_sources(requested: Optional[List[str]] = None) -> List[str]:
    """把请求/配置中的数据源解析为合法列表（内置源 + 已启用的 Lyrico 插件）。"""
    cfg = load_config()
    raw = list(requested) if requested else list(cfg.get("sources") or [])
    builtin = set(KNOWN_SOURCES)
    resolved: List[str] = []
    for item in raw:
        item = (item or "").strip()
        if item in builtin and item not in resolved:
            resolved.append(item)
        elif item.startswith("lyrico:"):
            key = security.normalize_plugin_key(item.split(":", 1)[1])
            if key:
                tag = f"lyrico:{key}"
                if tag not in resolved:
                    resolved.append(tag)

    if requested is None and cfg.get("plugin_execution_enabled", True):
        try:
            from lyrico_bridge import lyrico_bridge

            for key in sorted(cfg.get("enabled_lyrico_plugins", [])):
                if key in lyrico_bridge.plugins:
                    tag = f"lyrico:{key}"
                    if tag not in resolved:
                        resolved.append(tag)
        except Exception:
            pass
    return resolved


@app.post("/api/music/match")
async def match_music(req: MatchRequest):
    """单曲在线多源匹配"""
    target = _require_allowed_audio(req.filepath)
    loop = asyncio.get_running_loop()
    meta = await loop.run_in_executor(None, tag_engine.read_metadata, target)
    if not meta:
        raise HTTPException(status_code=400, detail="解析音频文件失败")

    s_title = req.title if req.title is not None else meta.suggested_title
    s_artist = req.artist if req.artist is not None else meta.suggested_artist
    s_query = req.query if req.query is not None else meta.suggested_query

    candidates = await scraper_engine.search(
        title=s_title,
        artist=s_artist,
        query=s_query,
        sources=_resolve_sources(req.sources),
        limit_per_source=5,
    )

    return {
        "file_meta": meta.to_dict(),
        "search_params": {"title": s_title, "artist": s_artist, "query": s_query},
        "candidates": [c.to_dict() for c in candidates],
    }


def _clean_text(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _parse_int(value: Any, lower: int = 0, upper: int = 999) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(lower, min(upper, number))


@app.post("/api/music/write_tag")
async def write_music_tag(req: WriteTagRequest):
    """将选定的候选或自定义元数据写回音频文件。"""
    target = _require_allowed_audio(req.filepath)
    if not isinstance(req.candidate, dict):
        raise HTTPException(status_code=400, detail="candidate 必须是对象")

    cand_dict = req.candidate
    extra_dict = cand_dict.get("extra") if isinstance(cand_dict.get("extra"), dict) else {}
    if "clear_cover" in cand_dict:
        extra_dict["clear_cover"] = bool(cand_dict["clear_cover"])
    if cand_dict.get("source") and cand_dict.get("source") not in ("manual", ""):
        extra_dict["scraped"] = True
    elif "scraped" in cand_dict:
        extra_dict["scraped"] = bool(cand_dict["scraped"])

    cover_url = _clean_text(cand_dict.get("cover_url"), 4096)
    cover_bytes: Optional[bytes] = None
    if cover_url:
        if cover_url.startswith("data:"):
            try:
                cover_bytes = security.decode_data_uri(cover_url)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"封面数据非法: {exc}")
            cover_url = ""
        else:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, security.validate_remote_url, cover_url)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"封面地址不合法: {exc}")

    cand = MusicCandidate(
        source=_clean_text(cand_dict.get("source") or "manual", 32),
        source_id=_clean_text(cand_dict.get("source_id"), 128),
        title=_clean_text(cand_dict.get("title")),
        artist=_clean_text(cand_dict.get("artist")),
        album=_clean_text(cand_dict.get("album")),
        album_artist=_clean_text(cand_dict.get("album_artist")),
        track_number=_parse_int(cand_dict.get("track_number")),
        total_tracks=_parse_int(cand_dict.get("total_tracks")),
        year=_clean_text(cand_dict.get("year"), 10),
        genre=_clean_text(cand_dict.get("genre"), 100),
        cover_url=cover_url,
        lyric=str(cand_dict.get("lyric") or "")[:200_000],
        extra=extra_dict,
    )

    # 用户显式清空歌词时，同步删除外置 .lrc
    if req.save_lrc and not cand.lyric:
        extra_dict["clear_lyric"] = True

    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(
        None,
        tag_engine.write_tags,
        target,
        cand,
        req.embed_cover,
        req.save_lrc,
        cover_bytes,
        req.convert_to_simplified,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="写入标签失败，请查看日志")

    current_filepath = target
    rename_info = {"renamed": False, "message": ""}
    if req.rename_file and req.new_filename:
        new_name = req.new_filename
        if req.convert_to_simplified:
            new_name = to_simplified_chinese(new_name)
        ren_ok, new_fp, ren_msg = await loop.run_in_executor(
            None, tag_engine.rename_file, current_filepath, new_name, True
        )
        if ren_ok:
            current_filepath = new_fp
            rename_info = {"renamed": True, "message": ren_msg, "new_filepath": new_fp}
        else:
            rename_info["message"] = ren_msg
            logger.warning("同步重命名未执行成功: %s", ren_msg)

    updated_meta = await loop.run_in_executor(None, tag_engine.read_metadata, current_filepath)
    if updated_meta:
        if not updated_meta.album:
            updated_meta.album_matched = False
        found = False
        for idx, item in enumerate(state.scanned_files):
            if item.filepath in (req.filepath, target, current_filepath):
                state.scanned_files[idx] = updated_meta
                found = True
                break
        if not found:
            state.scanned_files.append(updated_meta)

    return {
        "status": "success",
        "renamed": rename_info["renamed"],
        "new_filepath": current_filepath,
        "rename_message": rename_info.get("message", ""),
        "updated_meta": updated_meta.to_dict() if updated_meta else None,
    }


@app.post("/api/music/convert_zh")
async def convert_zh_endpoint(req: ConvertZhRequest):
    """繁体中文转换为简体中文 API（限制单次请求规模）。"""
    res = {}
    if req.text is not None:
        if len(req.text) > 200_000:
            raise HTTPException(status_code=413, detail="text 内容过长")
        res["converted"] = to_simplified_chinese(req.text)
    if req.texts is not None:
        if len(req.texts) > 1000 or any(len(t or "") > 10_000 for t in req.texts):
            raise HTTPException(status_code=413, detail="texts 数量或长度超限")
        res["converted_list"] = [to_simplified_chinese(t) for t in req.texts]
    if req.fields is not None:
        if len(req.fields) > 200:
            raise HTTPException(status_code=413, detail="fields 数量超限")
        res["converted_fields"] = {k: to_simplified_chinese(v) for k, v in req.fields.items()}
    return res


@app.post("/api/music/rename")
async def rename_music_file(req: RenameRequest):
    """重命名音频物理文件及同名 .lrc 歌词文件。"""
    target = _require_allowed_audio(req.filepath)
    loop = asyncio.get_running_loop()
    ren_ok, new_filepath, msg = await loop.run_in_executor(
        None, tag_engine.rename_file, target, req.new_filename, req.sync_lrc
    )
    if not ren_ok:
        raise HTTPException(status_code=400, detail=msg)

    updated_meta = await loop.run_in_executor(None, tag_engine.read_metadata, new_filepath)
    if updated_meta:
        for idx, item in enumerate(state.scanned_files):
            if item.filepath == target:
                state.scanned_files[idx] = updated_meta
                break

    return {
        "status": "success",
        "old_filepath": target,
        "new_filepath": new_filepath,
        "message": msg,
        "updated_meta": updated_meta.to_dict() if updated_meta else None,
    }


# -----------------------------------------------------------------------------
# 自动整理与分类归类 REST API 端点 (Auto-Organize & Folder Structuring)
# -----------------------------------------------------------------------------
@app.post("/api/music/organize/preview")
async def preview_organize_music(req: OrganizePreviewRequest):
    """预演分析音乐自动整理归类路径变更。"""
    cfg = load_config()
    music_dir = cfg.get("music_dir", "")
    if not music_dir or not os.path.isdir(music_dir):
        raise HTTPException(status_code=400, detail="音乐库根目录未配置或不存在")
    if not security.is_allowed_path(music_dir, _current_roots()):
        raise HTTPException(status_code=403, detail="音乐库根目录不在授权范围内")

    files = list(state.scanned_files)
    if req.filepaths:
        target_set = set(req.filepaths)
        files = [item for item in files if item.filepath in target_set]

    total = len(files)
    will_move = 0
    no_change = 0
    conflicts = 0
    items: List[Dict[str, Any]] = []

    for meta in files:
        if not os.path.isfile(meta.filepath):
            continue

        rel_dir, target_fn = tag_engine.format_organized_path(
            meta, pattern=req.pattern, convert_to_simplified=req.convert_to_simplified
        )
        target_dir = os.path.join(music_dir, rel_dir) if rel_dir else music_dir
        target_fp = os.path.join(target_dir, target_fn)

        current_rel = (
            os.path.relpath(meta.filepath, music_dir).replace("\\", "/")
            if security.is_within(meta.filepath, music_dir)
            else os.path.basename(meta.filepath)
        )
        target_rel = os.path.join(rel_dir, target_fn).replace("\\", "/")

        is_same = os.path.normcase(os.path.abspath(meta.filepath)) == os.path.normcase(
            os.path.abspath(target_fp)
        )
        is_conflict = (not is_same) and os.path.exists(target_fp)

        if is_same:
            no_change += 1
            status_desc = "无需移动"
        elif is_conflict:
            conflicts += 1
            will_move += 1
            status_desc = f"目标已存在（{req.conflict_mode}）"
        else:
            will_move += 1
            status_desc = "待归纳迁移"

        items.append(
            {
                "filepath": meta.filepath,
                "filename": meta.filename,
                "title": meta.title or meta.filename,
                "artist": meta.artist or "未知歌手",
                "album": meta.album or "未分类专辑",
                "genre": meta.genre or "流行",
                "language": getattr(meta, "language", "") or "华语",
                "current_rel": current_rel,
                "target_rel": target_rel,
                "target_dir": target_dir,
                "target_filename": target_fn,
                "target_filepath": target_fp,
                "is_same": is_same,
                "is_conflict": is_conflict,
                "status_desc": status_desc,
            }
        )

    return {
        "status": "success",
        "total": total,
        "will_move": will_move,
        "no_change": no_change,
        "conflicts": conflicts,
        "items": items[:300],
        "items_truncated": len(items) > 300,
        "items_total": len(items),
    }


def _organize_worker(
    tasks: List[Tuple[str, str, str]],
    music_dir: str,
    sync_lrc: bool,
    clean_empty_dirs: bool,
    conflict_mode: str,
) -> Dict[str, Any]:
    """在线程池中执行文件搬移，避免阻塞事件循环。"""
    moved_count = 0
    skipped_count = 0
    failed_count = 0
    results: List[Dict[str, Any]] = []
    updated: List[Tuple[str, Any]] = []

    for filepath, target_dir, target_fn in tasks:
        if not os.path.isfile(filepath):
            failed_count += 1
            results.append({"filepath": filepath, "status": "not_found", "message": "源文件不存在"})
            continue

        ok, new_fp, msg = tag_engine.organize_file(
            filepath=filepath,
            target_dir=target_dir,
            new_filename=target_fn,
            sync_lrc=sync_lrc,
            clean_empty_dirs=clean_empty_dirs,
            music_base_dir=music_dir,
            conflict_mode=conflict_mode,
        )
        if ok:
            if os.path.normcase(os.path.abspath(filepath)) != os.path.normcase(os.path.abspath(new_fp)):
                moved_count += 1
                new_meta = tag_engine.read_metadata(new_fp)
                if new_meta:
                    updated.append((filepath, new_meta))
            else:
                skipped_count += 1
            results.append(
                {"filepath": filepath, "new_filepath": new_fp, "status": "success", "message": msg}
            )
        elif "已跳过" in msg:
            skipped_count += 1
            results.append({"filepath": filepath, "status": "skipped", "message": msg})
        else:
            failed_count += 1
            results.append({"filepath": filepath, "status": "failed", "message": msg})

    # 批量任务结束统一落盘（配合 update_cache_item 的防抖，避免逐曲全量写盘）
    tag_engine.flush_cache()
    return {
        "moved_count": moved_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "results": results,
        "updated": updated,
    }


@app.post("/api/music/organize/execute")
async def execute_organize_music(req: OrganizeExecuteRequest):
    """执行批量音乐自动整理归档迁移（文件操作在线程池执行）。"""
    cfg = load_config()
    music_dir = cfg.get("music_dir", "")
    if not music_dir or not os.path.isdir(music_dir):
        raise HTTPException(status_code=400, detail="音乐库根目录未配置或不存在")
    if not security.is_allowed_path(music_dir, _current_roots()):
        raise HTTPException(status_code=403, detail="音乐库根目录不在授权范围内")

    files = list(state.scanned_files)
    if req.filepaths:
        target_set = set(req.filepaths)
        files = [item for item in files if item.filepath in target_set]

    tasks: List[Tuple[str, str, str]] = []
    for meta in files:
        if not os.path.isfile(meta.filepath):
            continue
        rel_dir, target_fn = tag_engine.format_organized_path(
            meta, pattern=req.pattern, convert_to_simplified=req.convert_to_simplified
        )
        target_dir = os.path.join(music_dir, rel_dir) if rel_dir else music_dir
        tasks.append((meta.filepath, target_dir, target_fn))

    loop = asyncio.get_running_loop()
    outcome = await loop.run_in_executor(
        None,
        _organize_worker,
        tasks,
        music_dir,
        req.sync_lrc,
        req.clean_empty_dirs,
        req.conflict_mode,
    )

    for old_path, new_meta in outcome["updated"]:
        for idx, item in enumerate(state.scanned_files):
            if item.filepath == old_path:
                state.scanned_files[idx] = new_meta
                break

    results = outcome["results"]
    return {
        "status": "completed",
        "total": len(results),
        "moved_count": outcome["moved_count"],
        "skipped_count": outcome["skipped_count"],
        "failed_count": outcome["failed_count"],
        "results": results[:200],
        "results_truncated": len(results) > 200,
        "results_total": len(results),
    }


# -----------------------------------------------------------------------------
# 整张专辑维度 REST API 端点 (Album Search, Detail & Batch Apply)
# -----------------------------------------------------------------------------
@app.get("/api/music/album/search")
async def search_albums_endpoint(query: str, sources: Optional[str] = None):
    """搜索整张官方专辑列表。"""
    q = (query or "").strip()
    if not q or len(q) > 200:
        raise HTTPException(status_code=400, detail="搜索词不能为空且不能超过 200 字符")
    source_list: Optional[List[str]] = None
    if sources:
        allowed = {"netease", "qqmusic", "itunes"}
        source_list = [s.strip() for s in sources.split(",") if s.strip() in allowed]
        if not source_list:
            raise HTTPException(status_code=400, detail="没有可用的专辑数据源")
    albums = await scraper_engine.search_albums(q, sources=source_list)
    return {"albums": albums}


@app.get("/api/music/album/detail")
async def get_album_detail_endpoint(source: str, album_id: str):
    """获取整张专辑的官方详情及所有曲目列表 (Tracklist)。"""
    src = (source or "").strip().lower()
    if src not in ("netease", "qqmusic", "itunes"):
        raise HTTPException(status_code=400, detail="不支持的专辑数据源")
    album_key = (album_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", album_key):
        raise HTTPException(status_code=400, detail="专辑 ID 格式非法")
    detail = await scraper_engine.get_album_details(src, album_key)
    if not detail:
        raise HTTPException(status_code=404, detail="未找到指定专辑详情")
    return {"detail": detail}


@app.post("/api/music/album/apply")
async def apply_album_metadata(req: AlbumApplyRequest):
    """一键将整张官方专辑元数据对齐写入本地对应曲目 (支持同步规范重命名)"""
    if not req.items:
        raise HTTPException(status_code=400, detail="待写入曲目列表为空")

    success_count = 0
    failed_count = 0
    renamed_count = 0
    results = []

    loop = asyncio.get_running_loop()

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        # 预先下载专辑公共封面（单次下载，复用至整张专辑）
        album_cover_bytes: Optional[bytes] = None
        if req.embed_cover and req.cover_url:
            try:
                if req.cover_url.startswith("data:"):
                    album_cover_bytes = security.decode_data_uri(req.cover_url)
                else:
                    await loop.run_in_executor(None, security.validate_remote_url, req.cover_url)
                    resp = await client.get(req.cover_url)
                    if resp.status_code == 200 and 100 < len(resp.content) <= 15 * 1024 * 1024:
                        if security.detect_image_mime(resp.content):
                            album_cover_bytes = resp.content
                        else:
                            logger.warning("[AlbumApply] 专辑封面不是受支持的图片格式，已忽略")
                    else:
                        logger.warning("[AlbumApply] 封面下载失败或超出大小限制: %s", resp.status_code)
            except ValueError as exc:
                logger.warning("[AlbumApply] 封面地址非法，已跳过: %s", exc)
            except Exception as exc:
                logger.warning("[AlbumApply] 预先下载专辑封面异常: %s", exc)

        for item in req.items:
            try:
                target = _require_allowed_audio(item.filepath)
            except HTTPException as exc:
                failed_count += 1
                results.append(
                    {"filepath": item.filepath, "status": "invalid", "message": exc.detail}
                )
                continue

            orig_meta = next((m for m in state.scanned_files if m.filepath == target), None)
            if not orig_meta:
                orig_meta = await loop.run_in_executor(None, tag_engine.read_metadata, target)

            # 检查是否跳过
            if not req.overwrite_existing and orig_meta:
                if orig_meta.has_cover and (orig_meta.has_lyric or orig_meta.has_lrc_file) and orig_meta.track_number:
                    results.append({"filepath": item.filepath, "status": "skipped"})
                    continue

            # 抓取官方同步歌词 (若有 source & source_id)
            lyric = ""
            tlyric = ""
            if req.save_lrc and item.source and item.source_id:
                try:
                    if item.source == "netease":
                        lyric, tlyric = await scraper_engine.fetch_netease_lyric(client, item.source_id)
                    elif item.source == "qqmusic":
                        lyric = await scraper_engine.fetch_qq_lyric(client, item.source_id)
                except Exception as e:
                    logger.warning(f"[AlbumApply] 歌词获取异常 ({item.filepath}): {e}")

            cand = MusicCandidate(
                source=item.source or "album_batch",
                source_id=item.source_id or "",
                title=item.title or (orig_meta.title if orig_meta else "") or os.path.basename(item.filepath),
                artist=item.artist or req.album_artist or (orig_meta.artist if orig_meta else "") or "",
                album=req.album_name,
                album_artist=req.album_artist or "",
                track_number=item.track_number,
                total_tracks=item.total_tracks or len(req.items),
                year=req.year or "",
                date=req.date or "",
                genre=req.genre or "",
                cover_url=req.cover_url or "",
                lyric=lyric,
                tlyric=tlyric,
                extra={"album_matched": True, "album_source": f"{item.source or 'album'}:{item.source_id or ''}"}
            )

            ok = await loop.run_in_executor(
                None,
                tag_engine.write_tags,
                target,
                cand,
                req.embed_cover,
                req.save_lrc,
                album_cover_bytes,
                req.convert_to_simplified,
            )

            if ok:
                success_count += 1
                current_fp = target
                renamed = False
                ren_msg = ""

                # 若勾选了同步重命名文件
                if req.rename_files:
                    target_filename = item.new_filename
                    if not target_filename:
                        _, old_ext = os.path.splitext(target)
                        if not old_ext:
                            old_ext = ".mp3"

                        track_no = cand.track_number or 1
                        total_t = cand.total_tracks or len(req.items)
                        pad_len = 2 if total_t < 100 else 3
                        track_str = str(track_no).zfill(pad_len)

                        s_title = cand.title or "Unknown"
                        s_artist = cand.artist or req.album_artist or "Unknown"
                        if req.convert_to_simplified:
                            s_title = to_simplified_chinese(s_title)
                            s_artist = to_simplified_chinese(s_artist)

                        pat = req.rename_pattern or "track-title"
                        if pat == "track-title":
                            target_filename = f"{track_str}. {s_title}{old_ext}"
                        elif pat == "track-artist-title":
                            target_filename = f"{track_str}. {s_artist} - {s_title}{old_ext}"
                        elif pat == "artist-title":
                            target_filename = f"{s_artist} - {s_title}{old_ext}"
                        elif pat == "title-artist":
                            target_filename = f"{s_title} - {s_artist}{old_ext}"
                        elif pat == "title":
                            target_filename = f"{s_title}{old_ext}"
                        else:
                            target_filename = f"{track_str}. {s_title}{old_ext}"

                    if req.convert_to_simplified:
                        target_filename = to_simplified_chinese(target_filename)
                    target_filename = sanitize_filename(target_filename)

                    ren_ok, new_fp, ren_msg = await loop.run_in_executor(
                        None, tag_engine.rename_file, current_fp, target_filename, True
                    )
                    if ren_ok:
                        current_fp = new_fp
                        renamed = True
                        renamed_count += 1
                    else:
                        logger.warning("[AlbumApply] 重命名未完成 (%s): %s", current_fp, ren_msg)

                results.append({
                    "filepath": item.filepath,
                    "new_filepath": current_fp,
                    "status": "success",
                    "title": cand.title,
                    "track": cand.track_number,
                    "renamed": renamed,
                    "rename_message": ren_msg
                })

                new_meta = await loop.run_in_executor(None, tag_engine.read_metadata, current_fp)
                if new_meta:
                    new_meta.album_matched = True
                    new_meta.scraped = True
                    found = False
                    for idx, m in enumerate(state.scanned_files):
                        if m.filepath in (item.filepath, target, current_fp):
                            state.scanned_files[idx] = new_meta
                            found = True
                            break
                    if not found:
                        state.scanned_files.append(new_meta)
            else:
                failed_count += 1
                results.append({"filepath": item.filepath, "status": "failed"})

    # 批量任务结束统一落盘（配合 update_cache_item 的防抖，避免逐曲全量写盘）
    tag_engine.flush_cache()
    return {
        "status": "completed",
        "total": len(req.items),
        "success": success_count,
        "failed": failed_count,
        "renamed_count": renamed_count,
        "results": results
    }


# -----------------------------------------------------------------------------
# 异步批量刮削流水线任务
# -----------------------------------------------------------------------------
def _merge_fill_missing(best: MusicCandidate, meta: AudioMetadata) -> MusicCandidate:
    """把“仅补全缺失字段”落实为字段级合并：已有值优先，封面/歌词保持原样。"""
    from dataclasses import replace

    merged = replace(best)
    merged.extra = dict(best.extra or {})
    if meta.title:
        merged.title = meta.title
    if meta.artist:
        merged.artist = meta.artist
    if meta.album:
        merged.album = meta.album
    if meta.album_artist:
        merged.album_artist = meta.album_artist
    if meta.year:
        merged.year = meta.year
    if meta.genre:
        merged.genre = meta.genre
    if meta.track_number:
        merged.track_number = meta.track_number
    if meta.total_tracks:
        merged.total_tracks = meta.total_tracks
    if meta.has_cover:
        merged.extra["preserve_cover"] = True
    if meta.has_lyric or meta.has_lrc_file:
        merged.extra["preserve_lyric"] = True
        merged.lyric = ""
    return merged


async def run_batch_scrape_task(req: BatchScrapeRequest):
    state.batch_success = 0
    state.batch_skipped = 0
    state.batch_failed = 0
    state.batch_current = 0
    state.batch_current_file = ""

    files = list(state.scanned_files)
    if req.filepaths:
        target_set = set(req.filepaths)
        files = [item for item in files if item.filepath in target_set]

    state.batch_total = len(files)

    def add_log(message: str) -> None:
        logger.info("[Batch] %s", message)
        state.batch_logs.append(message)
        if len(state.batch_logs) > 300:
            del state.batch_logs[: len(state.batch_logs) - 300]

    mode = req.overwrite_mode
    mode_name = {
        "overwrite_all": "强制全量覆盖",
        "fill_missing": "仅补全缺失字段",
        "skip_complete": "跳过已完整曲目",
    }.get(mode, mode)
    add_log(
        f"🚀 开始批量刮削: 待处理 {state.batch_total} 首, 策略: 【{mode_name}】, "
        f"阈值: {req.min_score}分 (预演: {req.dry_run})"
    )

    cfg = load_config()
    sources = _resolve_sources(None)
    loop = asyncio.get_running_loop()

    try:
        for idx, meta in enumerate(files, 1):
            if state.batch_stop_requested:
                add_log("⏹️ 用户终止了批量刮削任务")
                break

            state.batch_current = idx
            state.batch_current_file = meta.filename

            if mode == "skip_complete":
                if meta.has_cover and (meta.has_lyric or meta.has_lrc_file) and meta.album and meta.artist:
                    state.batch_skipped += 1
                    add_log(f"[{idx}/{state.batch_total}] ⏭️ 跳过（标签已完整）: {meta.filename}")
                    continue
            elif mode == "fill_missing":
                if (
                    meta.has_cover
                    and (meta.has_lyric or meta.has_lrc_file)
                    and meta.title
                    and meta.artist
                    and meta.album
                    and meta.year
                ):
                    state.batch_skipped += 1
                    add_log(f"[{idx}/{state.batch_total}] ⏭️ 跳过（字段已齐全）: {meta.filename}")
                    continue

            try:
                candidates = await scraper_engine.search(
                    title=meta.suggested_title,
                    artist=meta.suggested_artist,
                    query=meta.suggested_query,
                    sources=sources,
                    limit_per_source=3,
                )
                if not candidates or candidates[0].score < req.min_score:
                    state.batch_failed += 1
                    best_score = candidates[0].score if candidates else 0.0
                    add_log(
                        f"[{idx}/{state.batch_total}] ⚠️ 未达阈值 "
                        f"({best_score:.1f} < {req.min_score}): {meta.filename}"
                    )
                    continue

                best = candidates[0]
                if mode == "fill_missing":
                    best = _merge_fill_missing(best, meta)

                if req.dry_run:
                    state.batch_success += 1
                    add_log(
                        f"[{idx}/{state.batch_total}] 🔍 预演匹配: {meta.filename} -> "
                        f"【{best.source.upper()}】《{best.title}》 - {best.artist} ({best.score:.1f}分)"
                    )
                    continue

                ok = await loop.run_in_executor(
                    None,
                    tag_engine.write_tags,
                    meta.filepath,
                    best,
                    cfg.get("auto_embed_cover", True),
                    cfg.get("auto_save_lrc", True),
                    None,
                    req.convert_to_simplified,
                )
                if ok:
                    state.batch_success += 1
                    add_log(
                        f"[{idx}/{state.batch_total}] ✅ 已写入: {meta.filename} -> "
                        f"《{best.title}》 - {best.artist} (专辑: {best.album})"
                    )
                    new_meta = await loop.run_in_executor(
                        None, tag_engine.read_metadata, meta.filepath
                    )
                    if new_meta:
                        for pos, item in enumerate(state.scanned_files):
                            if item.filepath == meta.filepath:
                                state.scanned_files[pos] = new_meta
                                break
                else:
                    state.batch_failed += 1
                    add_log(f"[{idx}/{state.batch_total}] ❌ 写入失败: {meta.filename}")

            except Exception as exc:
                state.batch_failed += 1
                add_log(f"[{idx}/{state.batch_total}] ❌ 异常 ({meta.filename}): {exc}")

            await asyncio.sleep(0.05)

        add_log(
            f"🏁 批量任务结束 | 成功: {state.batch_success}, 跳过: {state.batch_skipped}, "
            f"失败/未匹配: {state.batch_failed}"
        )
    finally:
        # 无论正常结束、被停止还是异常退出，都保证缓存落盘
        try:
            tag_engine.flush_cache()
        except Exception:
            pass
        state.batch_running = False
        state.batch_current_file = ""


@app.post("/api/music/batch_scrape/start")
async def start_batch_scrape(req: BatchScrapeRequest):
    """启动批量任务：用锁原子占位，防止并发重复启动。"""
    async with state.batch_lock:
        if state.batch_running:
            raise HTTPException(status_code=409, detail="批量任务正在运行中")
        state.batch_running = True
        state.batch_stop_requested = False
        state.batch_total = 0
        state.batch_current = 0
        state.batch_current_file = ""
        state.batch_logs = []
        try:
            state.batch_task = asyncio.create_task(run_batch_scrape_task(req))
        except Exception:
            state.batch_running = False
            raise HTTPException(status_code=500, detail="无法启动批量任务")
    return {"status": "started"}


@app.get("/api/music/batch_scrape/status")
async def get_batch_scrape_status():
    pct = 0.0
    if state.batch_total > 0:
        pct = round((state.batch_current / state.batch_total) * 100, 1)
    return {
        "running": state.batch_running,
        "stopping": state.batch_stop_requested,
        "total": state.batch_total,
        "current": state.batch_current,
        "percentage": pct,
        "success": state.batch_success,
        "skipped": state.batch_skipped,
        "failed": state.batch_failed,
        "current_file": state.batch_current_file,
        "recent_logs": list(state.batch_logs[-40:]),
    }


@app.post("/api/music/batch_scrape/stop")
async def stop_batch_scrape():
    if state.batch_running:
        state.batch_stop_requested = True
        return {"status": "stopping"}
    return {"status": "not_running"}


# -----------------------------------------------------------------------------
# 运行日志（前台排查入口）
#   日志文件：<状态目录>/logs/app.log（RotatingFileHandler 轮转为 app.log.1 / app.log.2）
# -----------------------------------------------------------------------------
# 日志行首时间戳（logging 默认格式：2024-01-01 12:00:00,123，兼容带毫秒/无毫秒）
_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)\s+"
    r"\[(?P<level>[A-Za-z]+)\]\s?(?P<msg>.*)$",
    re.S,
)
# 对外只暴露固定枚举，避免日志内容被当作可解析结构
_LOG_LEVEL_TAGS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _log_level_tag(level_name: str) -> str:
    name = (level_name or "").upper()
    if name in ("WARN", "WARNING"):
        return "WARNING"
    if name in ("FATAL", "CRITICAL"):
        return "CRITICAL"
    return name if name in _LOG_LEVEL_TAGS else "INFO"


def _log_files() -> List[Dict[str, Any]]:
    """当前在用的日志文件列表（最新在前），只认 app.log 及其轮转备份。

    排序依据是修改时间：正在写入的 app.log 永远最新，轮转后 app.log 被清空重建、
    app.log.1 才是旧的，因此不能按文件名里的轮转序号判断新旧。
    """
    pattern = re.compile(r"^app\.log(?:\.\d+)?$")
    files: List[Dict[str, Any]] = []
    try:
        entries = list(os.scandir(LOG_DIR))
    except OSError:
        return files
    for entry in entries:
        if not pattern.match(entry.name):
            continue
        try:
            if not entry.is_file():
                continue
            stat = entry.stat()
        except OSError:
            continue
        suffix = entry.name[len("app.log"):]
        rotation = int(suffix[1:]) if suffix else 0
        files.append({
            "name": entry.name,
            "path": entry.path,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "rotation": rotation,
        })
    files.sort(key=lambda item: (item["mtime"], -item["rotation"]), reverse=True)
    return files


def _read_file_tail(path: str, max_lines: int) -> Tuple[List[Dict[str, Any]], bool]:
    """从单个日志文件尾部倒序读取，返回 (时间正序记录, 是否因条数上限被截断)。

    逐块（64KB）从文件尾向前扫描，不整体载入文件，内存占用恒定。
    单块内用 splitlines(keepends=True) 切分：
      - 最后一段不带换行符 => 该行不完整，属于更靠前的一块，留作 carry；
      - 其余段都是完整行。
    读完一块后把该块拼接到 carry 之前，保证行序正确。
    """
    records: List[Dict[str, Any]] = []
    truncated = False
    chunk_size = 64 * 1024
    carry = b""
    try:
        fh = open(path, "rb")
    except OSError:
        return records, False

    with fh:
        try:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
        except OSError as exc:
            logger.warning("日志文件定位失败 %s: %s", path, exc)
            return records, False

        while pos > 0:
            read_size = min(chunk_size, pos)
            pos -= read_size
            try:
                fh.seek(pos)
                chunk = fh.read(read_size)
            except OSError as exc:
                logger.warning("日志文件读取失败 %s: %s", path, exc)
                break
            if not chunk:
                if carry.strip() and len(records) < max_lines:
                    records.append(_parse_log_line(carry, len(records)))
                break

            data = chunk + carry
            pieces = data.splitlines(keepends=True)
            if pieces and not pieces[-1].endswith(b"\n"):
                carry = pieces.pop()  # 不完整行：属于更靠前的块
            else:
                carry = b""

            for piece in reversed(pieces):
                line = piece.rstrip(b"\r\n")
                if not line.strip():
                    continue
                if len(records) >= max_lines:
                    truncated = True
                    break
                records.append(_parse_log_line(line, len(records)))
            if truncated:
                break

        if pos == 0 and carry.strip() and not truncated:
            # 文件开头的一行可能没有换行符结尾
            if len(records) >= max_lines:
                truncated = True
            else:
                records.append(_parse_log_line(carry, len(records)))

    records.reverse()
    return records, truncated


def _tail_log_records(max_lines: int, current_only: bool = False) -> Tuple[List[Dict[str, Any]], bool]:
    """读取最近 max_lines 条日志（current_only=True 时只读当前 app.log）。

    返回 (records, truncated)，records 按时间正序（最旧 -> 最新）。
    """
    records: List[Dict[str, Any]] = []
    truncated = False
    for log_file in _log_files():  # 最新文件在前
        if len(records) >= max_lines:
            truncated = True
            break
        part, part_truncated = _read_file_tail(log_file["path"], max_lines - len(records))
        records.extend(part)
        if part_truncated or current_only:
            truncated = truncated or part_truncated
            break
    if len(records) > max_lines:
        records = records[-max_lines:]
        truncated = True
    # 文件按“新 -> 旧”处理、每个文件内部按时间正序，拼接结果即为时间正序
    for index, rec in enumerate(records):
        rec["seq"] = index
    return records, truncated


def _parse_log_line(raw: bytes, seq: int) -> Dict[str, Any]:
    """解析单行日志；无时间戳前缀的行标记为堆栈续行（continuation）。"""
    text = raw.decode("utf-8", errors="replace").rstrip("\r")
    match = _LOG_LINE_RE.match(text)
    if match:
        return {
            "seq": seq,
            "time": match.group("ts").replace("T", " ").replace(",", "."),
            "level": _log_level_tag(match.group("level")),
            "message": match.group("msg"),
            "continuation": False,
        }
    return {
        "seq": seq,
        "time": "",
        "level": "",
        "message": text,
        "continuation": True,
    }


def _client_logger() -> logging.Logger:
    """前端日志专用记录器：沿父级记录器配置，统一落到 app.log。"""
    return logging.getLogger(CLIENT_LOG_NAME)


@app.get("/api/logs")
async def get_logs_api(
    lines: int = Query(300, ge=1, le=LOG_MAX_LINES),
    level: str = Query("ALL", max_length=16),
    keyword: str = Query("", max_length=200),
    file: str = Query("all", max_length=32),
):
    """返回最近的运行日志（时间正序）。

    - lines: 返回的最大行数（1 ~ 5000）
    - level: ALL / DEBUG / INFO / WARNING / ERROR / CRITICAL（含更高级别）
    - keyword: 关键字过滤（不区分大小写的子串匹配，包含堆栈续行）
    - file: all（含轮转备份）| current（仅当前 app.log）
    """
    started = time.perf_counter()
    if file not in ("all", "current"):
        raise HTTPException(status_code=400, detail="file 参数仅支持 all 或 current")

    records, truncated = _tail_log_records(int(lines), current_only=(file == "current"))

    level_name = (level or "ALL").strip().upper()
    thresholds = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    min_level = thresholds.get(level_name)

    items = records
    if min_level is not None:
        # 堆栈续行本身没有级别，跟随其所属的上一条日志；关键词匹配到的续行才能一并展示
        filtered_by_level: List[Dict[str, Any]] = []
        last_level = ""
        for rec in items:
            if rec["continuation"]:
                if last_level and thresholds.get(last_level, 20) >= min_level:
                    filtered_by_level.append(rec)
                continue
            last_level = rec["level"]
            if last_level and thresholds.get(last_level, 20) >= min_level:
                filtered_by_level.append(rec)
        items = filtered_by_level

    needle = (keyword or "").strip().lower()
    if needle:
        # 关键字命中时保留紧随其后的堆栈续行，便于直接看到异常原因
        filtered: List[Dict[str, Any]] = []
        keep_continuation = False
        for rec in items:
            if rec["continuation"]:
                if keep_continuation:
                    filtered.append(rec)
                continue
            keep_continuation = needle in rec["message"].lower()
            if keep_continuation:
                filtered.append(rec)
        items = filtered

    files = _log_files()
    latest = files[0] if files else None
    return {
        "items": items,
        "total_returned": len(items),
        "scanned": len(records),
        "truncated": truncated,
        "requested_lines": int(lines),
        "level": level_name,
        "keyword": keyword or "",
        "file": file,
        "files": [
            {
                "name": f["name"],
                "size": f["size"],
                "mtime": f["mtime"],
                "rotation": f["rotation"],
            }
            for f in files
        ],
        "log_dir": LOG_DIR,
        "log_file": LOG_FILE,
        "current_size": latest["size"] if latest else 0,
        "max_bytes": LOG_MAX_BYTES,
        "backup_count": LOG_BACKUP_COUNT,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/logs/download")
async def download_logs_api(file: str = Query("current", max_length=32)):
    """下载日志文件（仅允许 app.log 与轮转备份，防止任意文件读取）。"""
    files = _log_files()
    if not files:
        raise HTTPException(status_code=404, detail="暂无日志文件")
    if file == "current":
        target = files[0]["path"]
    else:
        # 只接受白名单文件名，且必须在 LOG_DIR 之内
        if not re.match(r"^app\.log(?:\.\d+)?$", file or ""):
            raise HTTPException(status_code=400, detail="非法的日志文件名")
        target = os.path.join(LOG_DIR, file)
        if not security.is_within(target, LOG_DIR) or not os.path.isfile(target):
            raise HTTPException(status_code=404, detail="日志文件不存在")
    try:
        with open(target, "rb") as fh:
            payload = fh.read()
    except OSError as exc:
        logger.warning("读取日志文件失败 %s: %s", target, exc)
        raise HTTPException(status_code=500, detail="日志文件读取失败")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Response(
        content=payload,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="musictag-{os.path.basename(target)}-{stamp}.txt"',
            "Cache-Control": "no-store",
        },
    )


class ClientLogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["debug", "info", "warning", "error"] = "error"
    message: str = Field(..., min_length=1, max_length=CLIENT_LOG_MAX_CHARS * 2)
    source: str = Field("", max_length=200)
    stack: str = Field("", max_length=4000)
    url: str = Field("", max_length=500)


@app.post("/api/logs/client")
async def post_client_log_api(payload: ClientLogRequest):
    """前端异常 / 关键操作回写服务端日志。

    典型用法：在浏览器里点某个按钮报错后，直接在“运行日志”里看到前后端对照，
    无需再开 F12 控制台。所有字段做长度截断，防止日志被刷爆。
    """
    level_map = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }
    client_logger = _client_logger()
    if not client_logger.isEnabledFor(level_map[payload.level]):
        return {"status": "ignored"}

    source = re.sub(r"[\r\n]+", " ", payload.source or "").strip()[:200] or "-"
    page = re.sub(r"[\r\n]+", " ", payload.url or "").strip()[:200]
    message = re.sub(r"[\r\n]+", " ", payload.message or "").strip()[:CLIENT_LOG_MAX_CHARS]
    line = f"[WebUI] {source} | {message}"
    if page:
        line += f" | {page}"

    stack = (payload.stack or "").strip()
    if stack and payload.level in ("warning", "error"):
        # 堆栈按行缩进，与后端异常堆栈格式一致（日志页可折叠展示）
        indented = "\n".join("    " + ln.rstrip() for ln in stack.splitlines()[:40])
        line += "\n" + indented[:4000]

    client_logger.log(level_map[payload.level], line)
    return {"status": "ok"}


# -----------------------------------------------------------------------------
# 启动入口
# -----------------------------------------------------------------------------
def _resolve_port(cfg: Dict[str, Any]) -> int:
    """端口优先级：MUSICTAG_PORT > TRIM_SERVICE_PORT > PORT > 配置文件，非法值回退 8002。

    飞牛的 cmd/main 会显式导出 MUSICTAG_PORT=TRIM_SERVICE_PORT，两边保持一致。
    """
    for env_name in ("MUSICTAG_PORT", "TRIM_SERVICE_PORT", "PORT"):
        raw = (os.environ.get(env_name) or "").strip()
        if not raw:
            continue
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                return port
        except ValueError:
            logger.warning("环境变量 %s 不是合法端口: %s", env_name, raw)
    try:
        port = int(cfg.get("port", 8002))
        if 1 <= port <= 65535:
            return port
    except (TypeError, ValueError):
        logger.warning("配置文件端口非法，回退 8002")
    return 8002


def _probe_listen(host: str, port: int) -> bool:
    """尝试在目标地址绑定一次，用于判断端口是否被占用（监听前检查）。"""
    import socket

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True
    except OSError as exc:
        logger.warning("端口预检失败 %s:%s -> %s", host, port, exc)
        return False
    finally:
        sock.close()



def main() -> None:
    cfg = load_config()
    port = _resolve_port(cfg)
    host = (os.environ.get("MUSICTAG_HOST") or "0.0.0.0").strip() or "0.0.0.0"
    paths = describe_paths()

    print("=" * 70)
    print("🎵 飞牛 NAS 原生音乐刮削服务 (MusicTag Native)")
    print(f"🌐 访问地址: http://<NAS-IP>:{port} 或 http://127.0.0.1:{port}")
    print(f"📁 音乐库目录: {cfg.get('music_dir') or '(未设置)'}")
    print(f"📦 代码目录: {paths['base_dir']}")
    print(f"🗂️  状态目录: {paths['state_dir']}")
    print(f"🔐 访问令牌: {os.path.join(paths['state_dir'], 'auth_token')} (启动时自动注入 Web 页面)")
    if host == "0.0.0.0":
        print("⚠️  已监听所有网卡，请勿在不可信网络暴露，或设置 MUSICTAG_HOST=127.0.0.1")
    print("=" * 70)

    uvicorn.run(app, host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
