"""
安全与健壮性回归测试（对应 2026-10 代码审查报告中的修复项）
==============================================================
覆盖：
1. detect_language 不再把国语代表作误判为粤语；
2. _same_origin 在反向代理（X-Forwarded-Host）下放行同源请求，且不能被伪造头绕过；
3. safe_fetch_cover 逐跳校验重定向、拒绝内网目标、限制重定向次数与体积；
4. build_roots 不再把整个盘符 / /mnt 当作授权根目录；
5. 仅大小写改名在 WinError 183 场景下通过中转名完成；
6. 曲库缓存写入防抖（窗口内不重复落盘，flush 强制落盘）；
7. MusicCandidate 对松散 track_number 的类型收敛。
"""

from __future__ import annotations

import http.server
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("MUSICTAG_STATE_DIR", tempfile.mkdtemp(prefix="musictag_regress_"))

import security  # noqa: E402
import tag_engine  # noqa: E402
from scraper_engine import MusicCandidate  # noqa: E402
from tag_engine import AudioMetadata, AudioTagEngine, detect_language  # noqa: E402


class _FakeOrigin:
    """构造最小 Request 替身，避免为单测启动完整 ASGI 应用。"""

    def __init__(self, headers: dict):
        self.headers = headers


def _same_origin(headers: dict) -> bool:
    from server import _same_origin as fn

    class _Req:
        pass

    req = _Req()
    req.headers = headers
    return fn(req)


class TestLanguageDetection(unittest.TestCase):
    def test_mandarin_classics_not_cantonese(self):
        # 这些歌手的国语代表作曾因硬编码名单被误判为粤语
        cases = [
            ("红豆", "王菲"),
            ("十年", "陈奕迅"),
            ("吻别", "张学友"),
            ("晴天 Jay Chou", "周杰伦"),
        ]
        for title, artist in cases:
            with self.subTest(title=title, artist=artist):
                self.assertEqual(detect_language(title, artist), "华语")

    def test_explicit_cantonese_marker_still_wins(self):
        self.assertEqual(detect_language("一生所爱 粤语", "卢冠廷"), "粤语")
        self.assertEqual(detect_language("海阔天空", "Beyond", genre="Cantonese"), "粤语")

    def test_other_languages_unaffected(self):
        self.assertEqual(detect_language("First Love", "宇多田ヒカル"), "日语")
        self.assertEqual(detect_language("노래", "방탄소년단"), "韩语")
        self.assertEqual(detect_language("Hello", "Adele"), "欧美")


class TestSameOrigin(unittest.TestCase):
    def test_no_origin_allows(self):
        self.assertTrue(_same_origin({"host": "nas.local:8002"}))

    def test_direct_same_origin(self):
        self.assertTrue(
            _same_origin({"host": "nas.local:8002", "origin": "http://nas.local:8002"})
        )

    def test_cross_site_rejected(self):
        self.assertFalse(
            _same_origin({"host": "nas.local:8002", "origin": "http://evil.example"})
        )

    def test_reverse_proxy_forwarded_host_allowed(self):
        # 反代回源 Host=127.0.0.1，浏览器 Origin 是公网域名
        self.assertTrue(
            _same_origin(
                {
                    "host": "127.0.0.1:8002",
                    "origin": "https://nas.example.com",
                    "x-forwarded-host": "nas.example.com",
                }
            )
        )

    def test_forwarded_host_list_takes_first(self):
        self.assertTrue(
            _same_origin(
                {
                    "host": "127.0.0.1:8002",
                    "origin": "https://nas.example.com",
                    "x-forwarded-host": "nas.example.com, inner.proxy",
                }
            )
        )

    def test_forged_forwarded_host_not_trusted_on_direct_access(self):
        # 直连公网 IP 时不应采信伪造的 X-Forwarded-Host
        self.assertFalse(
            _same_origin(
                {
                    "host": "1.2.3.4:8002",
                    "origin": "http://evil.example",
                    "x-forwarded-host": "evil.example",
                }
            )
        )


class TestSafeFetchCover(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        png = (
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
        )

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/img":
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(png)))
                    self.end_headers()
                    self.wfile.write(png)
                elif self.path == "/to-metadata":
                    self.send_response(302)
                    self.send_header(
                        "Location", "http://169.254.169.254/latest/meta-data/"
                    )
                    self.end_headers()
                elif self.path == "/loop":
                    self.send_response(302)
                    self.send_header("Location", "/loop")
                    self.end_headers()
                elif self.path == "/huge":
                    self.send_response(200)
                    self.send_header("Content-Length", "999999999")
                    self.end_headers()
                    self.wfile.write(b"\x00" * (2 * 1024 * 1024))
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass

        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def test_private_ip_rejected_by_default(self):
        with self.assertRaises(ValueError):
            security.safe_fetch_cover(self._url("/img"), 1024 * 1024)

    def test_normal_image_ok_with_allow_private(self):
        data, final = security.safe_fetch_cover(
            self._url("/img"), 1024 * 1024, allow_private=True
        )
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertTrue(final.endswith("/img"))

    def test_redirect_to_internal_revalidated(self):
        with self.assertRaises(ValueError):
            security.safe_fetch_cover(
                self._url("/to-metadata"), 1024 * 1024, allow_private=True
            )

    def test_redirect_loop_capped(self):
        with self.assertRaises(ValueError) as ctx:
            security.safe_fetch_cover(
                self._url("/loop"), 1024 * 1024, allow_private=True
            )
        self.assertIn("重定向", str(ctx.exception))

    def test_oversize_rejected(self):
        with self.assertRaises(ValueError):
            security.safe_fetch_cover(
                self._url("/huge"), 64 * 1024, allow_private=True
            )

    def test_bad_scheme_rejected(self):
        for bad in ("file:///etc/passwd", "ftp://x/y", "http://127.0.0.1:1/x"):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    security.safe_fetch_cover(bad, 1024)


