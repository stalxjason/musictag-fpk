"""
飞牛 NAS 原生音乐刮削服务 - 多源刮削引擎原型 (Scraper Engine)
============================================================
特点与优势：
1. 纯原生 Python，内存占用极低 (< 20MB)，无 Wine 依赖，跨平台原生运行于 Linux / fnOS / Windows。
2. 多源高并发：同时请求 Apple iTunes、网易云音乐、QQ 音乐、酷我音乐、MusicBrainz。
3. 智能评分与排序：基于 Levenshtein/SequenceMatcher 相似度与繁简归一化算法，自动推荐置信度最高的元数据。
4. 全量元数据覆盖：标题、艺术家、专辑、专辑艺术家、音轨号、年代、流派、最高 3000px 封面图、LRC 同步歌词。
"""

from __future__ import annotations

import asyncio
import datetime
import difflib
import html
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import httpx

try:
    import zhconv
    HAS_ZHCONV = True
except ImportError:
    HAS_ZHCONV = False

CST_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 配置统一日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ScraperEngine")


@dataclass
class MusicCandidate:
    """标准化的音乐元数据模型"""
    source: str                          # 数据源: itunes, netease, qqmusic, kuwo, musicbrainz
    source_id: str                       # 唯一标识符 (song_id, trackId, songmid, mbid 等)
    title: str                           # 歌曲标题
    artist: str                          # 艺术家 / 歌手
    album: str = ""                      # 专辑名称
    album_artist: str = ""               # 专辑艺术家
    track_number: Optional[int] = None      # 音轨序号
    total_tracks: Optional[int] = None      # 专辑总音轨数
    disc_number: Optional[int] = None       # 光盘编号
    total_discs: Optional[int] = None       # 总光盘数
    year: str = ""                       # 发行年份 (YYYY)
    date: str = ""                       # 完整发行日期 (YYYY-MM-DD)
    genre: str = ""                      # 音乐流派
    cover_url: str = ""                  # 高清封面大图 URL
    lyric: str = ""                      # LRC 同步歌词
    tlyric: str = ""                     # LRC 翻译歌词
    duration_ms: Optional[int] = None     # 音频时长 (毫秒)
    score: float = 0.0                   # 匹配综合置信度得分 (0.0 ~ 100.0)
    extra: Dict[str, Any] = field(default_factory=dict) # 额外扩展属性

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典对象"""
        return asdict(self)

    def __post_init__(self) -> None:
        """统一收敛第三方 API 返回的松散数值字段。

        各平台会返回 "3/12"、"03"、3.0、None、"" 之类的混合类型，
        这里一次性规范成 Optional[int]，避免下游 int()/写入标签时抛异常。
        """
        self.track_number, extra_total = _coerce_track_pair(self.track_number)
        self.total_tracks = _coerce_int(self.total_tracks) or extra_total
        self.disc_number = _coerce_int(self.disc_number)
        self.total_discs = _coerce_int(self.total_discs)
        self.duration_ms = _coerce_int(self.duration_ms)


def _coerce_int(value: Any) -> Optional[int]:
    """把任意松散值转成非负整数；无法解析时返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    match = re.match(r"^\d+", text)
    return int(match.group(0)) if match else None


def _coerce_track_pair(value: Any) -> Tuple[Optional[int], Optional[int]]:
    """解析 "3/12" 这类音轨字段，返回 (当前轨, 总轨数)。"""
    if isinstance(value, str) and "/" in value:
        head, _, tail = value.partition("/")
        return _coerce_int(head), _coerce_int(tail)
    return _coerce_int(value), None


