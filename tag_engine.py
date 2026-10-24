"""
飞牛 NAS 原生音乐刮削服务 - 音频标签与扫描引擎 (Tag Engine)
===========================================================
支持格式：MP3 (ID3v2.3), FLAC (Vorbis Comment), M4A (MP4 Tags), WAV, OGG 等。
核心功能：
1. 递归扫描指定音乐目录，智能提取现有标签与文件名搜索词；
2. 过滤广告、杂质与规则识别（如 01. 歌曲、歌手 - 歌名、歌名 - 歌手、上级专辑目录推断）；
3. 联动多源刮削引擎 (MusicScraperEngine)，自动或半自动获取元数据；
4. 安全原子写入音频内嵌标签（标题、艺术家、专辑、音轨、年代、流派、高清封面、内嵌歌词）；
5. 同步保存外部标准同名 .lrc 歌词文件，支持各类播放器无缝展示。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx
import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    ID3,
    APIC,
    PictureType,
    TALB,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TRCK,
    TYER,
    USLT,
    TXXX,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

import security
from app_paths import (
    AUDIO_EXTS,
    CACHE_FILE as DEFAULT_CACHE_FILE,
    STATE_DIR as DEFAULT_STATE_DIR,
)
from scraper_engine import MusicCandidate, MusicScraperEngine

# 配置统一日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("TagEngine")

MAX_COVER_BYTES = 15 * 1024 * 1024
# 曲库缓存最小落盘间隔（秒）：批量刮削时避免每首歌都全量重写 JSON + fsync
CACHE_SAVE_INTERVAL = 30.0

try:
    import zhconv
    HAS_ZHCONV = True
except ImportError:
    HAS_ZHCONV = False


def to_simplified_chinese(text: str) -> str:
    """将繁体中文转换为简体中文（字符级转换，避免“西元→公元”这类词级改写）。"""
    if not text or not HAS_ZHCONV:
        return text or ""
    try:
        return zhconv.convert(str(text), "zh-hans")
    except Exception:
        return str(text)


def sanitize_filename(name: str) -> str:
    """清洗文件名中的非法字符，生成安全合法的文件名"""
    if not name:
        return ""
    # 替换 Windows 和 Linux 文件系统保留字符: / \ : * ? " < > |
    cleaned = re.sub(r'[\\/*?:"<>|]', '_', name)
    cleaned = re.sub(r'[\r\n\t]', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip('. ')


def normalize_genre(raw_genre: str) -> str:
    """标准化与归一化音乐流派 (参照主流音乐网站体系)"""
    if not raw_genre:
        return "流行"
    g = str(raw_genre).strip()
    g_low = g.lower()

    if any(k in g_low for k in ["pop", "流行"]):
        return "流行"
    if any(k in g_low for k in ["rock", "摇滚", "punk", "metal", "朋克", "金属"]):
        return "摇滚"
    if any(k in g_low for k in ["folk", "民谣", "country", "乡村"]):
        return "民谣"
    if any(k in g_low for k in ["rap", "hip-hop", "hiphop", "说唱", "嘻哈"]):
        return "说唱"
    if any(k in g_low for k in ["electronic", "dance", "edm", "电子", "舞曲", "house", "techno"]):
        return "电子"
    if any(k in g_low for k in ["soundtrack", "ost", "原声", "score", "影视"]):
        return "影视原声"
    if any(k in g_low for k in ["classical", "古典", "symphony", "交响"]):
        return "古典"
    if any(k in g_low for k in ["jazz", "爵士", "blues", "蓝调"]):
        return "爵士"
    if any(k in g_low for k in ["r&b", "soul", "节奏布鲁斯", "灵魂乐"]):
        return "R&B"
    if any(k in g_low for k in ["acg", "anime", "动漫", "游戏"]):
        return "ACG动漫"
    if any(k in g_low for k in ["古风", "国风", "中国风", "民乐", "民族"]):
        return "古风国韵"
    if any(k in g_low for k in ["instrumental", "纯音乐", "轻音乐", "piano", "guitar"]):
        return "纯音乐"

    clean_g = sanitize_filename(g[:16])
    return clean_g if clean_g else "流行"


def detect_language(title: str, artist: str, genre: str = "") -> str:
    """智能推断歌曲语种 (参照主流音乐网站: 华语、欧美、粤语、日语、韩语、纯音乐)"""
    comb = f"{title} {artist} {genre}"
    comb_low = comb.lower()

    if any(k in comb_low for k in ["instrumental", "纯音乐", "伴奏", "off vocal", "karaoke"]):
        return "纯音乐"

    # 日语假名检测 (平假名 \u3040-\u309f, 片假名 \u30a0-\u30ff)
    if re.search(r'[\u3040-\u30ff]', comb):
        return "日语"

    # 韩文检测 (\uac00-\ud7af)
    if re.search(r'[\uac00-\ud7af]', comb):
        return "韩语"

    # 经典粤语关键词与著名粤语歌手
    if "粤语" in comb_low or "cantonese" in comb_low:
        return "粤语"

    # 汉字检测 (\u4e00-\u9fa5)
    # 说明：不再按歌手名硬编码判定粤语——陈奕迅《十年》、王菲《红豆》、张学友《吻别》
    # 等都是国语作品，按歌手归类会大量误判。语种优先沿用已刮削到的 genre 标注，
    # 未标注时按字符集回落到「华语」，粤语仅在元数据明确标注时生效。
    if re.search(r'[\u4e00-\u9fa5]', comb):
        return "华语"

    # 英文字母 / 拉丁字母
    if re.search(r'[a-zA-Z]', comb):
        return "欧美"

    return "华语"


def format_organized_path(
    meta: AudioMetadata,
    pattern: str = "standard",
    convert_to_simplified: bool = False
) -> Tuple[str, str]:
    """
    根据给定的归类组织规则，计算出相对目录 (relative_dir) 与规范文件名 (target_filename)
    返回: (relative_dir, target_filename)
    """
    artist = (meta.artist or meta.suggested_artist or "未知歌手").strip()
    album = (meta.album or "未分类专辑").strip()
    title = (meta.title or meta.suggested_title or os.path.splitext(meta.filename)[0]).strip()
    year = (meta.year or "").strip()
    genre = normalize_genre(meta.genre)
    language = getattr(meta, "language", "") or detect_language(title, artist, genre)
    ext = meta.ext or os.path.splitext(meta.filename)[1].lower() or ".mp3"
    if not ext.startswith("."):
        ext = "." + ext

    track_no = meta.track_number
    if track_no:
        pad_len = 2 if (meta.total_tracks or 0) < 100 else 3
        track_str = str(track_no).zfill(pad_len)
    else:
        track_str = "01"

    if convert_to_simplified:
        artist = to_simplified_chinese(artist)
        album = to_simplified_chinese(album)
        title = to_simplified_chinese(title)
        genre = to_simplified_chinese(genre)
        language = to_simplified_chinese(language)

    s_artist = sanitize_filename(artist) or "未知歌手"
    s_album = sanitize_filename(album) or "未分类专辑"
    s_title = sanitize_filename(title) or "未命名曲目"
    s_genre = sanitize_filename(genre) or "流行"
    s_language = sanitize_filename(language) or "华语"
    s_year = sanitize_filename(year)

    # 1. 经典标准: 歌手 / 专辑 / 01. 歌名.ext
    if pattern in ["standard", "artist_album"]:
        rel_dir = os.path.join(s_artist, s_album)
        filename = f"{track_str}. {s_title}{ext}"

    # 2. 年代专辑: 歌手 / 2004 - 专辑 / 01. 歌名.ext
    elif pattern in ["era_album", "artist_era_album"]:
        alb_name = f"{s_year} - {s_album}" if s_year else s_album
        rel_dir = os.path.join(s_artist, alb_name)
        filename = f"{track_str}. {s_title}{ext}"

    # 3. 流派曲风: 流派 / 歌手 / 专辑 / 01. 歌名.ext
    elif pattern == "genre_artist_album":
        rel_dir = os.path.join(s_genre, s_artist, s_album)
        filename = f"{track_str}. {s_title}{ext}"

    # 4. 语种流派: 语种 / 流派 / 歌手 / 专辑 / 01. 歌名.ext
    elif pattern == "lang_genre_artist_album":
        rel_dir = os.path.join(s_language, s_genre, s_artist, s_album)
        filename = f"{track_str}. {s_title}{ext}"

    # 5. 音乐网站全能: 语种 / 歌手 / 年代 - 专辑 / 01. 歌手 - 歌名.ext
    elif pattern == "lang_artist_era_album":
        alb_name = f"{s_year} - {s_album}" if s_year else s_album
        rel_dir = os.path.join(s_language, s_artist, alb_name)
        filename = f"{track_str}. {s_artist} - {s_title}{ext}"

    # 6. 单层目录: 歌手 - 专辑 / 01. 歌名.ext
    elif pattern == "flat_artist_album":
        rel_dir = f"{s_artist} - {s_album}"
        filename = f"{track_str}. {s_title}{ext}"

    # 7. 自定义规则: 支持占位符 {artist}, {album}, {year}, {genre}, {language}, {track}, {title}
    else:
        cust = pattern.strip()
        cust = cust.replace("{artist}", s_artist)
        cust = cust.replace("{album}", s_album)
        cust = cust.replace("{year}", s_year)
        cust = cust.replace("{genre}", s_genre)
        cust = cust.replace("{language}", s_language)
        cust = cust.replace("{track}", track_str)
        cust = cust.replace("{title}", s_title)

        parts = re.split(r'[\\/]', cust)
        parts = [sanitize_filename(p) for p in parts if p.strip()]
        if not parts:
            rel_dir = os.path.join(s_artist, s_album)
            filename = f"{track_str}. {s_title}{ext}"
        elif len(parts) == 1:
            rel_dir = ""
            filename = parts[0]
            if not filename.lower().endswith(ext):
                filename += ext
        else:
            rel_dir = os.path.join(*parts[:-1])
            filename = parts[-1]
            if not filename.lower().endswith(ext):
                filename += ext

    return rel_dir, filename


@dataclass
class AudioMetadata:
    """音频文件现有元数据及扫描信息"""
    filepath: str
    filename: str
    ext: str
    size_bytes: int = 0
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track_number: Optional[int] = None
    total_tracks: Optional[int] = None
    year: str = ""
    genre: str = ""
    language: str = ""
    has_cover: bool = False
    has_lyric: bool = False
    has_lrc_file: bool = False
    suggested_title: str = ""
    suggested_artist: str = ""
    suggested_query: str = ""
    album_matched: bool = False
    album_source: str = ""
    scraped: bool = False
    scraped_source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class FilenameParser:
    """音频文件名与路径智能解析器"""

    # 常见网络广告杂质过滤正则 (例如 [91flac.com], www.xxx.cn, 【官方高品质】等)
    CLEAN_NOISE_RE = re.compile(
        r"(\[.*?flac.*?\]|\[.*?mp3.*?\]|\[.*?无损.*?\]|\(.*?高品质.*?\)|【.*?独家.*?】|www\.[a-zA-Z0-9.\-_]+\.[a-zA-Z]{2,4})",
        re.IGNORECASE
    )

    # 前缀纯音轨号正则 (如 "01. ", "01 - ", "01 ", "1-01. ")
    TRACK_PREFIX_RE = re.compile(r"^(?:(\d{1,2})[-_])?(\d{1,3})[\s.\-_]+(.+)$")

    @classmethod
    def clean_noise(cls, s: str) -> str:
        """去除文件名中的广告和杂质字符"""
        if not s:
            return ""
        s = cls.CLEAN_NOISE_RE.sub("", s)
        return s.strip()

    @classmethod
    def parse(cls, filepath: str, existing_title: str = "", existing_artist: str = "") -> Tuple[str, str, str]:
        """
        结合已有标签、文件名及所在父目录名，推断最佳搜索词 (title, artist, full_query)
        """
        # 1. 如果现有标签已经非常完整，且不是广告
        if existing_title and existing_artist:
            c_title = cls.clean_noise(existing_title)
            c_artist = cls.clean_noise(existing_artist)
            if c_title and c_artist and len(c_title) > 0 and len(c_artist) > 0:
                return c_title, c_artist, f"{c_title} {c_artist}"

        # 2. 从文件名提取
        basename = os.path.splitext(os.path.basename(filepath))[0]
        cleaned_base = cls.clean_noise(basename)

        # 检查是否包含音轨号前缀
        track_no = None
        m_track = cls.TRACK_PREFIX_RE.match(cleaned_base)
        if m_track:
            cleaned_base = m_track.group(3).strip()

        title = ""
        artist = ""

        # 识别分隔符（如 "歌手 - 歌名" 或 "歌名 - 歌手"）
        if " - " in cleaned_base:
            parts = [p.strip() for p in cleaned_base.split(" - ", 1)]
            p1, p2 = parts[0], parts[1]
            # 优先以原有已有标签做辅助参考
            if existing_artist and (existing_artist.lower() in p1.lower()):
                artist, title = p1, p2
            elif existing_artist and (existing_artist.lower() in p2.lower()):
                title, artist = p1, p2
            elif existing_title and (existing_title.lower() in p1.lower()):
                title, artist = p1, p2
            elif existing_title and (existing_title.lower() in p2.lower()):
                artist, title = p1, p2
            else:
                # 默认华语音乐最常见命名惯例: "歌名 - 歌手" 或 "歌手 - 歌名"
                # 在此两段同时保留，并输出组合 query
                title, artist = p1, p2
        else:
            title = cleaned_base

        # 3. 结合父目录信息补充
        parent_dir = os.path.basename(os.path.dirname(filepath))
        parent_dir_clean = cls.clean_noise(parent_dir)
        if not artist and " - " in parent_dir_clean:
            # 父目录形如 "张学友 - 活出生命Live演唱会"
            d_parts = parent_dir_clean.split(" - ", 1)
            artist = d_parts[0].strip()
        elif not artist and parent_dir_clean and parent_dir_clean.lower() not in ["music", "flac", "mp3", "download", "music_library"]:
            artist = parent_dir_clean

        full_query = f"{title} {artist}".strip() if artist else title
        return title, artist, full_query


class AudioTagEngine:
    """音频扫描与标签安全写入引擎"""

    SUPPORTED_EXTS = set(AUDIO_EXTS)
    format_organized_path = staticmethod(format_organized_path)

    def __init__(
        self,
        timeout: float = 10.0,
        cache_file: Optional[str] = None,
        state_dir: Optional[str] = None,
    ):
        self.timeout = timeout
        self.client: Optional[httpx.Client] = None
        self.state_dir = state_dir or DEFAULT_STATE_DIR
        self.cache_file = cache_file or DEFAULT_CACHE_FILE
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = threading.RLock()
        self._cache_dirty = False
        self._cache_last_save = 0.0
        self._scan_lock = threading.Lock()
        self._load_cache()

    def close(self) -> None:
        client = self.client
        self.client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _load_cache(self):
        try:
            if os.path.isfile(self.cache_file):
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    with self._cache_lock:
                        self._cache = loaded
                    logger.info("已加载曲库本地缓存: %d 条记录", len(loaded))
        except Exception as e:
            logger.warning("读取曲库缓存失败，将使用空缓存: %s", e)
            self._cache = {}

    def _save_cache(self):
        with self._cache_lock:
            try:
                security.atomic_write_json(self.cache_file, self._cache, mode=0o600)
                self._cache_dirty = False
                self._cache_last_save = time.monotonic()
            except Exception as e:
                logger.warning("保存曲库缓存失败: %s", e)

    def _save_cache_debounced(self) -> None:
        """标记脏并最多每 CACHE_SAVE_INTERVAL 秒落盘一次，避免逐曲全量写盘（I/O 写放大）。

        数据安全兜底：即使进程异常退出，最多丢失该时间窗内的缓存增量；
        缓存本身可由音乐库重新扫描重建，因此可接受。
        """
        with self._cache_lock:
            self._cache_dirty = True
            now = time.monotonic()
            if now - self._cache_last_save < CACHE_SAVE_INTERVAL:
                return
        self._save_cache()

    def flush_cache(self) -> None:
        """批量任务结束后强制落盘（有脏数据时才写）。"""
        with self._cache_lock:
            if not self._cache_dirty:
                return
        self._save_cache()

    def update_cache_item(self, filepath: str, meta: AudioMetadata):
        """为单曲即时更新缓存 (0 延迟)"""
        try:
            if os.path.exists(filepath):
                st = os.stat(filepath)
                with self._cache_lock:
                    self._cache[filepath] = {
                        "mtime": st.st_mtime,
                        "size": st.st_size,
                        "meta": meta.to_dict(),
                    }
                self._save_cache_debounced()
        except Exception as e:
            logger.warning("更新缓存项失败 (%s): %s", filepath, e)

    @staticmethod
    def _unique_case_tmp(target: str) -> str:
        """为大小写中转改名胜出一个不存在的临时路径（不覆盖任何现有文件）。"""
        directory = os.path.dirname(target)
        base = os.path.basename(target)
        for suffix in range(100):
            candidate = os.path.join(directory, f".{base}.case{suffix}.tmp")
            if not os.path.exists(candidate):
                return candidate
        raise OSError(f"无法为 {base} 生成临时文件名")

    def rename_file(
        self,
        filepath: str,
        new_filename: str,
        sync_lrc: bool = True
    ) -> Tuple[bool, str, str]:
        """
        同步重命名物理音频文件及同名 .lrc 歌词文件，并自动维护持久化缓存映射
        :param filepath: 当前音频文件完整物理路径
        :param new_filename: 新文件名（可包含或不包含原有扩展名）
        :param sync_lrc: 是否同步重命名同名 .lrc 歌词文件
        :return: (成功状态, 新文件物理路径, 提示信息)
        """
        import time
        if not os.path.isfile(filepath):
            return False, filepath, f"源音频文件不存在: {filepath}"

        dir_name = os.path.dirname(filepath)
        old_ext = os.path.splitext(filepath)[1].lower()

        clean_new = sanitize_filename(new_filename)
        if not clean_new:
            return False, filepath, "新文件名为空或仅包含非法字符"

        # 确保扩展名与源文件一致
        if not clean_new.lower().endswith(old_ext):
            clean_new += old_ext

        new_filepath = os.path.join(dir_name, clean_new)

        # 检查是否未做修改
        if os.path.abspath(filepath) == os.path.abspath(new_filepath):
            return True, filepath, "文件名未做改动"

        same_normcase = os.path.normcase(os.path.abspath(filepath)) == os.path.normcase(
            os.path.abspath(new_filepath)
        )
        if same_normcase:
            # 仅大小写变化（大小写不敏感文件系统）。
            # 部分 Windows/网络盘上直接 os.rename 会抛 WinError 183(目标已存在)，
            # 此时经由临时名中转两次重命名；中转文件命名成「目标名 + 不存在后缀」，
            # 确保不会覆盖目录中其他真实文件。
            try:
                os.rename(filepath, new_filepath)
            except OSError:
                tmp_name = self._unique_case_tmp(new_filepath)
                try:
                    os.rename(filepath, tmp_name)
                except OSError as exc:
                    return False, filepath, f"修改大小写失败: {exc}"
                try:
                    os.rename(tmp_name, new_filepath)
                except OSError as exc:
                    # 回滚，避免文件停留在临时名下
                    try:
                        os.rename(tmp_name, filepath)
                    except OSError:
                        pass
                    return False, filepath, f"修改大小写失败: {exc}"
        else:
            # 目标已存在则拒绝，绝不覆盖用户文件
            if os.path.exists(new_filepath):
                return False, filepath, f"目标文件名已存在: {clean_new}"
            try:
                os.rename(filepath, new_filepath)
            except OSError as exc:
                return False, filepath, f"重命名文件失败: {exc}"

        logger.info("📁 文件已重命名: %s -> %s", os.path.basename(filepath), clean_new)

        # 同步重命名同名 .lrc 歌词文件（不覆盖已存在的其他歌词）
        if sync_lrc:
            old_lrc = os.path.splitext(filepath)[0] + ".lrc"
            new_lrc = os.path.splitext(new_filepath)[0] + ".lrc"
            if os.path.isfile(old_lrc):
                same_lrc = os.path.normcase(os.path.abspath(old_lrc)) == os.path.normcase(
                    os.path.abspath(new_lrc)
                )
                if not same_lrc and os.path.exists(new_lrc):
                    logger.warning("目标歌词已存在，保留原歌词文件: %s", new_lrc)
                else:
                    try:
                        os.replace(old_lrc, new_lrc)
                        logger.info(
                            "📜 同步重命名歌词: %s -> %s",
                            os.path.basename(old_lrc),
                            os.path.basename(new_lrc),
                        )
                    except OSError as exc:
                        logger.warning("同步重命名歌词失败 (%s): %s", old_lrc, exc)

        # 维护缓存映射：旧路径清除，新路径写入
        try:
            with self._cache_lock:
                self._cache.pop(filepath, None)
            new_meta = self.read_metadata(new_filepath)
            if new_meta:
                self.update_cache_item(new_filepath, new_meta)
            else:
                self._save_cache()
        except Exception as exc:
            logger.warning("更新重命名后缓存失败: %s", exc)

        return True, new_filepath, f"成功重命名为 {clean_new}"

    def clean_empty_parent_dirs(self, dir_path: str, stop_at_dir: str):
        """递归清理空文件夹，严格限制在 stop_at_dir 之内。"""
        try:
            stop = os.path.abspath(stop_at_dir)
            current = os.path.abspath(dir_path)
            while current and os.path.normcase(current) != os.path.normcase(stop):
                try:
                    rel = os.path.relpath(current, stop)
                except ValueError:
                    break
                if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
                    break
                if not os.path.isdir(current) or os.listdir(current):
                    break
                os.rmdir(current)
                logger.info("🧹 已清理空文件夹: %s", current)
                current = os.path.dirname(current)
        except OSError as exc:
            logger.debug("清理空目录异常: %s", exc)

    def organize_file(
        self,
        filepath: str,
        target_dir: str,
        new_filename: str,
        sync_lrc: bool = True,
        clean_empty_dirs: bool = True,
        music_base_dir: Optional[str] = None,
        conflict_mode: str = "rename"
    ) -> Tuple[bool, str, str]:
        """
        将音频文件自动归类迁移至目标目录结构并维护持久化缓存映射
        :param filepath: 当前音频文件完整物理路径
        :param target_dir: 目标物理目录完整路径
        :param new_filename: 目标文件名 (包含扩展名)
        :param sync_lrc: 是否同步移动同名 .lrc 歌词文件
        :param clean_empty_dirs: 搬迁后是否清理原文件夹中的空目录
        :param music_base_dir: 音乐库根路径 (清理空目录的停止边界)
        :param conflict_mode: "rename" (重名自动加序号), "skip" (跳过), "overwrite" (强制覆盖)
        :return: (成功状态, 新文件物理路径, 提示信息)
        """
        if not os.path.isfile(filepath):
            return False, filepath, f"源音频文件不存在: {filepath}"

        old_ext = os.path.splitext(filepath)[1].lower()
        clean_new = sanitize_filename(new_filename)
        if not clean_new:
            return False, filepath, "目标文件名为空或仅包含非法字符"

        if not clean_new.lower().endswith(old_ext):
            clean_new += old_ext

        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception as de:
            return False, filepath, f"创建目标目录失败 ({target_dir}): {de}"

        new_filepath = os.path.join(target_dir, clean_new)

        # 检查是否完全无需变动
        if os.path.normcase(os.path.abspath(filepath)) == os.path.normcase(os.path.abspath(new_filepath)):
            return True, filepath, "文件已位于目标规范路径，无需变动"

        # 检查重名冲突（仅大小写差异在 Linux 上同样视为冲突）
        if os.path.exists(new_filepath):
            if conflict_mode == "skip":
                return False, filepath, f"目标文件已存在，已跳过: {clean_new}"
            if conflict_mode != "overwrite":
                # rename 模式：添加序号 (1), (2)...
                base_n, ext_n = os.path.splitext(clean_new)
                counter = 1
                while os.path.exists(new_filepath):
                    new_filepath = os.path.join(target_dir, f"{base_n} ({counter}){ext_n}")
                    counter += 1

        # 执行物理搬移（overwrite 模式使用原子替换，失败不会先删除目标文件）
        old_dir = os.path.dirname(filepath)
        try:
            self._move_atomic(filepath, new_filepath)
            logger.info("🚚 音频文件已归档迁移: %s -> %s", os.path.basename(filepath), new_filepath)
        except Exception as exc:
            return False, filepath, f"移动文件失败: {exc}"

        # 同步搬迁同名 .lrc 歌词文件
        if sync_lrc:
            old_lrc = os.path.splitext(filepath)[0] + ".lrc"
            new_lrc = os.path.splitext(new_filepath)[0] + ".lrc"
            if os.path.isfile(old_lrc):
                same_lrc = os.path.normcase(os.path.abspath(old_lrc)) == os.path.normcase(
                    os.path.abspath(new_lrc)
                )
                if not same_lrc and os.path.exists(new_lrc) and conflict_mode != "overwrite":
                    logger.warning("目标歌词已存在，未覆盖: %s", new_lrc)
                else:
                    try:
                        self._move_atomic(old_lrc, new_lrc)
                        logger.info("📜 同步迁移歌词: %s -> %s", os.path.basename(old_lrc), new_lrc)
                    except Exception as exc:
                        logger.warning("同步迁移歌词失败 (%s): %s", old_lrc, exc)

        # 清理源空文件夹
        if (
            clean_empty_dirs
            and music_base_dir
            and os.path.normcase(os.path.abspath(old_dir)) != os.path.normcase(os.path.abspath(music_base_dir))
        ):
            self.clean_empty_parent_dirs(old_dir, music_base_dir)

        # 维护缓存映射：清除旧路径，写入新路径
        try:
            with self._cache_lock:
                self._cache.pop(filepath, None)
            new_meta = self.read_metadata(new_filepath)
            if new_meta:
                self.update_cache_item(new_filepath, new_meta)
            else:
                self._save_cache()
        except Exception as exc:
            logger.warning("更新归类后缓存失败: %s", exc)

        return True, new_filepath, f"成功归纳迁移至 {os.path.basename(target_dir)}/{os.path.basename(new_filepath)}"

    @staticmethod
    def _move_atomic(src: str, dst: str) -> None:
        """尽量原子地移动文件；跨文件系统时先复制到目标目录再替换。"""
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            import errno as _errno

            if getattr(exc, "errno", None) != _errno.EXDEV:
                raise
        import time as _time

        tmp = os.path.join(os.path.dirname(dst) or ".", f".musictag_move_{os.getpid()}_{int(_time.time() * 1000)}")
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        os.remove(src)

    def _get_client(self) -> httpx.Client:
        if self.client is None or self.client.is_closed:
            self.client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": MusicScraperEngine.DEFAULT_USER_AGENT}
            )
        return self.client

    def download_cover(self, cover_url: str) -> Optional[bytes]:
        """下载封面：逐跳校验重定向、连接钉死已校验 IP，防 SSRF 与超大文件。"""
        if not cover_url:
            return None
        try:
            data, final_url = security.safe_fetch_cover(cover_url, MAX_COVER_BYTES)
        except ValueError as exc:
            logger.warning("封面地址非法或获取失败 (%s): %s", str(cover_url)[:80], exc)
            return None
        except Exception as exc:
            logger.warning("下载封面失败 (%s...): %s", str(cover_url)[:60], exc)
            return None
        if len(data) < 100:
            return None
        if not security.detect_image_mime(data):
            logger.warning("封面不是受支持的图片格式，已忽略: %s", final_url)
            return None
        return data

    def read_metadata(self, filepath: str, stat_info: Optional[os.stat_result] = None) -> Optional[AudioMetadata]:
        """读取指定音频文件的当前元数据"""
        if not os.path.exists(filepath):
            return None

        ext = os.path.splitext(filepath)[1].lower()
        if ext not in self.SUPPORTED_EXTS:
            return None

        try:
            if stat_info is None:
                stat_info = os.stat(filepath)
        except Exception:
            return None

        filename = os.path.basename(filepath)
        lrc_path = os.path.splitext(filepath)[0] + ".lrc"
        has_lrc_file = os.path.exists(lrc_path)

        meta = AudioMetadata(
            filepath=filepath,
            filename=filename,
            ext=ext,
            size_bytes=stat_info.st_size,
            has_lrc_file=has_lrc_file
        )

        try:
            if ext in (".mp3", ".wav"):
                audio = MP3(filepath) if ext == ".mp3" else WAVE(filepath)
                if audio.tags:
                    tags = audio.tags
                    meta.title = str(tags.get("TIT2", ""))
                    meta.artist = str(tags.get("TPE1", ""))
                    meta.album = str(tags.get("TALB", ""))
                    meta.album_artist = str(tags.get("TPE2", ""))
                    trck = str(tags.get("TRCK", ""))
                    if trck:
                        parts = trck.split("/")
                        if parts[0].isdigit():
                            meta.track_number = int(parts[0])
                        if len(parts) > 1 and parts[1].isdigit():
                            meta.total_tracks = int(parts[1])
                    yd = tags.get("TDRC") or tags.get("TYER")
                    if yd:
                        meta.year = str(yd)[:4]
                    meta.genre = str(tags.get("TCON", ""))
                    meta.has_cover = bool(tags.getall("APIC"))
                    meta.has_lyric = any(k.startswith("USLT") for k in tags.keys())
                    # 读取整辑对齐与刮削标记
                    for k in tags.keys():
                        if "ALBUM_MATCHED" in k:
                            meta.album_matched = True
                        elif "ALBUM_SOURCE" in k:
                            try:
                                meta.album_source = str(tags[k])
                            except Exception:
                                pass
                        elif "SCRAPED" in k and "SOURCE" not in k:
                            meta.scraped = True
                        elif "SCRAPED_SOURCE" in k:
                            try:
                                meta.scraped_source = str(tags[k])
                            except Exception:
                                pass

            elif ext in (".flac", ".ogg"):
                audio = FLAC(filepath) if ext == ".flac" else OggVorbis(filepath)
                if audio.tags:
                    tags = audio.tags
                    meta.title = (tags.get("TITLE") or [""])[0]
                    meta.artist = (tags.get("ARTIST") or [""])[0]
                    meta.album = (tags.get("ALBUM") or [""])[0]
                    meta.album_artist = (tags.get("ALBUMARTIST") or [""])[0]
                    raw_trck = (tags.get("TRACKNUMBER") or [""])[0]
                    if raw_trck:
                        parts = str(raw_trck).split("/")
                        if parts[0].strip().isdigit():
                            meta.track_number = int(parts[0].strip())
                        if len(parts) > 1 and parts[1].strip().isdigit():
                            meta.total_tracks = int(parts[1].strip())
                    raw_tot = (tags.get("TRACKTOTAL") or tags.get("TOTALTRACKS") or [""])[0]
                    if raw_tot and str(raw_tot).strip().isdigit():
                        meta.total_tracks = int(str(raw_tot).strip())
                    raw_date = (tags.get("DATE") or tags.get("YEAR") or [""])[0]
                    if raw_date:
                        meta.year = str(raw_date)[:4]
                    meta.genre = (tags.get("GENRE") or [""])[0]
                    meta.has_lyric = bool(tags.get("LYRICS") or tags.get("UNSYNCEDLYRICS"))
                    # 读取整辑对齐与刮削标记
                    if (tags.get("ALBUM_MATCHED") or [""])[0] in ("1", "true", "True"):
                        meta.album_matched = True
                    meta.album_source = (tags.get("ALBUM_SOURCE") or [""])[0]
                    if (tags.get("SCRAPED") or [""])[0] in ("1", "true", "True"):
                        meta.scraped = True
                    meta.scraped_source = (tags.get("SCRAPED_SOURCE") or [""])[0]
                if ext == ".flac":
                    meta.has_cover = bool(audio.pictures)
                else:
                    meta.has_cover = bool(
                        audio.tags
                        and (audio.tags.get("METADATA_BLOCK_PICTURE") or audio.tags.get("COVERART"))
                    )

            elif ext in [".m4a", ".mp4"]:
                audio = MP4(filepath)
                if audio.tags:
                    meta.title = audio.tags.get("\xa9nam", [""])[0]
                    meta.artist = audio.tags.get("\xa9ART", [""])[0]
                    meta.album = audio.tags.get("\xa9alb", [""])[0]
                    meta.album_artist = audio.tags.get("aART", [""])[0]
                    trkn = audio.tags.get("trkn")
                    if trkn and isinstance(trkn, list) and len(trkn) > 0:
                        meta.track_number = trkn[0][0]
                        if trkn[0][1]:
                            meta.total_tracks = trkn[0][1]
                    raw_day = audio.tags.get("\xa9day", [""])[0]
                    if raw_day:
                        meta.year = str(raw_day)[:4]
                    meta.genre = audio.tags.get("\xa9gen", [""])[0]
                    meta.has_cover = bool(audio.tags.get("covr"))
                    meta.has_lyric = bool(audio.tags.get("\xa9lyr"))
                    # 读取整辑对齐与刮削标记
                    if audio.tags.get("----:com.apple.iTunes:ALBUM_MATCHED"):
                        meta.album_matched = True
                    source_raw = audio.tags.get("----:com.apple.iTunes:ALBUM_SOURCE")
                    if source_raw and isinstance(source_raw, list) and len(source_raw) > 0:
                        try:
                            meta.album_source = source_raw[0].decode("utf-8") if isinstance(source_raw[0], bytes) else str(source_raw[0])
                        except Exception:
                            pass
                    if audio.tags.get("----:com.apple.iTunes:SCRAPED"):
                        meta.scraped = True
                    scraped_src_raw = audio.tags.get("----:com.apple.iTunes:SCRAPED_SOURCE")
                    if scraped_src_raw and isinstance(scraped_src_raw, list) and len(scraped_src_raw) > 0:
                        try:
                            meta.scraped_source = scraped_src_raw[0].decode("utf-8") if isinstance(scraped_src_raw[0], bytes) else str(scraped_src_raw[0])
                        except Exception:
                            pass

        except Exception as e:
            logger.warning(f"读取元数据失败 ({filepath}): {e}")

        # 智能判定：若文件本身包含正规整轨专辑标签 (album, artist, track_number, total_tracks >= 2)
        if not meta.album_matched:
            if meta.album and meta.artist and meta.track_number and meta.total_tracks and meta.total_tracks > 1:
                meta.album_matched = True

        # 智能判定：若文件已被整辑对齐，或拥有完整的标题、歌手、封面与歌词，视为已完成刮削
        if not meta.scraped:
            if meta.album_matched or (meta.has_cover and (meta.has_lyric or meta.has_lrc_file) and meta.title and meta.artist):
                meta.scraped = True

        # 计算建议搜索词
        s_title, s_artist, s_query = FilenameParser.parse(filepath, meta.title, meta.artist)
        meta.suggested_title = s_title
        meta.suggested_artist = s_artist
        meta.suggested_query = s_query

        # 智能判定语种分类
        meta.language = detect_language(meta.title or s_title, meta.artist or s_artist, meta.genre)

        return meta

    def scan_directory(self, dir_path: str, recursive: bool = True, force_rescan: bool = False) -> List[AudioMetadata]:
        """
        递归扫描指定目录下的全部支持音频文件
        采用 MTime + FileSize 增量快检机制：
        未修改的文件 0 磁盘 I/O 毫秒级命中缓存；仅对新增或已修改文件进行 mutagen 解析。
        """
        import time
        results: List[AudioMetadata] = []
        if not os.path.exists(dir_path):
            logger.error(f"目录不存在: {dir_path}")
            return results

        t_start = time.time()
        logger.info(f"开始扫描音频目录: {dir_path} (递归={recursive}, 强制重解={force_rescan})")
        walk_errors = []
        cache_hits = 0
        cache_misses = 0
        cache_updated = False
        scanned_full_paths = set()

        def on_walk_error(err):
            walk_errors.append(err)
            logger.warning(f"扫描目录遇到权限或读取异常: {err}")

        def process_file(root_dir: str, filename: str):
            nonlocal cache_hits, cache_misses, cache_updated
            ext = os.path.splitext(filename)[1].lower()
            if ext in self.SUPPORTED_EXTS:
                full_path = os.path.abspath(os.path.join(root_dir, filename))
                scanned_full_paths.add(full_path)
                try:
                    stat_info = os.stat(full_path)
                except Exception as se:
                    logger.warning(f"获取文件状态失败 ({full_path}): {se}")
                    return

                with self._cache_lock:
                    cached_entry = self._cache.get(full_path)
                if (
                    not force_rescan
                    and cached_entry
                    and cached_entry.get("mtime") == stat_info.st_mtime
                    and cached_entry.get("size") == stat_info.st_size
                    and "meta" in cached_entry
                ):
                    try:
                        meta = AudioMetadata(**cached_entry["meta"])
                        if not getattr(meta, "language", None):
                            meta.language = detect_language(meta.title, meta.artist, meta.genre)
                        if not meta.album_matched and meta.album and meta.artist and meta.track_number and meta.total_tracks and meta.total_tracks > 1:
                            meta.album_matched = True
                        if not meta.scraped:
                            if meta.album_matched or (meta.has_cover and (meta.has_lyric or meta.has_lrc_file) and meta.title and meta.artist):
                                meta.scraped = True
                        results.append(meta)
                        cache_hits += 1
                        return
                    except Exception:
                        pass

                # 缓存未命中或文件变动：重新解析
                meta = self.read_metadata(full_path, stat_info=stat_info)
                if meta:
                    results.append(meta)
                    with self._cache_lock:
                        self._cache[full_path] = {
                            "mtime": stat_info.st_mtime,
                            "size": stat_info.st_size,
                            "meta": meta.to_dict()
                        }
                    cache_misses += 1
                    cache_updated = True

        ignore_dirs = {"@eadir", "#recycle", ".trash", ".git", "__pycache__", "@tmp"}
        with self._scan_lock:
            if recursive:
                for root, dirs, files in os.walk(dir_path, onerror=on_walk_error):
                    dirs[:] = [d for d in dirs if d.lower() not in ignore_dirs and not d.startswith(".")]
                    for f in files:
                        process_file(root, f)
            else:
                try:
                    for f in os.listdir(dir_path):
                        full_p = os.path.join(dir_path, f)
                        if os.path.isfile(full_p):
                            process_file(dir_path, f)
                except Exception as e:
                    on_walk_error(e)

            if not results and walk_errors:
                raise PermissionError(f"目录权限受限无法读取: {walk_errors[0]}")

            # 清理缓存中已不存在的文件
            abs_scan_dir = os.path.abspath(dir_path)
            with self._cache_lock:
                keys_to_remove = [
                    k for k in self._cache.keys()
                    if security.is_within(k, abs_scan_dir) and k not in scanned_full_paths and not os.path.exists(k)
                ]
                if keys_to_remove:
                    for k in keys_to_remove:
                        self._cache.pop(k, None)
                    cache_updated = True

            elapsed = time.time() - t_start
            if cache_updated:
                self._save_cache()

        logger.info(
            f"⚡ 扫描完成: 共 {len(results)} 首音频 | 命中缓存: {cache_hits} 首, 新增/重解: {cache_misses} 首, 耗时: {elapsed:.3f}s"
        )
        return results

    def write_tags(
        self,
        filepath: str,
        cand: MusicCandidate,
        embed_cover: bool = True,
        save_lrc: bool = True,
        cover_bytes: Optional[bytes] = None,
        convert_to_simplified: bool = False
    ) -> bool:
        """
        安全写入选中的音乐标签至音频文件（支持 MP3/FLAC/M4A）并保存同名 .lrc
        :param convert_to_simplified: 是否自动将繁体中文标签转换为简体中文
        """
        if not os.path.exists(filepath):
            logger.error(f"文件不存在: {filepath}")
            return False

        ext = os.path.splitext(filepath)[1].lower()
        if ext not in self.SUPPORTED_EXTS:
            logger.error(f"不支持的音频扩展名: {ext}")
            return False

        # 如果开启繁转简，在写入前自动转换各项元数据
        if convert_to_simplified:
            cand.title = to_simplified_chinese(cand.title)
            cand.artist = to_simplified_chinese(cand.artist)
            if cand.album:
                cand.album = to_simplified_chinese(cand.album)
            if cand.album_artist:
                cand.album_artist = to_simplified_chinese(cand.album_artist)
            if cand.genre:
                cand.genre = to_simplified_chinese(cand.genre)
            if cand.lyric:
                cand.lyric = to_simplified_chinese(cand.lyric)

        # 保留原文件权限，仅确保属主可写（不破坏 NAS 用户组和其它服务的只读权限）
        try:
            current_mode = stat.S_IMODE(os.stat(filepath).st_mode)
            os.chmod(filepath, current_mode | stat.S_IWUSR)
        except Exception:
            pass

        preserve_cover = bool(cand.extra.get("preserve_cover", False))
        preserve_lyric = bool(cand.extra.get("preserve_lyric", False))

        # 校验封面数据 MIME 类型，未知格式则忽略
        cover_mime = "image/jpeg"
        if cover_bytes:
            detected_mime = security.detect_image_mime(cover_bytes)
            if detected_mime:
                cover_mime = detected_mime
            else:
                logger.warning(f"传入的封面二进制数据不是合法图片，忽略该封面: {filepath}")
                cover_bytes = None

        # 预先下载封面（支持 HTTP URL 或 data:image base64）
        if embed_cover and not preserve_cover and cover_bytes is None and cand.cover_url:
            if cand.cover_url.startswith("data:"):
                try:
                    cover_bytes = security.decode_data_uri(cand.cover_url)
                    detected_mime = security.detect_image_mime(cover_bytes)
                    if detected_mime:
                        cover_mime = detected_mime
                    else:
                        cover_bytes = None
                except Exception as be:
                    logger.warning(f"解码 data URI 封面失败: {be}")
            else:
                cover_bytes = self.download_cover(cand.cover_url)
                if cover_bytes:
                    detected_mime = security.detect_image_mime(cover_bytes)
                    if detected_mime:
                        cover_mime = detected_mime
                    else:
                        cover_bytes = None

        try:
            if ext in (".mp3", ".wav"):
                from mutagen.wave import WAVE
                audio = MP3(filepath) if ext == ".mp3" else WAVE(filepath)
                if audio.tags is None:
                    audio.add_tags()
                tags = audio.tags

                # 基础标签 (使用 UTF-8 编码 encoding=3，避免乱码)
                if cand.title:
                    tags.setall("TIT2", [TIT2(encoding=3, text=[cand.title])])
                else:
                    tags.delall("TIT2")

                if cand.artist:
                    tags.setall("TPE1", [TPE1(encoding=3, text=[cand.artist])])
                else:
                    tags.delall("TPE1")

                # 专辑与专辑艺术家
                if cand.album:
                    tags.setall("TALB", [TALB(encoding=3, text=[cand.album])])
                else:
                    tags.delall("TALB")

                if cand.album_artist:
                    tags.setall("TPE2", [TPE2(encoding=3, text=[cand.album_artist])])
                elif not cand.album:
                    tags.delall("TPE2")
                elif cand.artist:
                    tags.setall("TPE2", [TPE2(encoding=3, text=[cand.artist])])
                else:
                    tags.delall("TPE2")

                # 音轨号
                if cand.track_number:
                    trck_val = f"{cand.track_number}/{cand.total_tracks}" if cand.total_tracks else str(cand.track_number)
                    tags.setall("TRCK", [TRCK(encoding=3, text=[trck_val])])
                else:
                    tags.delall("TRCK")

                # 年代
                if cand.year:
                    tags.setall("TDRC", [TDRC(encoding=3, text=[cand.year])])
                    tags.setall("TYER", [TYER(encoding=3, text=[cand.year])])
                else:
                    tags.delall("TDRC")
                    tags.delall("TYER")

                # 流派
                if cand.genre:
                    tags.setall("TCON", [TCON(encoding=3, text=[cand.genre])])
                else:
                    tags.delall("TCON")

                # 嵌入歌词
                if not preserve_lyric:
                    if cand.lyric:
                        tags.setall("USLT", [
                            USLT(encoding=3, lang="chi", desc="", text=cand.lyric)
                        ])
                    else:
                        tags.delall("USLT")

                # 嵌入或清除封面
                clear_cover = bool(cand.extra.get("clear_cover", False))
                if not preserve_cover:
                    if cover_bytes:
                        tags.setall("APIC", [
                            APIC(
                                encoding=3,
                                mime=cover_mime,
                                type=PictureType.COVER_FRONT,
                                desc="Cover",
                                data=cover_bytes
                            )
                        ])
                    elif clear_cover:
                        tags.delall("APIC")

                # 整张专辑对齐与来源标记 (若专辑为空，彻底删除标记)
                if not cand.album:
                    tags.delall("TXXX:ALBUM_MATCHED")
                    tags.delall("TXXX:ALBUM_SOURCE")
                else:
                    is_matched = bool(cand.extra.get("album_matched", False)) or (cand.source in ["album_batch", "netease", "qqmusic"] and cand.total_tracks and cand.total_tracks > 1)
                    if is_matched:
                        tags.setall("TXXX:ALBUM_MATCHED", [TXXX(encoding=3, desc="ALBUM_MATCHED", text=["1"])])
                        if cand.source and cand.source_id:
                            tags.setall("TXXX:ALBUM_SOURCE", [TXXX(encoding=3, desc="ALBUM_SOURCE", text=[f"{cand.source}:{cand.source_id}"])])
                    else:
                        tags.delall("TXXX:ALBUM_MATCHED")
                        tags.delall("TXXX:ALBUM_SOURCE")

                # 单曲刮削标记写入
                is_scraped = bool(cand.extra.get("scraped", False)) or (cand.source and cand.source not in ["manual", ""]) or (bool(cand.album) and is_matched)
                if is_scraped:
                    tags.setall("TXXX:SCRAPED", [TXXX(encoding=3, desc="SCRAPED", text=["1"])])
                    if cand.source:
                        tags.setall("TXXX:SCRAPED_SOURCE", [TXXX(encoding=3, desc="SCRAPED_SOURCE", text=[cand.source])])

                if ext == ".mp3":
                    audio.save(v2_version=3)
                else:
                    audio.save()

            elif ext in (".flac", ".ogg"):
                from mutagen.oggvorbis import OggVorbis
                audio = FLAC(filepath) if ext == ".flac" else OggVorbis(filepath)
                if cand.title:
                    audio["TITLE"] = [cand.title]
                else:
                    audio.pop("TITLE", None)

                if cand.artist:
                    audio["ARTIST"] = [cand.artist]
                else:
                    audio.pop("ARTIST", None)

                if cand.album:
                    audio["ALBUM"] = [cand.album]
                else:
                    audio.pop("ALBUM", None)

                if cand.album_artist:
                    audio["ALBUMARTIST"] = [cand.album_artist]
                elif not cand.album:
                    audio.pop("ALBUMARTIST", None)
                elif cand.artist:
                    audio["ALBUMARTIST"] = [cand.artist]
                else:
                    audio.pop("ALBUMARTIST", None)

                if cand.track_number:
                    audio["TRACKNUMBER"] = [str(cand.track_number)]
                else:
                    audio.pop("TRACKNUMBER", None)

                if cand.total_tracks:
                    audio["TRACKTOTAL"] = [str(cand.total_tracks)]
                else:
                    audio.pop("TRACKTOTAL", None)

                if cand.year:
                    audio["DATE"] = [cand.year]
                else:
                    audio.pop("DATE", None)
                    audio.pop("YEAR", None)

                if cand.genre:
                    audio["GENRE"] = [cand.genre]
                else:
                    audio.pop("GENRE", None)

                if not preserve_lyric:
                    if cand.lyric:
                        audio["LYRICS"] = [cand.lyric]
                    else:
                        audio.pop("LYRICS", None)
                        audio.pop("UNSYNCEDLYRICS", None)

                clear_cover = bool(cand.extra.get("clear_cover", False))
                if not preserve_cover:
                    if cover_bytes:
                        pic = Picture()
                        pic.type = 3  # front cover
                        pic.mime = cover_mime
                        pic.desc = "Cover"
                        pic.data = cover_bytes
                        if ext == ".flac":
                            audio.clear_pictures()
                            audio.add_picture(pic)
                        else:
                            audio["METADATA_BLOCK_PICTURE"] = [base64.b64encode(pic.write()).decode("ascii")]
                    elif clear_cover:
                        if ext == ".flac":
                            audio.clear_pictures()
                        else:
                            audio.pop("METADATA_BLOCK_PICTURE", None)
                            audio.pop("COVERART", None)

                # 整张专辑对齐与来源标记 (若专辑为空，彻底删除标记)
                if not cand.album:
                    audio.pop("ALBUM_MATCHED", None)
                    audio.pop("ALBUM_SOURCE", None)
                else:
                    is_matched = bool(cand.extra.get("album_matched", False)) or (cand.source in ["album_batch", "netease", "qqmusic"] and cand.total_tracks and cand.total_tracks > 1)
                    if is_matched:
                        audio["ALBUM_MATCHED"] = ["1"]
                        if cand.source and cand.source_id:
                            audio["ALBUM_SOURCE"] = [f"{cand.source}:{cand.source_id}"]
                    else:
                        audio.pop("ALBUM_MATCHED", None)
                        audio.pop("ALBUM_SOURCE", None)

                # 单曲刮削标记写入
                is_scraped = bool(cand.extra.get("scraped", False)) or (cand.source and cand.source not in ["manual", ""]) or (bool(cand.album) and is_matched)
                if is_scraped:
                    audio["SCRAPED"] = ["1"]
                    if cand.source:
                        audio["SCRAPED_SOURCE"] = [cand.source]

                audio.save()

            elif ext in [".m4a", ".mp4"]:
                audio = MP4(filepath)
                if cand.title:
                    audio["\xa9nam"] = [cand.title]
                else:
                    audio.pop("\xa9nam", None)

                if cand.artist:
                    audio["\xa9ART"] = [cand.artist]
                else:
                    audio.pop("\xa9ART", None)

                if cand.album:
                    audio["\xa9alb"] = [cand.album]
                else:
                    audio.pop("\xa9alb", None)

                if cand.album_artist:
                    audio["aART"] = [cand.album_artist]
                elif not cand.album:
                    audio.pop("aART", None)
                elif cand.artist:
                    audio["aART"] = [cand.artist]
                else:
                    audio.pop("aART", None)

                if cand.track_number:
                    audio["trkn"] = [(int(cand.track_number), int(cand.total_tracks or 0))]
                else:
                    audio.pop("trkn", None)

                if cand.year:
                    audio["\xa9day"] = [str(cand.year)]
                else:
                    audio.pop("\xa9day", None)

                if cand.genre:
                    audio["\xa9gen"] = [cand.genre]
                else:
                    audio.pop("\xa9gen", None)

                if not preserve_lyric:
                    if cand.lyric:
                        audio["\xa9lyr"] = [cand.lyric]
                    else:
                        audio.pop("\xa9lyr", None)

                clear_cover = bool(cand.extra.get("clear_cover", False))
                if not preserve_cover:
                    if cover_bytes:
                        fmt = MP4Cover.FORMAT_PNG if cover_mime == "image/png" else MP4Cover.FORMAT_JPEG
                        cov = MP4Cover(cover_bytes, imageformat=fmt)
                        audio["covr"] = [cov]
                    elif clear_cover:
                        audio.pop("covr", None)

                # 整张专辑对齐与来源标记 (若专辑为空，彻底删除标记)
                if not cand.album:
                    audio.pop("----:com.apple.iTunes:ALBUM_MATCHED", None)
                    audio.pop("----:com.apple.iTunes:ALBUM_SOURCE", None)
                else:
                    is_matched = bool(cand.extra.get("album_matched", False)) or (cand.source in ["album_batch", "netease", "qqmusic"] and cand.total_tracks and cand.total_tracks > 1)
                    if is_matched:
                        audio["----:com.apple.iTunes:ALBUM_MATCHED"] = [b"1"]
                        if cand.source and cand.source_id:
                            audio["----:com.apple.iTunes:ALBUM_SOURCE"] = [f"{cand.source}:{cand.source_id}".encode("utf-8")]
                    else:
                        audio.pop("----:com.apple.iTunes:ALBUM_MATCHED", None)
                        audio.pop("----:com.apple.iTunes:ALBUM_SOURCE", None)

                # 单曲刮削标记写入
                is_scraped = bool(cand.extra.get("scraped", False)) or (cand.source and cand.source not in ["manual", ""]) or (bool(cand.album) and is_matched)
                if is_scraped:
                    audio["----:com.apple.iTunes:SCRAPED"] = [b"1"]
                    if cand.source:
                        audio["----:com.apple.iTunes:SCRAPED_SOURCE"] = [cand.source.encode("utf-8")]

                audio.save()
            else:
                logger.error(f"不支持写入标签的音频类型: {ext}")
                return False

            # 外部同名 .lrc 歌词文件同步保存或清理
            lrc_path = os.path.splitext(filepath)[0] + ".lrc"
            if save_lrc and not preserve_lyric:
                if cand.lyric:
                    try:
                        content = cand.lyric if cand.lyric.endswith("\n") else cand.lyric + "\n"
                        with open(lrc_path, "w", encoding="utf-8") as f:
                            f.write(content)
                    except Exception as le:
                        logger.warning(f"保存外部 LRC 文件失败 ({lrc_path}): {le}")
                elif bool(cand.extra.get("clear_lyric", False)) and os.path.exists(lrc_path):
                    try:
                        os.remove(lrc_path)
                        logger.info(f"🗑️ 已同步删除外部歌词文件: {os.path.basename(lrc_path)}")
                    except Exception as de:
                        logger.warning(f"删除外部歌词文件失败: {de}")

            # 标签写入成功后，立即更新本地持久化缓存
            try:
                updated_meta = self.read_metadata(filepath)
                if updated_meta:
                    self.update_cache_item(filepath, updated_meta)
            except Exception as ce:
                logger.warning(f"更新写入后缓存失败 ({filepath}): {ce}")

            logger.info(f"✅ 标签成功写入: {os.path.basename(filepath)} <- 《{cand.title}》 - {cand.artist}")
            return True

        except Exception as e:
            logger.error(f"❌ 标签写入异常 ({filepath}): {e}")
            return False

    def match_file(
        self,
        filepath: str,
        scraper: Optional[MusicScraperEngine] = None,
        limit_per_source: int = 5
    ) -> Tuple[Optional[AudioMetadata], List[MusicCandidate]]:
        """对单个音频文件执行扫描与多源搜索匹配"""
        meta = self.read_metadata(filepath)
        if not meta:
            return None, []

        engine = scraper or MusicScraperEngine()
        candidates = engine.search_sync(
            title=meta.suggested_title,
            artist=meta.suggested_artist,
            query=meta.suggested_query,
            limit_per_source=limit_per_source
        )
        return meta, candidates

    def read_lyric_text(self, filepath: str) -> str:
        """读取指定音频的歌词文本（优先外置 .lrc，其次内嵌歌词）"""
        if not os.path.exists(filepath):
            return ""
        # 1. 优先读取同名 .lrc 文件
        lrc_path = os.path.splitext(filepath)[0] + ".lrc"
        if os.path.exists(lrc_path):
            for enc in ["utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16"]:
                try:
                    with open(lrc_path, "r", encoding=enc) as f:
                        content = f.read()
                        if content.strip():
                            return content
                except Exception:
                    continue

        # 2. 读取内嵌歌词
        ext = os.path.splitext(filepath)[1].lower()
        try:
            if ext in (".mp3", ".wav"):
                from mutagen.wave import WAVE
                audio = MP3(filepath) if ext == ".mp3" else WAVE(filepath)
                if audio.tags:
                    for k, v in audio.tags.items():
                        if k.startswith("USLT") and hasattr(v, "text"):
                            return str(v.text)
            elif ext in (".flac", ".ogg"):
                from mutagen.oggvorbis import OggVorbis
                audio = FLAC(filepath) if ext == ".flac" else OggVorbis(filepath)
                if audio.tags:
                    lyrics = audio.tags.get("LYRICS") or audio.tags.get("UNSYNCEDLYRICS")
                    if lyrics:
                        return "\n".join(lyrics)
            elif ext in [".m4a", ".mp4"]:
                audio = MP4(filepath)
                if audio.tags and "\xa9lyr" in audio.tags and audio.tags["\xa9lyr"]:
                    return "\n".join(audio.tags["\xa9lyr"])
        except Exception as e:
            logger.warning(f"读取内嵌歌词异常 ({filepath}): {e}")
        return ""



# -----------------------------------------------------------------------------
# 命令行测试与演示入口
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="飞牛 NAS 原生音乐标签与扫描引擎 (Tag Engine)")
    parser.add_argument("-d", "--dir", default="", help="待扫描的音乐文件夹路径 (如 D:\\Music)")
    parser.add_argument("-f", "--file", default="", help="单个待测试音频文件路径")
    parser.add_argument("--scan-only", action="store_true", help="仅执行目录扫描与现状分析，不发起网络请求")
    parser.add_argument("--match-only", action="store_true", help="仅执行在线匹配预览，不实际修改本地文件")
    parser.add_argument("--write", action="store_true", help="确认将最佳匹配结果写入音频文件及保存 LRC")
    parser.add_argument("--limit", type=int, default=10, help="处理或展示的文件数量上限")
    args = parser.parse_args()

    tag_engine = AudioTagEngine()
    scraper = MusicScraperEngine()

    # 1. 单文件测试模式
    if args.file:
        target_file = args.file
        print("=" * 75)
        print(f"🎵 正在分析单曲: {target_file}")
        print("=" * 75)
        meta, candidates = tag_engine.match_file(target_file, scraper=scraper)
        if not meta:
            print("❌ 文件无法读取或不是支持的音频格式！")
            sys.exit(1)

        print(f"📁 现有标签状态:")
        print(f"   曲名: '{meta.title or '(空)'}' | 歌手: '{meta.artist or '(空)'}' | 专辑: '{meta.album or '(空)'}'")
        print(f"   内嵌封面: {'[有]' if meta.has_cover else '[无]'} | 内嵌歌词: {'[有]' if meta.has_lyric else '[无]'} | 外置LRC: {'[有]' if meta.has_lrc_file else '[无]'}")
        print(f"   智能解析搜索词: title='{meta.suggested_title}', artist='{meta.suggested_artist}', query='{meta.suggested_query}'\n")

        if args.scan_only:
            sys.exit(0)

        print(f"🔍 在线多源聚合匹配结果 (共 {len(candidates)} 项):")
        for i, c in enumerate(candidates[:5], 1):
            print(f"  [{i}] 【{c.source.upper()}】得分 {c.score:4.1f} | 《{c.title}》 - {c.artist} (专辑: {c.album}, 年份: {c.year})")

        if args.write and candidates:
            best = candidates[0]
            print(f"\n⚡ 正在写入第一候选标签: 《{best.title}》 - {best.artist} (专辑: {best.album})...")
            ok = tag_engine.write_tags(target_file, best, embed_cover=True, save_lrc=True)
            if ok:
                print("🎉 写入成功！可重新检查标签验证。")
            else:
                print("❌ 写入失败，请检查文件权限或格式！")

    # 2. 目录批量处理模式
    elif args.dir:
        target_dir = args.dir
        print("=" * 75)
        print(f"📂 正在扫描目录: {target_dir}")
        print("=" * 75)
        file_metas = tag_engine.scan_directory(target_dir, recursive=True)
        print(f"📊 目录总音频数: {len(file_metas)}")

        missing_cover = sum(1 for m in file_metas if not m.has_cover)
        missing_lyric = sum(1 for m in file_metas if not m.has_lyric and not m.has_lrc_file)
        missing_album = sum(1 for m in file_metas if not m.album)
        print(f"   其中缺封面: {missing_cover} 首 | 缺歌词: {missing_lyric} 首 | 缺专辑名: {missing_album} 首\n")

        # 抽样展示部分文件的推断效果
        print(f"🔍 抽样前 {min(args.limit, len(file_metas))} 首文件标签与建议搜索词预览:")
        for idx, m in enumerate(file_metas[:args.limit], 1):
            cover_s = "✅" if m.has_cover else "❌"
            lyric_s = "✅" if (m.has_lyric or m.has_lrc_file) else "❌"
            print(f"[{idx:02d}] {m.filename}")
            print(f"     现状: 封面[{cover_s}] 歌词[{lyric_s}] 专辑='{m.album or '无'}'")
            print(f"     推断搜索词: title='{m.suggested_title}' | artist='{m.suggested_artist}'")
            print("-" * 75)

    else:
        print("💡 请指定参数:")
        print("   测试单曲: python tag_engine.py -f \"D:\\Music\\0932 - 陈小春.mp3\"")
        print("   扫描目录: python tag_engine.py -d \"D:\\Music\" --scan-only")
