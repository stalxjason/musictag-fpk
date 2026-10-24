"""
安全工具集 (Security Helpers)
=============================
集中实现：访问令牌、路径白名单、远程 URL 校验、图片类型嗅探、安全解压、原子写。

所有函数均把“文件路径 / URL / 上传内容”视为不可信输入（fnOS 权限规范建议）。
"""

from __future__ import annotations

import base64
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import stat
import tempfile
import threading
import zipfile
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from app_paths import AUDIO_EXTS, BASE_DIR, PLUGINS_DIR, STATE_DIR

# ---------------------------------------------------------------------------
# 访问令牌
# ---------------------------------------------------------------------------
_TOKEN_LOCK = threading.Lock()


def load_or_create_token(token_file: str) -> str:
    """读取或生成访问令牌（权限 600）。令牌只用于本机 WebUI/API 鉴权。"""
    with _TOKEN_LOCK:
        try:
            with open(token_file, "r", encoding="utf-8") as fh:
                token = fh.read().strip()
            if len(token) >= 32:
                return token
        except OSError:
            pass

        token = secrets.token_urlsafe(32)
        tmp = token_file + ".tmp"
        os.makedirs(os.path.dirname(token_file), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(token)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, token_file)
        return token


def token_equals(expected: str, supplied: Optional[str]) -> bool:
    if not supplied:
        return False
    return hmac.compare_digest(str(expected), str(supplied))


# ---------------------------------------------------------------------------
# 路径白名单
# ---------------------------------------------------------------------------
def is_within(path: str, root: str) -> bool:
    """判断 path 是否位于 root 之内（解析符号链接，大小写按平台规则）。"""
    if not path or not root:
        return False
    try:
        real_path = os.path.realpath(os.path.abspath(path))
        real_root = os.path.realpath(os.path.abspath(root))
        if os.path.normcase(real_path) == os.path.normcase(real_root):
            return True
        return os.path.commonpath([real_path, real_root]) == real_root
    except (ValueError, OSError):
        return False


def _split_paths(value: str) -> List[str]:
    parts: List[str] = []
    for item in (value or "").split(":"):
        item = item.strip().strip("'\"")
        if item:
            parts.append(item)
    return parts


def build_roots(music_dir: str = "") -> List[str]:
    """返回允许访问的根目录列表（去重、只保留存在的目录）。

    安全原则：只信任「用户显式配置的音乐目录」与「fnOS 通过环境变量授权的共享路径」。
    不再把整个盘符（Windows）或 /mnt、/media、/vol1..8（Linux）当作可访问根目录——
    这些泛化根目录会让路径白名单形同虚设（例如外接盘挂载在 /mnt 时整盘可读写）。
    """
    candidates: List[str] = []

    if music_dir:
        candidates.append(music_dir)

    for env_name in ("TRIM_DATA_ACCESSIBLE_PATHS", "TRIM_DATA_SHARE_PATHS"):
        candidates.extend(_split_paths(os.environ.get(env_name, "")))

    app_name = (os.environ.get("TRIM_APPNAME") or "MusicTagNative").strip() or "MusicTagNative"
    for share in (
        f"/var/apps/{app_name}/shares",
        f"/var/apps/{app_name}/share",
        f"/var/apps/{app_name}/target/shares",
    ):
        candidates.append(share)

    # 本地开发/调试：显式设置该环境变量才追加额外根目录（多个用 os.pathsep 分隔，
    # 与 fnOS 的冒号分隔格式不同，以便 Windows 盘符路径也能正确拆分）
    for extra in (os.environ.get("MUSICTAG_EXTRA_ROOTS", "") or "").split(os.pathsep):
        extra = extra.strip().strip("'\"")
        if extra:
            candidates.append(extra)

    if os.environ.get("MUSICTAG_DEV_UNRESTRICTED") == "1" and os.name == "nt":
        # 仅限本地开发的完全放行开关，默认关闭
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                candidates.append(drive)

    candidates.append(STATE_DIR)

    roots: List[str] = []
    seen = set()
    for cand in candidates:
        try:
            real = os.path.realpath(os.path.abspath(cand))
        except OSError:
            continue
        key = os.path.normcase(real)
        if key in seen:
            continue
        if not os.path.isdir(real):
            continue
        seen.add(key)
        roots.append(real)
    return roots