class MusicScraperEngine:
    """多源音乐刮削引擎"""

    DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    MB_USER_AGENT = "MusicTagNative/1.0.0 ( https://github.com/musictag-fnos )"

    # MusicBrainz 请求频率限制 (全局锁 + 时间戳，确保两次请求间隔 >= 1.0 秒)
    _mb_lock = asyncio.Lock()
    _last_mb_request_time = 0.0

    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout

    @staticmethod
    def normalize_text(text: str) -> str:
        """标准化文本：简繁归一化、去除符号标点、小写去空"""
        if not text:
            return ""
        # 1. 繁体转简体映射（优先使用 zhconv 进行字符级无歧义映射）
        if HAS_ZHCONV:
            try:
                t = zhconv.convert(str(text), "zh-hans")
            except Exception:
                t = str(text)
        else:
            t = str(text)
        # 2. 小写化
        t = t.lower()
        # 3. 去除各种全半角标点符号及括号
        t = re.sub(r"[\s\-_,，.。!！?？:：'\"`·~()（）\[\]【】/\\|]+", " ", t)
        return t.strip()

    def calculate_score(
        self,
        query_title: str,
        query_artist: str,
        cand_title: str,
        cand_artist: str,
        candidate: MusicCandidate
    ) -> float:
        """
        计算置信度匹配分：
        - 标题匹配度 50%
        - 歌手匹配度 35%
        - 元数据完整度加分 15% (封面+5, 歌词+5, 年份+3, 专辑+2)
          注意：当标题基本不匹配时（title_sim < 0.4），不赋予完整度加分，防止无关歌曲因元数据完整而虚高。
        """
        q_title = self.normalize_text(query_title)
        q_artist = self.normalize_text(query_artist)
        c_title = self.normalize_text(cand_title)
        c_artist = self.normalize_text(cand_artist)

        # 1. 标题相似度 (0 ~ 1.0)
        if q_title and c_title:
            if q_title == c_title:
                title_sim = 1.0
            elif q_title in c_title or c_title in q_title:
                title_sim = 0.92
            else:
                title_sim = difflib.SequenceMatcher(None, q_title, c_title).ratio()
        else:
            title_sim = 0.5

        # 2. 艺术家相似度 (0 ~ 1.0)
        if q_artist and c_artist:
            if q_artist == c_artist:
                artist_sim = 1.0
            elif q_artist in c_artist or c_artist in q_artist:
                artist_sim = 0.95
            else:
                artist_sim = difflib.SequenceMatcher(None, q_artist, c_artist).ratio()
        elif not q_artist:
            artist_sim = 0.85
        else:
            artist_sim = 0.2

        base_score = (title_sim * 50.0) + (artist_sim * 35.0)

        # 3. 元数据完整度加分 (最多 15 分；当标题相似度过低时取消加分)
        bonus = 0.0
        if title_sim >= 0.4:
            if candidate.cover_url:
                bonus += 5.0
            if candidate.lyric:
                bonus += 5.0
            if candidate.year:
                bonus += 3.0
            if candidate.album:
                bonus += 2.0

        return round(min(100.0, base_score + bonus), 1)

    # -------------------------------------------------------------------------
    # 数据源 1: Apple iTunes (免鉴权，极速，支持最高 3000x3000 超清封面)
    # -------------------------------------------------------------------------
    async def scrape_itunes(self, client: httpx.AsyncClient, query: str, limit: int = 5) -> List[MusicCandidate]:
        results: List[MusicCandidate] = []
        # 港区 hk 曲库丰富度高，繁简中文兼容最好，其次美区 us
        for country in ["hk", "cn", "us"]:
            url = "https://itunes.apple.com/search"
            params = {
                "term": query,
                "media": "music",
                "entity": "song",
                "limit": limit,
                "country": country,
            }
            try:
                resp = await client.get(url, params=params, headers={"User-Agent": self.DEFAULT_USER_AGENT})
                if resp.status_code != 200:
                    continue
                data = resp.json()
                raw_items = data.get("results", [])
                if not raw_items:
                    continue

                for item in raw_items:
                    track_id = str(item.get("trackId", ""))
                    title = item.get("trackName", "")
                    artist = item.get("artistName", "")
                    album = item.get("collectionName", "")
                    genre = item.get("primaryGenreName", "")
                    rel_date = item.get("releaseDate", "")
                    year = rel_date[:4] if rel_date else ""
                    date_str = rel_date[:10] if rel_date else ""

                    # 智能替换为最高分辨率 3000x3000bb 原图
                    art_100 = item.get("artworkUrl100", "")
                    cover_url = ""
                    if art_100:
                        cover_url = re.sub(r"/\d+x\d+bb\.", "/3000x3000bb.", art_100)

                    cand = MusicCandidate(
                        source="itunes",
                        source_id=track_id,
                        title=title,
                        artist=artist,
                        album=album,
                        track_number=item.get("trackNumber"),
                        total_tracks=item.get("trackCount"),
                        disc_number=item.get("discNumber"),
                        total_discs=item.get("discCount"),
                        year=year,
                        date=date_str,
                        genre=genre,
                        cover_url=cover_url,
                        duration_ms=item.get("trackTimeMillis"),
                        extra={"preview_url": item.get("previewUrl", ""), "country": country}
                    )
                    results.append(cand)

                if results:
                    break
            except Exception as e:
                logger.warning(f"[iTunes] 请求异常 ({country}): {e}")
        return results

    # -------------------------------------------------------------------------
    # 数据源 2: 网易云音乐 (Netease - 华语、小众独立、逐行 LRC 歌词)
    # -------------------------------------------------------------------------
    async def scrape_netease(self, client: httpx.AsyncClient, query: str, limit: int = 5) -> List[MusicCandidate]:
        results: List[MusicCandidate] = []
        search_url = "https://music.163.com/api/search/get/web"
        search_params = {
            "csrf_token": "",
            "s": query,
            "type": 1,
            "offset": 0,
            "total": "true",
            "limit": limit
        }
        headers = {"User-Agent": self.DEFAULT_USER_AGENT, "Referer": "https://music.163.com"}
        try:
            resp = await client.get(search_url, params=search_params, headers=headers)
            if resp.status_code != 200:
                return results
            songs = resp.json().get("result", {}).get("songs", [])
            if not songs:
                return results

            # 批量获取详情（高清图 + 发布年份）
            song_ids = [s["id"] for s in songs if s.get("id")]
            detail_map: Dict[int, Dict[str, Any]] = {}
            if song_ids:
                detail_url = "https://music.163.com/api/song/detail/"
                detail_params = {
                    "id": str(song_ids[0]),
                    "ids": f"[{','.join(str(i) for i in song_ids)}]"
                }
                try:
                    d_resp = await client.get(detail_url, params=detail_params, headers=headers)
                    if d_resp.status_code == 200:
                        for ds in d_resp.json().get("songs", []):
                            detail_map[ds.get("id")] = ds
                except Exception as de:
                    logger.warning(f"[Netease] 详情获取失败: {de}")

            for s in songs:
                sid = s.get("id")
                title = s.get("name", "")
                artists = ", ".join([a.get("name", "") for a in s.get("artists", []) if a.get("name")])
                album_info = s.get("album", {}) or {}
                album = album_info.get("name", "")

                cover_url = ""
                year = ""
                track_no = None
                if sid in detail_map:
                    ds = detail_map[sid]
                    d_album = ds.get("album", {}) or {}
                    raw_pic = d_album.get("picUrl", "")
                    if raw_pic:
                        cover_url = f"{raw_pic}?param=1400y1400"
                    pub_time = d_album.get("publishTime")
                    if pub_time and isinstance(pub_time, int) and pub_time > 0:
                        try:
                            year = str(datetime.datetime.fromtimestamp(pub_time / 1000.0, tz=CST_TZ).year)
                        except Exception:
                            pass
                    track_no = ds.get("no") or ds.get("position")
                else:
                    raw_pic = album_info.get("picUrl", "")
                    if raw_pic:
                        cover_url = f"{raw_pic}?param=1400y1400"

                cand = MusicCandidate(
                    source="netease",
                    source_id=str(sid),
                    title=title,
                    artist=artists,
                    album=album,
                    track_number=track_no,
                    year=year,
                    cover_url=cover_url,
                    duration_ms=s.get("duration"),
                    extra={"song_id": sid}
                )
                results.append(cand)

            # 并发为最有希望的前 2 位候选获取 LRC 歌词
            async def _fill_lyric(cand: MusicCandidate):
                lrc, tlrc = await self.fetch_netease_lyric(client, cand.source_id)
                cand.lyric = lrc
                cand.tlyric = tlrc

            tasks = [_fill_lyric(c) for c in results[:2]]
            await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            logger.warning(f"[Netease] 请求异常: {e}")
        return results

    async def fetch_netease_lyric(self, client: httpx.AsyncClient, song_id: str) -> Tuple[str, str]:
        """抓取网易云音乐 LRC 歌词与翻译"""
        url = "https://music.163.com/api/song/lyric"
        params = {"os": "pc", "id": song_id, "lv": "-1", "kv": "-1", "tv": "-1"}
        headers = {"User-Agent": self.DEFAULT_USER_AGENT, "Referer": "https://music.163.com"}
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                lrc = data.get("lrc", {}).get("lyric", "") or ""
                tlrc = data.get("tlyric", {}).get("lyric", "") or ""
                return lrc, tlrc
        except Exception as e:
            logger.debug(f"[Netease] 歌词获取失败 (id={song_id}): {e}")
        return "", ""

    # -------------------------------------------------------------------------
    # 数据源 3: QQ 音乐 (国内最全版权曲库、高清封面与同步歌词)
    # -------------------------------------------------------------------------
    async def scrape_qqmusic(self, client: httpx.AsyncClient, query: str, limit: int = 5) -> List[MusicCandidate]:
        results: List[MusicCandidate] = []
        url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
        params = {"p": 1, "n": limit, "w": query, "format": "json"}
        headers = {"User-Agent": self.DEFAULT_USER_AGENT, "Referer": "https://y.qq.com"}
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                return results
            song_list = resp.json().get("data", {}).get("song", {}).get("list", [])
            for s in song_list:
                songmid = s.get("songmid", "")
                title = s.get("songname", "")
                singers = ", ".join([sg.get("name", "") for sg in s.get("singer", []) if sg.get("name")])
                album = s.get("albumname", "")
                albummid = s.get("albummid", "")
                cover_url = f"https://y.gtimg.cn/music/photo_new/T002R800x800M000{albummid}.jpg" if albummid else ""
                
                # 正确解析 Unix 时间戳 pubtime
                pubtime = s.get("pubtime")
                year = ""
                date_str = ""
                if pubtime:
                    try:
                        ts = int(pubtime)
                        if ts > 10000000:
                            dt = datetime.datetime.fromtimestamp(ts, tz=CST_TZ)
                            year = str(dt.year)
                            date_str = dt.strftime("%Y-%m-%d")
                    except Exception:
                        year = str(pubtime)[:4]

                cand = MusicCandidate(
                    source="qqmusic",
                    source_id=songmid,
                    title=title,
                    artist=singers,
                    album=album,
                    year=year,
                    date=date_str,
                    cover_url=cover_url,
                    duration_ms=(s.get("interval") or 0) * 1000,
                    extra={"songmid": songmid, "albummid": albummid}
                )
                results.append(cand)

            # 并发获取歌词
            async def _fill_qq_lyric(cand: MusicCandidate):
                lrc = await self.fetch_qq_lyric(client, cand.source_id)
                cand.lyric = lrc

            if results:
                tasks = [_fill_qq_lyric(c) for c in results[:2]]
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            logger.warning(f"[QQMusic] 请求异常: {e}")
        return results

    async def fetch_qq_lyric(self, client: httpx.AsyncClient, songmid: str) -> str:
        """获取 QQ 音乐 LRC 歌词"""
        url = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
        params = {"songmid": songmid, "format": "json", "nobase64": "1"}
        headers = {"User-Agent": self.DEFAULT_USER_AGENT, "Referer": "https://y.qq.com"}
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code == 200:
                lyric = resp.json().get("lyric", "")
                return html.unescape(lyric)
        except Exception as e:
            logger.debug(f"[QQMusic] 歌词获取失败: {e}")
        return ""

    fetch_qqmusic_lyric = fetch_qq_lyric

    # -------------------------------------------------------------------------
    # 数据源 4: 酷我音乐 (Kuwo - 无损音轨元数据补充)
    # -------------------------------------------------------------------------
    async def scrape_kuwo(self, client: httpx.AsyncClient, query: str, limit: int = 5) -> List[MusicCandidate]:
        results: List[MusicCandidate] = []
        url = "https://search.kuwo.cn/r.s"
        params = {
            "client": "kt",
            "all": query,
            "pn": 0,
            "rn": limit,
            "uid": "794761770",
            "ver": "kwplayer_ar_99.99.99.99",
            "vipver": "1",
            "show_copyright_off": "1",
            "newsearch": "1",
            "ft": "music",
            "cluster": "0",
            "strategy": "2012",
            "encoding": "utf8",
            "rformat": "json"
        }
        headers = {"User-Agent": "okhttp/3.10.0"}
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                return results
            import ast
            try:
                import json
                data = json.loads(resp.text)
            except Exception:
                data = ast.literal_eval(resp.text)

            for item in data.get("abslist", []):
                song_id = item.get("DC_TARGETID") or item.get("MUSICRID", "")
                title = html.unescape(item.get("SONGNAME", ""))
                artist = html.unescape(item.get("ARTIST", ""))
                album = html.unescape(item.get("ALBUM", ""))
                track_no = None
                try:
                    track_no = int(item.get("TRACK", "0")) or None
                except Exception:
                    pass

                cand = MusicCandidate(
                    source="kuwo",
                    source_id=str(song_id),
                    title=title,
                    artist=artist,
                    album=album,
                    track_number=track_no,
                    extra={"kuwo_id": song_id}
                )
                results.append(cand)
        except Exception as e:
            logger.warning(f"[Kuwo] 请求异常: {e}")
        return results

    # -------------------------------------------------------------------------
    # 数据源 5: MusicBrainz (国际权威曲库，标准元数据与 CoverArtArchive 原盘扫描)
    # -------------------------------------------------------------------------
    async def scrape_musicbrainz(
        self,
        client: httpx.AsyncClient,
        title: str,
        artist: str,
        full_query: str,
        limit: int = 5
    ) -> List[MusicCandidate]:
        results: List[MusicCandidate] = []
        url = "https://musicbrainz.org/ws/2/recording"
        
        # 优化查询：如果有具体标题和艺术家，使用字段精准查询语法
        if title and artist:
            mb_query = f'recording:"{title}" AND artist:"{artist}"'
        elif title:
            mb_query = f'recording:"{title}"'
        else:
            mb_query = full_query

        params = {"query": mb_query, "fmt": "json", "limit": limit}
        headers = {"User-Agent": self.MB_USER_AGENT}
        try:
            # 严格遵守 MusicBrainz 限速策略 (1 req/s)
            async with self._mb_lock:
                now = time.monotonic()
                wait_time = 1.0 - (now - self._last_mb_request_time)
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                self._last_mb_request_time = time.monotonic()
                resp = await client.get(url, params=params, headers=headers)

            if resp.status_code != 200:
                return results
            recordings = resp.json().get("recordings", [])
            for rec in recordings:
                mbid = rec.get("id", "")
                r_title = rec.get("title", "")
                credits = rec.get("artist-credit", [])
                r_artist = ", ".join([c.get("name", "") for c in credits if isinstance(c, dict)])
                releases = rec.get("releases", [])
                album = ""
                year = ""
                date_str = ""
                track_no = None
                cover_url = ""

                if releases:
                    rel = releases[0]
                    album = rel.get("title", "")
                    date_str = rel.get("date", "")
                    year = date_str[:4] if date_str else ""
                    rel_id = rel.get("id", "")
                    if rel_id:
                        cover_url = f"https://coverartarchive.org/release/{rel_id}/front"
                    media = rel.get("media", [])
                    if media and media[0].get("track"):
                        raw_no = media[0]["track"][0].get("number")
                        if isinstance(raw_no, int):
                            track_no = raw_no
                        elif isinstance(raw_no, str) and raw_no.isdigit():
                            track_no = int(raw_no)

                cand = MusicCandidate(
                    source="musicbrainz",
                    source_id=mbid,
                    title=r_title,
                    artist=r_artist,
                    album=album,
                    track_number=track_no,
                    year=year,
                    date=date_str,
                    cover_url=cover_url,
                    duration_ms=rec.get("length"),
                    extra={"mbid": mbid}
                )
                results.append(cand)
        except Exception as e:
            logger.warning(f"[MusicBrainz] 请求异常: {e}")
        return results

    # -------------------------------------------------------------------------
    # 数据源 6: Lyrico 外部生态扩展插件
    # -------------------------------------------------------------------------
    async def scrape_lyrico_plugin(
        self,
        plugin_key: str,
        query: str,
        limit: int = 5
    ) -> List[MusicCandidate]:
        """通过 LyricoBridge 调用独立的 Lyrico-Plugins 扩展插件"""
        try:
            from lyrico_bridge import lyrico_bridge
            if not lyrico_bridge.is_available:
                return []

            loop = asyncio.get_running_loop()
            raw_songs = await loop.run_in_executor(
                None,
                lyrico_bridge.search_songs,
                plugin_key,
                query,
                1,
                limit
            )

            results: List[MusicCandidate] = []
            for s in raw_songs:
                fields = s.get("fields") or {}
                title = s.get("title") or fields.get("title") or ""
                artist = s.get("artist") or fields.get("artist") or ""
                album = s.get("album") or fields.get("album") or ""
                date_str = str(s.get("date") or fields.get("date") or "")
                year = date_str[:4] if date_str and len(date_str) >= 4 else ""
                cover_url = s.get("picUrl") or fields.get("cover_url") or ""
                track_no = None
                try:
                    tn = s.get("trackNumber") or fields.get("track_number")
                    if tn:
                        track_no = int(str(tn).split("/")[0])
                except Exception:
                    pass

                cand = MusicCandidate(
                    source=f"lyrico:{plugin_key}",
                    source_id=str(s.get("id") or ""),
                    title=title,
                    artist=artist,
                    album=album,
                    track_number=track_no,
                    year=year,
                    date=date_str,
                    cover_url=cover_url,
                    duration_ms=s.get("duration"),
                    extra={
                        "lyrico_plugin": plugin_key,
                        "raw_song": s
                    }
                )
                results.append(cand)

            # 针对支持歌词能力的插件（如 lrcshare、kugou），为第一首自动预取歌词
            p_info = lyrico_bridge.plugins.get(plugin_key, {})
            if results and "getLyrics" in p_info.get("capabilities", []):
                async def _fill_lrc(cand: MusicCandidate):
                    try:
                        lrc = await loop.run_in_executor(
                            None,
                            lyrico_bridge.get_lyrics,
                            plugin_key,
                            cand.extra.get("raw_song", {})
                        )
                        if lrc:
                            cand.lyric = lrc
                    except Exception as e:
                        logger.debug(f"Lyrico 获取歌词异常: {e}")

                await _fill_lrc(results[0])

            return results
        except Exception as e:
            logger.warning(f"[Lyrico:{plugin_key}] 请求异常: {e}")
            return []

    # -------------------------------------------------------------------------
    # 统一并发搜索入口
    # -------------------------------------------------------------------------
    async def search(
        self,
        title: str = "",
        artist: str = "",
        query: str = "",
        sources: Optional[List[str]] = None,
        limit_per_source: int = 5
    ) -> List[MusicCandidate]:
        """
        并发从多个源搜索音乐，聚合候选结果并按置信度从高到低排序
        """
        parts = [p.strip() for p in [title, artist] if p.strip()]
        full_query = " ".join(parts) if parts else query.strip()
        if not full_query:
            return []

        # 记录调用方是否显式指定了数据源（用于决定是否自动并入 Lyrico 插件）
        sources_explicit = sources is not None
        if sources is None:
            sources = ["itunes", "netease", "qqmusic", "kuwo", "musicbrainz"]

        logger.info(f"多源并发搜索: '{full_query}', 目标源: {sources}")

        all_candidates: List[MusicCandidate] = []
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            tasks = []
            if "itunes" in sources:
                tasks.append(self.scrape_itunes(client, full_query, limit_per_source))
            if "netease" in sources:
                tasks.append(self.scrape_netease(client, full_query, limit_per_source))
            if "qqmusic" in sources:
                tasks.append(self.scrape_qqmusic(client, full_query, limit_per_source))
            if "kuwo" in sources:
                tasks.append(self.scrape_kuwo(client, full_query, limit_per_source))
            if "musicbrainz" in sources:
                tasks.append(self.scrape_musicbrainz(client, title, artist, full_query, limit_per_source))

            # 并发调度已选或推荐的 Lyrico 插件
            try:
                from lyrico_bridge import lyrico_bridge
                if lyrico_bridge.is_available:
                    lyrico_keys = []
                    for s in sources:
                        if s.startswith("lyrico:"):
                            k = s.split(":", 1)[1]
                            if k in lyrico_bridge.plugins:
                                lyrico_keys.append(k)

                    # 仅当调用方未显式指定数据源时，才自动并入已启用的优质 Lyrico 插件，
                    # 避免显式 sources 被暗中扩增（如批量任务只想要内置源）
                    if not sources_explicit:
                        for k, p in lyrico_bridge.plugins.items():
                            if p.get("enabled", False) and k in ["apple", "lrcshare"]:
                                lyrico_keys.append(k)

                    for lk in set(lyrico_keys):
                        tasks.append(self.scrape_lyrico_plugin(lk, full_query, limit_per_source))
            except Exception as le:
                logger.debug(f"Lyrico 调度检查异常: {le}")

            results_list = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results_list:
                if isinstance(res, list):
                    all_candidates.extend(res)
                elif isinstance(res, Exception):
                    logger.error(f"源抓取异常: {res}")

        # 计算置信度综合得分
        match_title = title if title else full_query
        match_artist = artist if artist else ""
        for cand in all_candidates:
            cand.score = self.calculate_score(match_title, match_artist, cand.title, cand.artist, cand)

        # 按照综合得分倒序排序
        all_candidates.sort(key=lambda c: c.score, reverse=True)
        return all_candidates

    def search_sync(
        self,
        title: str = "",
        artist: str = "",
        query: str = "",
        sources: Optional[List[str]] = None,
        limit_per_source: int = 5
    ) -> List[MusicCandidate]:
        """同步接口包装"""
        return asyncio.run(self.search(title, artist, query, sources, limit_per_source))

    # =========================================================================
    # 整张专辑维度核心能力 (Album Scraping & Tracklist)
    # =========================================================================
    async def search_albums(
        self,
        query: str,
        sources: Optional[List[str]] = None,
        limit_per_source: int = 4
    ) -> List[Dict[str, Any]]:
        """多源并发搜索官方整张专辑"""
        q = (query or "").strip()
        if not q:
            return []

        enabled_sources = set(sources or ["netease", "qqmusic", "itunes"])
        all_albums: List[Dict[str, Any]] = []

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            tasks = []
            if "qqmusic" in enabled_sources:
                tasks.append(self._search_albums_qqmusic(client, q, limit_per_source))
            if "netease" in enabled_sources:
                tasks.append(self._search_albums_netease(client, q, limit_per_source))
            if "itunes" in enabled_sources:
                tasks.append(self._search_albums_itunes(client, q, limit_per_source))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, list):
                    all_albums.extend(res)
                elif isinstance(res, Exception):
                    logger.warning(f"[AlbumSearch] 异常: {res}")

        # 计算并按相似度粗排序
        q_norm = self.normalize_text(q).lower()
        def _score(alb: Dict[str, Any]) -> float:
            score = 0.0
            name_norm = self.normalize_text(alb.get("album_name", "")).lower()
            if q_norm in name_norm or name_norm in q_norm:
                score += 50.0
            if alb.get("cover_url"):
                score += 20.0
            if (alb.get("total_tracks") or 0) > 1:
                score += 15.0
            return score

        all_albums.sort(key=_score, reverse=True)
        return all_albums

    async def _search_albums_netease(self, client: httpx.AsyncClient, query: str, limit: int) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        url = "https://music.163.com/api/search/get/web"
        params = {"s": query, "type": 10, "limit": limit, "offset": 0, "csrf_token": ""}
        headers = {"User-Agent": self.DEFAULT_USER_AGENT, "Referer": "https://music.163.com"}
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                albums = data.get("result", {}).get("albums", [])
                for alb in albums:
                    pub = alb.get("publishTime")
                    year = ""
                    if pub and isinstance(pub, int) and pub > 0:
                        try:
                            year = str(datetime.datetime.fromtimestamp(pub / 1000.0, tz=CST_TZ).year)
                        except Exception:
                            pass
                    pic = alb.get("picUrl", "")
                    cover = f"{pic}?param=1400y1400" if pic else ""
                    results.append({
                        "source": "netease",
                        "album_id": str(alb.get("id")),
                        "album_name": alb.get("name", ""),
                        "artist_name": alb.get("artist", {}).get("name", "") or "",
                        "cover_url": cover,
                        "year": year,
                        "date": "",
                        "total_tracks": alb.get("size", 0),
                        "company": alb.get("company", ""),
                        "genre": ""
                    })
        except Exception as e:
            logger.warning(f"[Netease Albums] 搜索失败: {e}")
        return results

    async def _search_albums_qqmusic(self, client: httpx.AsyncClient, query: str, limit: int) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
        params = {"p": 1, "n": limit, "w": query, "t": 8, "format": "json"}
        headers = {"User-Agent": self.DEFAULT_USER_AGENT, "Referer": "https://y.qq.com"}
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                albums = data.get("data", {}).get("album", {}).get("list", [])
                for alb in albums:
                    amid = alb.get("albumMID", "")
                    cover = f"https://y.gtimg.cn/music/photo_new/T002R800x800M000{amid}.jpg" if amid else ""
                    pub = alb.get("publicTime", "")
                    results.append({
                        "source": "qqmusic",
                        "album_id": amid,
                        "album_name": alb.get("albumName", ""),
                        "artist_name": alb.get("singerName", "") or "",
                        "cover_url": cover,
                        "year": pub[:4] if pub else "",
                        "date": pub,
                        "total_tracks": alb.get("song_count", 0),
                        "company": "",
                        "genre": ""
                    })
        except Exception as e:
            logger.warning(f"[QQMusic Albums] 搜索失败: {e}")
        return results

    async def _search_albums_itunes(self, client: httpx.AsyncClient, query: str, limit: int) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        url = "https://itunes.apple.com/search"
        params = {"term": query, "entity": "album", "limit": limit}
        headers = {"User-Agent": self.DEFAULT_USER_AGENT}
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                for alb in data.get("results", []):
                    art = alb.get("artworkUrl100", "")
                    cover = re.sub(r"/\d+x\d+bb\.", "/3000x3000bb.", art) if art else ""
                    rel = alb.get("releaseDate", "")
                    results.append({
                        "source": "itunes",
                        "album_id": str(alb.get("collectionId")),
                        "album_name": alb.get("collectionName", ""),
                        "artist_name": alb.get("artistName", "") or "",
                        "cover_url": cover,
                        "year": rel[:4] if rel else "",
                        "date": rel[:10] if rel else "",
                        "total_tracks": alb.get("trackCount", 0),
                        "company": alb.get("copyright", ""),
                        "genre": alb.get("primaryGenreName", "")
                    })
        except Exception as e:
            logger.warning(f"[iTunes Albums] 搜索失败: {e}")
        return results

    async def get_album_details(self, source: str, album_id: str) -> Optional[Dict[str, Any]]:
        """获取官方整张专辑详细信息与全部曲目清单 (Tracklist)"""
        if not source or not album_id:
            return None

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            if source == "qqmusic":
                return await self._get_album_details_qqmusic(client, album_id)
            elif source == "netease":
                return await self._get_album_details_netease(client, album_id)
            elif source == "itunes":
                return await self._get_album_details_itunes(client, album_id)
        return None

    async def _get_album_details_qqmusic(self, client: httpx.AsyncClient, album_id: str) -> Optional[Dict[str, Any]]:
        url = f"https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg?albummid={album_id}&format=json"
        headers = {"User-Agent": self.DEFAULT_USER_AGENT, "Referer": "https://y.qq.com"}
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            data = resp.json()
            d = data.get("data") or {}
            raw_songs = d.get("list", [])
            tracks = []
            for idx, s in enumerate(raw_songs, 1):
                tracks.append({
                    "track_number": s.get("index_album") or idx,
                    "title": s.get("songname", ""),
                    "artist": ", ".join([sg.get("name", "") for sg in s.get("singer", []) if sg.get("name")]),
                    "duration_ms": (s.get("interval") or 0) * 1000,
                    "source_id": s.get("songmid", "")
                })

            cover = f"https://y.gtimg.cn/music/photo_new/T002R800x800M000{album_id}.jpg"
            date_str = d.get("aDate", "")
            return {
                "source": "qqmusic",
                "album_id": album_id,
                "album_name": d.get("name", ""),
                "artist_name": d.get("singername", ""),
                "cover_url": cover,
                "year": date_str[:4] if date_str else "",
                "date": date_str,
                "company": d.get("company", ""),
                "genre": d.get("genre", ""),
                "desc": d.get("desc", ""),
                "total_tracks": len(tracks),
                "tracks": tracks
            }
        except Exception as e:
            logger.warning(f"[QQMusic AlbumDetail] 获取失败: {e}")
        return None

    async def _get_album_details_netease(self, client: httpx.AsyncClient, album_id: str) -> Optional[Dict[str, Any]]:
        url = f"https://music.163.com/api/album/{album_id}"
        headers = {"User-Agent": self.DEFAULT_USER_AGENT, "Referer": "https://music.163.com"}
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            data = resp.json()
            alb = data.get("album") or {}
            raw_songs = data.get("songs") or alb.get("songs") or []
            tracks = []
            for idx, s in enumerate(raw_songs, 1):
                tracks.append({
                    "track_number": s.get("position") or s.get("no") or idx,
                    "title": s.get("name", ""),
                    "artist": ", ".join([a.get("name", "") for a in s.get("artists", []) if a.get("name")]),
                    "duration_ms": s.get("duration", 0),
                    "source_id": str(s.get("id", ""))
                })

            pub = alb.get("publishTime")
            year = ""
            if pub and isinstance(pub, int) and pub > 0:
                try:
                    year = str(datetime.datetime.fromtimestamp(pub / 1000.0, tz=CST_TZ).year)
                except Exception:
                    pass

            pic = alb.get("picUrl", "")
            cover = f"{pic}?param=1400y1400" if pic else ""
            return {
                "source": "netease",
                "album_id": album_id,
                "album_name": alb.get("name", ""),
                "artist_name": alb.get("artist", {}).get("name", "") or "",
                "cover_url": cover,
                "year": year,
                "date": "",
                "company": alb.get("company", ""),
                "genre": "",
                "desc": alb.get("description", ""),
                "total_tracks": len(tracks),
                "tracks": tracks
            }
        except Exception as e:
            logger.warning(f"[Netease AlbumDetail] 获取失败: {e}")
        return None

    async def _get_album_details_itunes(self, client: httpx.AsyncClient, album_id: str) -> Optional[Dict[str, Any]]:
        url = f"https://itunes.apple.com/lookup?id={album_id}&entity=song"
        headers = {"User-Agent": self.DEFAULT_USER_AGENT}
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            data = resp.json()
            results = data.get("results", [])
            col = next((it for it in results if it.get("wrapperType") == "collection"), {})
            song_items = [it for it in results if it.get("wrapperType") == "track"]

            tracks = []
            for idx, t in enumerate(song_items, 1):
                tracks.append({
                    "track_number": t.get("trackNumber") or idx,
                    "title": t.get("trackName", ""),
                    "artist": t.get("artistName", ""),
                    "duration_ms": t.get("trackTimeMillis", 0),
                    "source_id": str(t.get("trackId", ""))
                })

            art = col.get("artworkUrl100", "")
            cover = re.sub(r"/\d+x\d+bb\.", "/3000x3000bb.", art) if art else ""
            rel = col.get("releaseDate", "")
            return {
                "source": "itunes",
                "album_id": album_id,
                "album_name": col.get("collectionName", ""),
                "artist_name": col.get("artistName", ""),
                "cover_url": cover,
                "year": rel[:4] if rel else "",
                "date": rel[:10] if rel else "",
                "company": col.get("copyright", ""),
                "genre": col.get("primaryGenreName", ""),
                "desc": "",
                "total_tracks": len(tracks),
                "tracks": tracks
            }
        except Exception as e:
            logger.warning(f"[iTunes AlbumDetail] 获取失败: {e}")
        return None