class TestBuildRoots(unittest.TestCase):
    def test_drive_letters_not_whitelisted_by_default(self):
        roots = [r.lower() for r in security.build_roots("")]
        if os.name == "nt":
            self.assertFalse(
                any(len(r) == 3 and r.endswith(":\\") for r in roots),
                f"盘符不应被当作授权根目录: {roots}",
            )
            self.assertFalse(
                security.is_allowed_path(r"C:\Windows\System32", security.build_roots(""))
            )
        else:
            for bad in ("/mnt", "/media", "/vol1"):
                self.assertNotIn(bad, roots)

    def test_explicit_roots_still_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = security.build_roots(tmp)
            self.assertTrue(security.is_allowed_path(tmp, roots))

    def test_extra_roots_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            # _split_paths 按 ":" 分隔（与 fnOS 环境变量格式一致），
            # 故用不含冒号的独立子目录验证追加逻辑
            sub = os.path.join(tmp, "extra_music_root")
            os.makedirs(sub, exist_ok=True)
            with mock.patch.dict(os.environ, {"MUSICTAG_EXTRA_ROOTS": sub}):
                roots = security.build_roots("")
                self.assertTrue(
                    security.is_allowed_path(sub, roots), f"未包含显式根目录: {roots}"
                )


class TestCaseOnlyRename(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="musictag_rename_")
        self.engine = AudioTagEngine(
            cache_file=os.path.join(self.tmp, "cache.json"), state_dir=self.tmp
        )
        self.path = os.path.join(self.tmp, "song.mp3")
        with open(self.path, "wb") as fh:
            fh.write(b"\x00" * 64)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fallback_through_temp_name(self):
        """首次 os.rename 抛 WinError 183 时，应经临时名完成改名且不残留临时文件。"""
        real_rename = os.rename
        calls = []

        def flaky(src, dst):
            calls.append((os.path.basename(src), os.path.basename(dst)))
            if len(calls) == 1:
                raise OSError(183, "File exists")
            return real_rename(src, dst)

        with mock.patch.object(tag_engine.os, "rename", side_effect=flaky):
            ok, new_path, msg = self.engine.rename_file(
                self.path, "Song.mp3", sync_lrc=False
            )

        self.assertTrue(ok, msg)
        self.assertEqual(os.path.basename(new_path), "Song.mp3")
        self.assertEqual(len(calls), 3, calls)
        leftovers = [n for n in os.listdir(self.tmp) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [], f"残留临时文件: {leftovers}")

    def test_plain_rename_unchanged(self):
        ok, new_path, _ = self.engine.rename_file(self.path, "other.mp3", sync_lrc=False)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(new_path))


class TestCacheDebounce(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="musictag_cache_")
        self.cache_file = os.path.join(self.tmp, "cache.json")
        self.engine = AudioTagEngine(cache_file=self.cache_file, state_dir=self.tmp)
        self.audio = os.path.join(self.tmp, "a.mp3")
        with open(self.audio, "wb") as fh:
            fh.write(b"\x00" * 32)
        self.meta = AudioMetadata(
            filepath=self.audio, filename="a.mp3", ext=".mp3", title="T", artist="A"
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_repeated_updates_write_once_per_window(self):
        writes = []
        real_writer = security.atomic_write_json

        def counting(path, data, mode=0o600):
            writes.append(path)
            return real_writer(path, data, mode=mode)

        with mock.patch.object(security, "atomic_write_json", side_effect=counting):
            for _ in range(15):
                self.engine.update_cache_item(self.audio, self.meta)

        self.assertLessEqual(len(writes), 1, f"窗口内不应重复落盘: {len(writes)} 次")

    def test_flush_persists_dirty_state(self):
        self.engine.update_cache_item(self.audio, self.meta)
        if os.path.exists(self.cache_file):
            os.remove(self.cache_file)
        self.engine.update_cache_item(self.audio, self.meta)  # 窗口内，不落盘
        self.assertFalse(os.path.exists(self.cache_file))
        self.engine.flush_cache()
        self.assertTrue(os.path.exists(self.cache_file))

    def test_window_expiry_triggers_write(self):
        self.engine.update_cache_item(self.audio, self.meta)
        os.remove(self.cache_file)
        self.engine._cache_last_save = time.monotonic() - tag_engine.CACHE_SAVE_INTERVAL - 1
        self.engine.update_cache_item(self.audio, self.meta)
        self.assertTrue(os.path.exists(self.cache_file))


class TestTrackNumberCoercion(unittest.TestCase):
    def _cand(self, **kw):
        return MusicCandidate(source="t", source_id="1", title="x", artist="y", **kw)

    def test_loose_values(self):
        self.assertEqual(self._cand(track_number="3/12").track_number, 3)
        self.assertEqual(self._cand(track_number="3/12").total_tracks, 12)
        self.assertEqual(self._cand(track_number="03").track_number, 3)
        self.assertEqual(self._cand(track_number=5.0).track_number, 5)
        self.assertIsNone(self._cand(track_number="abc").track_number)
        self.assertIsNone(self._cand(track_number="").track_number)
        self.assertIsNone(self._cand(track_number=None).track_number)

    def test_explicit_total_wins(self):
        c = self._cand(track_number="4/9", total_tracks="9")
        self.assertEqual((c.track_number, c.total_tracks), (4, 9))

    def test_downstream_int_cast_safe(self):
        # 模拟 tag_engine 写 MP4 trkn 的用法，不应抛异常
        c = self._cand(track_number="7/10")
        self.assertEqual((int(c.track_number), int(c.total_tracks or 0)), (7, 10))


if __name__ == "__main__":
    unittest.main()
