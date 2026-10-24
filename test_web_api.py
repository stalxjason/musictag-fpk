"""
飞牛 NAS 原生音乐刮削服务 - Web API 自动化与安全测试套件
=========================================================
测试范围：
1. 访问令牌鉴权守卫（未带令牌拒绝 401，携带合法令牌正常放行）；
2. 跨站请求防护与安全响应头注入；
3. 目录与音频路径白名单越权检测；
4. 配置读取与严格校验修改（非法端口与未知字段拦截）；
5. 批量刮削并发互斥保护。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

# 设置隔离测试状态目录
os.environ["MUSICTAG_STATE_DIR"] = tempfile.mkdtemp(prefix="musictag_api_test_")

from fastapi.testclient import TestClient
import app_paths
import security
from server import app, load_config, state


class TestWebAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.token = security.load_or_create_token(app_paths.TOKEN_FILE)
        cls.headers = {"X-App-Token": cls.token}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(app_paths.STATE_DIR, ignore_errors=True)

    def test_01_public_routes_and_security_headers(self):
        """测试公开路由与基础安全响应头"""
        # 1. 首页下发令牌并注入 Cookie
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("window.__APP_TOKEN__", resp.text)
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")

        # 2. 健康检查
        resp_health = self.client.get("/api/health")
        self.assertEqual(resp_health.status_code, 200)
        self.assertEqual(resp_health.json().get("status"), "ok")

    def test_02_auth_guard(self):
        """测试受保护 API 的未授权拦截与合法访问"""
        # 清除 test_01 中由 GET / 设置的 Cookie
        fresh_client = TestClient(app)

        # 未提供令牌拒绝 401
        unauth = fresh_client.get("/api/config")
        self.assertEqual(unauth.status_code, 401)

        # 携带非法令牌拒绝 401
        bad_token = fresh_client.get("/api/config", headers={"X-App-Token": "invalid_token_12345"})
        self.assertEqual(bad_token.status_code, 401)

        # 携带合法令牌正常放行 200
        auth = fresh_client.get("/api/config", headers=self.headers)
        self.assertEqual(auth.status_code, 200)
        self.assertIn("config", auth.json())

    def test_03_config_validation(self):
        """测试配置安全校验：拦截非法端口及未知键污染"""
        # 非法端口
        resp_bad_port = self.client.post(
            "/api/config",
            headers=self.headers,
            json={"port": "invalid_number"}
        )
        self.assertEqual(resp_bad_port.status_code, 422)

        # 未知字段 (extra="forbid") 拦截
        resp_unknown_field = self.client.post(
            "/api/config",
            headers=self.headers,
            json={"unknown_injected_key": "malicious_value"}
        )
        self.assertEqual(resp_unknown_field.status_code, 422)

    def test_04_fs_browse_boundary(self):
        """测试目录浏览边界：只能浏览授权根目录"""
        # 空路径列出授权根目录
        resp_roots = self.client.get("/api/fs/browse", headers=self.headers)
        self.assertEqual(resp_roots.status_code, 200)
        data = resp_roots.json()
        self.assertTrue(len(data.get("items", [])) > 0)

    def _plugin_zip(self, manifest: dict, script: str = "function searchSongs(r){return []}"):
        import io
        import json as _json
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", _json.dumps(manifest))
            zf.writestr("source.js", script)
        return buf.getvalue()

    def test_05_plugin_upload_safety(self):
        """测试插件上传安全：路径穿越、超限、冒用内置插件、重复安装"""
        import io
        import zipfile

        ct = {**self.headers, "Content-Type": "application/octet-stream"}

        # 1. 缺少 manifest
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "x")
        resp = self.client.post(
            "/api/plugins/lyrico/upload", headers=ct, content=buf.getvalue()
        )
        self.assertEqual(resp.status_code, 400)

        # 2. 路径穿越
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../evil.txt", "bad")
            zf.writestr("manifest.json", '{"id":"com.test.evil"}')
        resp = self.client.post(
            "/api/plugins/lyrico/upload", headers=ct, content=buf.getvalue()
        )
        self.assertEqual(resp.status_code, 400)

        # 3. 冒用内置插件 id（必须 403）
        hijack = self._plugin_zip(
            {"id": "com.applemusic.source", "name": "Hijack", "entry": "source.js"}
        )
        resp = self.client.post("/api/plugins/lyrico/upload", headers=ct, content=hijack)
        self.assertEqual(resp.status_code, 403, "内置插件身份必须被拒绝覆盖")

        # 4. 冒用内置插件目录名
        hijack2 = self._plugin_zip(
            {"id": "apple", "name": "Hijack2", "entry": "source.js"}
        )
        resp = self.client.post("/api/plugins/lyrico/upload", headers=ct, content=hijack2)
        self.assertEqual(resp.status_code, 403)

        # 5. 正常第三方插件可安装，且同名重复安装被拒
        good = self._plugin_zip(
            {"id": "com.example.testplugin.source", "name": "UT", "entry": "source.js"}
        )
        resp = self.client.post("/api/plugins/lyrico/upload", headers=ct, content=good)
        self.assertEqual(resp.status_code, 200, resp.text)
        key = resp.json()["key"]
        self.assertEqual(key, "testplugin")
        resp_dup = self.client.post("/api/plugins/lyrico/upload", headers=ct, content=good)
        self.assertEqual(resp_dup.status_code, 400)

        # 6. 删除内置插件被拒，删除第三方插件成功
        resp = self.client.post(
            "/api/plugins/lyrico/delete", headers=self.headers, json={"key": "apple"}
        )
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(
            "/api/plugins/lyrico/delete", headers=self.headers, json={"key": key}
        )
        self.assertEqual(resp.status_code, 200)

    def test_06_concurrent_batch_start_mutex(self):
        """测试批量任务启动互斥（并发只允许一个任务启动）"""
        import asyncio

        from server import BatchScrapeRequest, run_batch_scrape_task

        async def _run():
            results = []

            async def try_start():
                async with state.batch_lock:
                    if state.batch_running:
                        results.append(409)
                        return
                    state.batch_running = True
                    state.batch_task = asyncio.create_task(
                        run_batch_scrape_task(BatchScrapeRequest(dry_run=True))
                    )
                    results.append(200)

            await asyncio.gather(*[try_start() for _ in range(5)])
            if state.batch_task:
                try:
                    await asyncio.wait_for(state.batch_task, timeout=10)
                except Exception:
                    pass
            return results

        results = asyncio.run(_run())
        self.assertEqual(results.count(200), 1, "并发起点必须只有一个成功")
        self.assertFalse(state.batch_running)

    def test_07_runtime_logs_api(self):
        """测试运行日志接口：级别筛选、堆栈续行、关键字、前端回写、下载白名单"""
        from server import logger

        logger.warning("日志测试 WARNING_MARKER 磁盘空间不足")
        try:
            raise ValueError("日志测试 EXCEPTION_MARKER")
        except ValueError:
            logger.exception("日志测试 触发异常 EXCEPTION_MARKER")

        # 1. 基本读取：日志确实落盘并可被接口读到
        resp = self.client.get("/api/logs", headers=self.headers, params={"lines": 200})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["items"], "日志文件应有内容（日志必须落盘才可在前台排查）")
        self.assertEqual(data["level"], "ALL")
        messages = [item["message"] for item in data["items"]]
        self.assertTrue(
            any("WARNING_MARKER" in m or "EXCEPTION_MARKER" in m for m in messages),
            "刚写入的日志必须能在接口返回中看到（说明读的是最新内容）",
        )
        # 校验时间戳顺序（时间正序）
        times = [item["time"] for item in data["items"] if item["time"]]
        self.assertEqual(times, sorted(times))

        # 2. 级别筛选：ERROR 级别结果里不应有 INFO/WARNING（堆栈续行 level 为空，允许保留）
        resp_err = self.client.get("/api/logs", headers=self.headers, params={"level": "ERROR"})
        self.assertEqual(resp_err.status_code, 200)
        error_items = resp_err.json()["items"]
        for item in error_items:
            self.assertIn(item["level"], ("ERROR", "CRITICAL", ""))
        self.assertTrue(any(i["level"] == "ERROR" for i in error_items))

        # 3. 关键字命中时保留其后的异常堆栈续行
        #    （仅程序写入磁盘的日志带 "[LEVEL]" 前缀，TestClient 的访问日志不会被匹配）
        resp_kw = self.client.get(
            "/api/logs",
            headers=self.headers,
            params={"keyword": "日志测试 触发异常", "level": "ERROR"},
        )
        items = resp_kw.json()["items"]
        self.assertTrue(items)
        self.assertFalse(items[0]["continuation"])
        self.assertTrue(any(it["continuation"] for it in items), "异常堆栈续行应一并返回")
        self.assertTrue(any("EXCEPTION_MARKER" in it["message"] for it in items if it["continuation"]))

        # 4. 前端日志回写
        resp = self.client.post(
            "/api/logs/client",
            headers=self.headers,
            json={"level": "error", "message": "前端报错 CLIENT_MARKER", "source": "saveTagEdit"},
        )
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/api/logs", headers=self.headers, params={"keyword": "CLIENT_MARKER"})
        echoes = resp.json()["items"]
        self.assertTrue(any("[WebUI]" in it["message"] and "saveTagEdit" in it["message"] for it in echoes))

        # 5. 前端日志非法输入拦截（超长 / 非法级别 / 多余字段）
        self.assertEqual(
            self.client.post(
                "/api/logs/client", headers=self.headers, json={"level": "error", "message": "x" * 5000}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/api/logs/client", headers=self.headers, json={"level": "fatal", "message": "x"}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/api/logs/client",
                headers=self.headers,
                json={"level": "error", "message": "x", "injected": 1},
            ).status_code,
            422,
        )

        # 6. 下载：当前文件可下载，任意文件读取 / 路径穿越被拒绝
        resp = self.client.get("/api/logs/download", headers=self.headers, params={"file": "current"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers.get("content-disposition", ""))
        for bad in ("../auth_token", "/etc/passwd", "config.json", "app.log/../../auth_token"):
            resp = self.client.get("/api/logs/download", headers=self.headers, params={"file": bad})
            self.assertIn(resp.status_code, (400, 404), f"非法日志文件名必须被拒绝: {bad}")

        # 7. 参数校验
        self.assertEqual(
            self.client.get("/api/logs", headers=self.headers, params={"lines": 0}).status_code, 422
        )
        self.assertEqual(
            self.client.get("/api/logs", headers=self.headers, params={"file": "other"}).status_code, 400
        )

        # 8. 未带令牌拒绝访问
        self.assertEqual(TestClient(app).get("/api/logs").status_code, 401)

    def test_08_log_tail_rotation(self):
        """测试日志轮转：含历史备份、truncated 标记、只读当前文件模式（mtime 决定新旧）"""
        import os
        import time

        import app_paths

        backup = app_paths.LOG_FILE + ".1"
        try:
            with open(backup, "w", encoding="utf-8") as fh:
                fh.write("2024-01-01 00:00:00 [INFO] 历史轮转备份 ROTATED_MARKER\n")
            # 轮转备份的 mtime 一定早于正在写入的 app.log
            old = time.time() - 3600
            os.utime(backup, (old, old))

            resp = self.client.get(
                "/api/logs", headers=self.headers, params={"file": "all", "keyword": "ROTATED_MARKER"}
            )
            payload = resp.json()
            self.assertTrue(
                [i for i in payload["items"] if "ROTATED_MARKER" in i["message"]],
                "file=all 时应读入轮转备份 app.log.1",
            )
            # 当前文件必须排在最前（mtime 决定新旧，而不是文件名里的轮转序号）
            self.assertEqual([f["name"] for f in payload["files"]][0], "app.log")

            # 只读当前文件时不应看到备份内容
            resp = self.client.get(
                "/api/logs", headers=self.headers, params={"file": "current"}
            )
            self.assertFalse(
                [i for i in resp.json()["items"] if i["message"] == "历史轮转备份 ROTATED_MARKER"],
                "file=current 时不应读入轮转备份 app.log.1",
            )

            # 行数上限：应标记 truncated，且返回的是最新的记录
            from server import logger as server_logger

            server_logger.info("最新一条日志 TAIL_MARKER")
            resp = self.client.get("/api/logs", headers=self.headers, params={"lines": 1, "file": "all"})
            self.assertEqual(resp.json()["total_returned"], 1)
            self.assertTrue(resp.json()["truncated"])
            self.assertIn("TAIL_MARKER", resp.json()["items"][-1]["message"])
        finally:
            if os.path.exists(backup):
                os.remove(backup)

    def _make_test_audio(self, name: str = "song.mp3", size: int = 512 * 1024) -> str:
        """在已授权的状态目录内创建测试音频文件（音乐根白名单包含 STATE_DIR）。

        MP3 需要用最小合法 MPEG-1 Layer3 帧填充，否则 mutagen 无法写入内嵌歌词。
        """
        media_dir = os.path.join(app_paths.STATE_DIR, "media")
        os.makedirs(media_dir, exist_ok=True)
        path = os.path.join(media_dir, name)
        frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
        reps = max(1, size // len(frame))
        with open(path, "wb") as fh:
            fh.write(frame * reps)
        return path

    def test_09_lyric_save_keeps_other_tags(self):
        """保存歌词（含写入内嵌）不得破坏标题/歌手/专辑/音轨/年份/流派/封面等既有标签"""
        from server import tag_engine
        from scraper_engine import MusicCandidate

        path = self._make_test_audio("preserve.mp3", 64 * 1024)
        tiny_png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c63000100000500010d0a2d0b0000000049454e44ae426082"
        )
        cand = MusicCandidate(
            source="manual", source_id="", title="原始歌名", artist="原始歌手",
            album="原始专辑", album_artist="原始专辑歌手", track_number=7,
            total_tracks=12, year="2020", genre="Pop", lyric="",
        )
        self.assertTrue(tag_engine.write_tags(path, cand, embed_cover=True, save_lrc=False, cover_bytes=tiny_png))
        keys = ("title", "artist", "album", "album_artist", "track_number", "total_tracks",
                "year", "genre", "has_cover")
        before = tag_engine.read_metadata(path).to_dict()

        resp = self.client.post(
            "/api/music/lyric/save",
            headers=self.headers,
            json={"filepath": path, "lines": [{"time": 1, "text": "第一句"}, {"time": 2, "text": "第二句"}]},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["written_embedded"])

        after = tag_engine.read_metadata(path).to_dict()
        for key in keys:
            self.assertEqual(before[key], after[key], f"保存歌词后字段被改动: {key}")
        self.assertIn("[00:01.00]第一句", tag_engine.read_lyric_text(path))

    def test_10_audio_stream_range(self):
        """测试网页播放接口：完整读取、Range 分段、越界、路径白名单"""
        import urllib.parse

        path = self._make_test_audio("stream.mp3", 300 * 1024)
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertGreater(len(data), 300 * 1024 - 500)
        url = "/api/music/stream?filepath=" + urllib.parse.quote(path)

        # 1. 完整请求
        resp = self.client.get(url, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("accept-ranges"), "bytes")
        self.assertEqual(resp.headers.get("content-type"), "audio/mpeg")
        self.assertEqual(len(resp.content), len(data))

        # 2. Range: 前 1KB
        resp = self.client.get(url, headers={**self.headers, "Range": "bytes=0-1023"})
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp.headers.get("content-range"), f"bytes 0-1023/{len(data)}")
        self.assertEqual(resp.content, data[:1024])

        # 3. Range: 开放结尾 / 后缀区间
        resp = self.client.get(url, headers={**self.headers, "Range": "bytes=1000-"})
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp.content, data[1000:])
        resp = self.client.get(url, headers={**self.headers, "Range": "bytes=-256"})
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp.content, data[-256:])

        # 4. 越界 Range -> 416
        resp = self.client.get(url, headers={**self.headers, "Range": "bytes=999999999-"})
        self.assertEqual(resp.status_code, 416)
        self.assertEqual(resp.headers.get("content-range"), f"bytes */{len(data)}")

        # 5. 路径白名单 / 未授权
        bad = "/api/music/stream?filepath=" + urllib.parse.quote(os.path.abspath(__file__))
        self.assertEqual(self.client.get(bad, headers=self.headers).status_code, 403)
        self.assertEqual(TestClient(app).get(url).status_code, 401)

    def test_11_lrc_parse_and_build(self):
        """测试 LRC 解析与生成：多种时间格式、多标签同行、元信息、偏移"""
        from server import build_lrc_text, parse_lrc_text

        # 1. [mm:ss.xx] + 多标签同一行
        fmt, lines, meta, offset = parse_lrc_text("[00:12.34]第一句\n[00:15.00][00:30.00]重复句")
        self.assertEqual(fmt, "lrc")
        self.assertEqual([l["time"] for l in lines], [12.34, 15.0, 30.0])
        self.assertTrue(all(l["text"] == "重复句" for l in lines[1:]))

        # 2. 分钟整秒 / 冒号百分秒 / 三段式带小数
        fmt, lines, _, _ = parse_lrc_text("[00:05]整秒\n[00:05:12]冒号百分秒\n[00:05:12.50]三段带小数")
        self.assertEqual([round(l["time"], 2) for l in lines], [5.0, 5.12, 312.5])

        # 2.1 长分钟仍按 mm:ss:xx 解释；三段式带小数才按 时:分:秒
        fmt, lines, _, _ = parse_lrc_text("[70:05:12]长分")
        self.assertEqual(lines[0]["time"], 70 * 60 + 5 + 0.12)
        fmt, lines, _, _ = parse_lrc_text("[24:05:12.700]时")
        self.assertEqual(lines[0]["time"], round(24 * 3600 + 5 * 60 + 12.7, 2))
        # 一位小数按「百分秒的十分位」解释（.7 即 0.07 秒）
        fmt, lines, _, _ = parse_lrc_text("[24:05:12.7]时")
        self.assertEqual(lines[0]["time"], round(24 * 3600 + 5 * 60 + 12.07, 2))
        # 2.2 元信息不会被当成歌词行，也不影响时间标签解析
        fmt, lines, meta, _ = parse_lrc_text("[ti:歌名]\n[by:某人]\n[00:03.00]歌词")
        self.assertEqual(meta, ["[ti:歌名]", "[by:某人]"])
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["time"], 3.0)
        self.assertEqual(lines[0]["text"], "歌词")

        # 3. 元信息与 offset
        fmt, lines, meta, offset = parse_lrc_text("[ti:歌名]\n[ar:歌手]\n[offset:250]\n[00:01.00]x")
        self.assertEqual(meta, ["[ti:歌名]", "[ar:歌手]", "[offset:250]"])
        self.assertEqual(offset, 250)

        # 4. 纯文本 -> plain，无时间
        fmt, lines, _, _ = parse_lrc_text("第一句\n\n第二句")
        self.assertEqual(fmt, "plain")
        self.assertTrue(all(l["time"] is None for l in lines))

        # 5. 生成：排序、合并同时间、未打标保留
        text, untimed = build_lrc_text(
            [{"time": 9.0, "text": "第三"}, {"time": 1.0, "text": "第一"},
             {"time": 9.0, "text": "第三-同时"}, {"time": None, "text": "未打标"}],
            ["[ti:测试]"],
        )
        self.assertEqual(untimed, ["未打标"])
        self.assertEqual(
            text, "[ti:测试]\n[00:01.00]第一\n[00:09.00]第三\n[00:09.00]第三-同时\n未打标\n"
        )

        # 6. offset 只写一次
        text, _ = build_lrc_text([{"time": 1.0, "text": "x"}], ["[offset:100]"], 200)
        self.assertEqual(text.count("[offset:"), 1)
        text, _ = build_lrc_text([{"time": 1.0, "text": "x"}], [], -300)
        self.assertIn("[offset:-300]", text)

        # 7. 中文往返一致
        original = "[ti:歌名]\n[00:01.50]第一句\n[00:05.25]第二句\n"
        _, parsed, meta_lines, off = parse_lrc_text(original)
        rebuilt, _ = build_lrc_text(parsed, meta_lines, off)
        self.assertEqual(rebuilt, original)

    def test_12_lyric_manual_tagging(self):
        """测试歌词手动打标：读取、保存外置/内嵌、编辑重存、非法输入"""
        import urllib.parse

        path = self._make_test_audio("lyrics.mp3", 64 * 1024)
        lrc_path = os.path.splitext(path)[0] + ".lrc"
        q = urllib.parse.quote(path)

        # 1. 初始无歌词
        data = self.client.get("/api/music/lyric/lyrics?filepath=" + q, headers=self.headers).json()
        self.assertEqual(data["source"], "none")
        self.assertEqual(data["format"], "plain")

        # 2. 打标保存（外置 + 内嵌）
        payload = {
            "filepath": path,
            "lines": [
                {"time": 1.5, "text": "第一句"},
                {"time": 5.0, "text": "第二句"},
                {"time": None, "text": "还没打标的一句"},
            ],
            "meta_lines": ["[ti:测试歌]", "[ar:测试歌手]"],
        }
        resp = self.client.post("/api/music/lyric/save", headers=self.headers, json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        result = resp.json()
        self.assertTrue(result["written_sidecar"])
        self.assertEqual(result["timed_lines"], 2)
        self.assertEqual(result["untimed_lines"], 1)

        with open(lrc_path, "r", encoding="utf-8") as fh:
            saved = fh.read()
        self.assertIn("[00:01.50]第一句", saved)
        self.assertIn("[00:05.00]第二句", saved)
        self.assertIn("[ti:测试歌]", saved)
        self.assertIn("还没打标的一句", saved)

        # 3. 重新读取：来源为外置、格式为 lrc、行可继续编辑
        data = self.client.get("/api/music/lyric/lyrics?filepath=" + q, headers=self.headers).json()
        self.assertEqual(data["source"], "sidecar")
        self.assertEqual(data["format"], "lrc")
        self.assertEqual(len(data["lines"]), 3)
        self.assertEqual(data["lines"][0]["time"], 1.5)

        # 4. 编辑时间后重存（模拟拖动校对）
        edited = [dict(l) for l in data["lines"]]
        edited[0]["time"] = 2.25
        resp = self.client.post(
            "/api/music/lyric/save",
            headers=self.headers,
            json={"filepath": path, "lines": edited, "meta_lines": data["meta_lines"], "offset_ms": -200},
        )
        self.assertEqual(resp.status_code, 200)
        with open(lrc_path, "r", encoding="utf-8") as fh:
            self.assertIn("[00:02.25]第一句", fh.read())

        # 5. 只写外置 / 只写内嵌
        resp = self.client.post(
            "/api/music/lyric/save",
            headers=self.headers,
            json={"filepath": path, "lines": edited, "write_embedded": False},
        )
        self.assertTrue(resp.json()["written_sidecar"])
        self.assertFalse(resp.json()["written_embedded"])

        os.remove(lrc_path)
        resp = self.client.post(
            "/api/music/lyric/save",
            headers=self.headers,
            json={"filepath": path, "lines": edited, "write_sidecar": False},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["written_embedded"])
        from server import tag_engine

        self.assertIn("[00:02.25]", tag_engine.read_lyric_text(path))

        # 6. 非法输入：没有任何时间、越权路径、负时间、多余字段
        resp = self.client.post(
            "/api/music/lyric/save",
            headers=self.headers,
            json={"filepath": path, "lines": [{"time": None, "text": "x"}]},
        )
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(
            "/api/music/lyric/save",
            headers=self.headers,
            json={"filepath": os.path.abspath(__file__), "lines": [{"time": 1, "text": "x"}]},
        )
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(
            "/api/music/lyric/save",
            headers=self.headers,
            json={"filepath": path, "lines": [{"time": -3, "text": "x"}]},
        )
        self.assertEqual(resp.status_code, 422)
        resp = self.client.post(
            "/api/music/lyric/save",
            headers=self.headers,
            json={"filepath": path, "lines": [], "evil": 1},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(
            TestClient(app).get("/api/music/lyric/lyrics?filepath=" + q).status_code, 401
        )


if __name__ == "__main__":
    unittest.main()