# -----------------------------------------------------------------------------
# 命令行测试入口
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import json
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="飞牛 NAS 原生多源音乐刮削引擎原型测试")
    parser.add_argument("-t", "--title", default="", help="歌曲标题 (例如: 晴天)")
    parser.add_argument("-a", "--artist", default="", help="艺术家/歌手 (例如: 周杰伦)")
    parser.add_argument("-q", "--query", default="", help="综合搜索词 (例如: Hotel California Eagles)")
    parser.add_argument("-s", "--sources", nargs="+", default=None, help="启用源列表 (itunes, netease, qqmusic, kuwo, musicbrainz)")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出完整结果")
    args = parser.parse_args()

    s_title = args.title
    s_artist = args.artist
    s_query = args.query

    if not s_title and not s_query:
        s_title = "晴天"
        s_artist = "周杰伦"

    engine = MusicScraperEngine()
    print("=" * 72)
    print(f"🎵 正在并发搜索: 歌名='{s_title}', 歌手='{s_artist}', 综合词='{s_query}'")
    print("=" * 72)

    candidates = engine.search_sync(
        title=s_title,
        artist=s_artist,
        query=s_query,
        sources=args.sources
    )

    if args.json:
        print(json.dumps([c.to_dict() for c in candidates], ensure_ascii=False, indent=2))
    else:
        print(f"✅ 成功聚合 {len(candidates)} 个候选结果 (按置信度降序排列):\n")
        for i, c in enumerate(candidates, 1):
            lyric_stat = "✅有" if c.lyric else "❌无"
            cover_stat = "✅有" if c.cover_url else "❌无"
            print(f"[{i:02d}] 【{c.source.upper():<12}】 得分: {c.score:5.1f} | 《{c.title}》 - {c.artist}")
            print(f"     专辑: {c.album or '未知'} | 年份: {c.year or '未知'} | 音轨: {c.track_number or '未知'}")
            print(f"     封面: [{cover_stat}] {c.cover_url[:65] + '...' if len(c.cover_url) > 65 else c.cover_url}")
            if c.lyric:
                first_line = [line for line in c.lyric.splitlines() if line.strip()][0]
                print(f"     歌词: [{lyric_stat}] (首行: {first_line})")
            else:
                print(f"     歌词: [{lyric_stat}]")
            print("-" * 72)