def is_allowed_path(path: str, roots: Iterable[str]) -> bool:
    if not path:
        return False
    return any(is_within(path, root) for root in roots)


def is_allowed_audio(path: str, roots: Iterable[str]) -> bool:
    if not path or not os.path.isfile(path):
        return False
    if os.path.splitext(path)[1].lower() not in AUDIO_EXTS:
        return False
    return is_allowed_path(path, roots)


def is_allowed_plugin_dir(path: str) -> bool:
    return is_within(path, PLUGINS_DIR)


# ---------------------------------------------------------------------------
# 远程 URL 校验（防 SSRF）
# ---------------------------------------------------------------------------
def validate_remote_url(url: str, allow_private: bool = False) -> str:
    """校验封面/资源 URL，拒绝非 http(s)、内网地址、带凭据的 URL。

    返回规范化后的 URL；不合法时抛出 ValueError。
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL 为空")
    url = url.strip()
    if len(url) > 2048:
        raise ValueError("URL 过长")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("仅支持 http/https URL")
    if parts.username or parts.password:
        raise ValueError("URL 不允许包含凭据")
    host = parts.hostname
    if not host:
        raise ValueError("URL 缺少主机名")
    # 字面量 IP 直接判定
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    def _bad_ip(ip: ipaddress._BaseAddress) -> bool:
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )

    if literal is not None and not allow_private and _bad_ip(literal):
        raise ValueError("不允许访问内网地址")
    if literal is None:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise ValueError(f"域名解析失败: {exc}") from exc
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if not allow_private and _bad_ip(ip):
                raise ValueError("不允许访问内网地址")
    return url


def resolve_allowed_ips(host: str, port: int, allow_private: bool = False) -> List[str]:
    """解析主机名并返回全部合法 IP；存在任一内网 IP 时抛 ValueError。

    调用方应把返回的 IP 钉死到实际连接上（见 safe_fetch_cover），
    避免「校验时解析一次、连接时再解析一次」造成 DNS 重绑定。
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ValueError(f"域名解析失败: {exc}") from exc
    ips: List[str] = []
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if not allow_private and _is_forbidden_ip(ip):
            raise ValueError("不允许访问内网地址")
        if raw not in ips:
            ips.append(raw)
    if not ips:
        raise ValueError("域名未解析到可用地址")
    return ips


def _is_forbidden_ip(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


MAX_REDIRECTS = 3


def _build_connection(scheme: str, pinned_ip: str, port: int, sni_host: str):
    """构造到已校验 IP 的连接；HTTPS 仍以原域名做 SNI 与证书校验。"""
    import http.client
    import ssl

    if scheme == "https":
        # 关键：TCP 连到固定 IP，但用真实域名做 SNI + 证书主机名校验
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(pinned_ip, port, timeout=15, context=ctx)

        def _connect() -> None:
            sock = socket.create_connection((pinned_ip, port), conn.timeout)
            conn.sock = ctx.wrap_socket(sock, server_hostname=sni_host)

        conn.connect = _connect  # type: ignore[method-assign]
        return conn
    return http.client.HTTPConnection(pinned_ip, port, timeout=15)


def safe_fetch_cover(
    url: str,
    max_bytes: int,
    allow_private: bool = False,
    max_redirects: int = MAX_REDIRECTS,
) -> Tuple[bytes, str]:
    """安全下载远程图片：逐跳校验重定向目标，并把连接钉死在已校验的 IP 上。

    返回 (数据, 最终 URL)。任何一跳指向内网、协议非法或体积超限都会抛 ValueError，
    不会被上层当作「下载失败」静默掩盖。
    """
    import http.client

    current = validate_remote_url(url, allow_private=allow_private)
    for _hop in range(max_redirects + 1):
        parts = urlsplit(current)
        host = parts.hostname or ""
        scheme = parts.scheme
        default_port = 443 if scheme == "https" else 80
        port = parts.port or default_port
        # 对当前跳重新解析并校验；返回的 IP 全部可信，取第一个钉死连接
        ips = resolve_allowed_ips(host, port, allow_private=allow_private)
        pinned_ip = ips[0]
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        request_host = host if parts.port in (None, default_port) else f"{host}:{port}"

        conn = _build_connection(scheme, pinned_ip, port, host)
        try:
            try:
                conn.request(
                    "GET",
                    path,
                    headers={
                        "Host": request_host,
                        "User-Agent": "MusicTagNative/1.1",
                        "Accept": "image/*,*/*;q=0.8",
                        "Connection": "close",
                    },
                )
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                raise ValueError(f"封面连接失败: {exc}") from exc
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location", "")
                if not location:
                    raise ValueError("重定向缺少 Location")
                current = urljoin(current, location)
                # 下一跳回到循环顶部重新校验，绝不放行内网目标
                continue
            if resp.status != 200:
                raise ValueError(f"封面请求返回 HTTP {resp.status}")
            chunks: List[bytes] = []
            total = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("封面超过大小限制")
                chunks.append(chunk)
            return b"".join(chunks), current
        finally:
            try:
                conn.close()
            except Exception:
                pass

    raise ValueError("封面重定向次数过多")


# ---------------------------------------------------------------------------
# 图片类型嗅探
# ---------------------------------------------------------------------------
_IMAGE_SIGNATURES: Tuple[Tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def detect_image_mime(data: bytes) -> Optional[str]:
    """根据文件头判断图片 MIME，未知则返回 None。"""
    if not data or len(data) < 4:
        return None
    for signature, mime in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def is_supported_cover(data: bytes) -> bool:
    return detect_image_mime(data) is not None


# ---------------------------------------------------------------------------
# ZIP 安全解压
# ---------------------------------------------------------------------------
class UnsafeArchiveError(ValueError):
    """压缩包内容不安全（路径穿越 / 符号链接 / 超限）。"""


def safe_extract_zip(
    payload: bytes,
    dest_dir: str,
    max_files: int = 500,
    max_total_bytes: int = 50 * 1024 * 1024,
    max_file_bytes: int = 20 * 1024 * 1024,
) -> None:
    """把 ZIP 解压到 dest_dir，拒绝路径穿越、符号链接与超限内容。"""
    if not payload:
        raise UnsafeArchiveError("压缩包为空")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload), "r")
    except zipfile.BadZipFile as exc:
        raise UnsafeArchiveError("不是合法的 ZIP 压缩包") from exc

    base = os.path.abspath(dest_dir)
    os.makedirs(base, exist_ok=True)
    total = 0
    count = 0
    with archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            count += 1
            if count > max_files:
                raise UnsafeArchiveError(f"压缩包文件数量超过上限 {max_files}")
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
                raise UnsafeArchiveError("压缩包包含绝对路径")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if any(p == ".." for p in parts):
                raise UnsafeArchiveError("压缩包包含路径穿越")
            mode = info.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                raise UnsafeArchiveError("压缩包包含符号链接")
            if info.file_size > max_file_bytes:
                raise UnsafeArchiveError("压缩包内单个文件过大")
            total += info.file_size
            if total > max_total_bytes:
                raise UnsafeArchiveError("压缩包解压总大小超限")
            target = os.path.join(base, *parts)
            if not is_within(target, base):
                raise UnsafeArchiveError("压缩包路径越界")
            os.makedirs(os.path.dirname(target) or base, exist_ok=True)
            with archive.open(info, "r") as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 256)


# ---------------------------------------------------------------------------
# 插件标识
# ---------------------------------------------------------------------------
_PLUGIN_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


def normalize_plugin_key(raw: str) -> str:
    key = re.sub(r"[^a-zA-Z0-9_-]", "", str(raw or "")).lower()
    if not key:
        return ""
    if not _PLUGIN_KEY_RE.match(key):
        return ""
    return key


# ---------------------------------------------------------------------------
# 原子写 JSON
# ---------------------------------------------------------------------------
def atomic_write_json(path: str, data: object, mode: int = 0o600) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json(path: str, default: object = None) -> object:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 文件名 / 字节工具
# ---------------------------------------------------------------------------
def decode_data_uri(url: str, max_bytes: int = 20 * 1024 * 1024) -> bytes:
    """解析 data:image/...;base64,... 并限制大小。"""
    if not url.startswith("data:"):
        raise ValueError("不是 data URI")
    header, _, b64data = url.partition(",")
    if ";base64" not in header:
        raise ValueError("仅支持 base64 data URI")
    try:
        raw = base64.b64decode(b64data, validate=True)
    except Exception as exc:
        raise ValueError("data URI 解码失败") from exc
    if len(raw) > max_bytes:
        raise ValueError("图片数据超出大小限制")
    return raw
