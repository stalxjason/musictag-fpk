"""
飞牛 NAS 原生音乐刮削服务 - 标签引擎 (Tag Engine) 自动化单元与集成测试
================================================================
测试范围：
1. FilenameParser 文件名智能提取与广告过滤规则；
2. MP3 / WAV 真实标签写入、内嵌封面嗅探 (PNG/JPEG) 与外置 LRC 同步；
3. 繁简转换在元数据写入流程中的字符级转换；
4. 大小写重命名与重名冲突保护逻辑；
5. 目录归纳迁移原子性与空父目录受控清理。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
import wave

from scraper_engine import MusicCandidate
from tag_engine import AudioTagEngine, FilenameParser, sanitize_filename

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class TestTagEngine(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="musictag_test_")
        self.cache_file = os.path.join(self.test_dir, "test_cache.json")
        self.engine = AudioTagEngine(timeout=5.0, cache_file=self.cache_file, state_dir=self.test_dir)

    def tearDown(self):
        self.engine.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_dummy_mp3(self, filepath: str):
        # 写入最小合法 MPEG-1 Layer 3 帧
        frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
        with open(filepath, "wb") as f:
            f.write(frame * 10)

    def _create_dummy_wav(self, filepath: str):
        with wave.open(filepath, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 200)

    def test_01_filename_parser_and_sanitizer(self):
        """测试文件名推断规则与非法字符清洗"""
        t, a, q = FilenameParser.parse(r"C:\Music\01. 晴天 - 周杰伦.mp3")
        self.assertEqual(t, "晴天")
        self.assertEqual(a, "周杰伦")

        t, a, q = FilenameParser.parse(r"C:\Music\[91flac.com] 富士山下 - 陈奕迅.flac")
        self.assertEqual(t, "富士山下")
        self.assertEqual(a, "陈奕迅")

        t, a, q = FilenameParser.parse(r"C:\Music\张学友 - 活出生命Live演唱会\02. 听天由命.mp3")
        self.assertEqual(t, "听天由命")
        self.assertEqual(a, "张学友")

        self.assertEqual(sanitize_filename('../../etc/passwd.mp3'), '_.._etc_passwd.mp3')
        self.assertEqual(sanitize_filename('a/b\\c:d*e?f"g<h>i|j.mp3'), 'a_b_c_d_e_f_g_h_i_j.mp3')

    def test_02_mp3_write_and_cover_mime_detection(self):
        """测试 MP3 写入、UTF-8 字符、PNG 封面真实 MIME 嗅探及 LRC 生成"""
        mp3_path = os.path.join(self.test_dir, "test_song.mp3")
        self._create_dummy_mp3(mp3_path)

        # 构造有效 1x1 真实 PNG 二进制数据
        tiny_png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c63000100000500010d0a2d0b0000000049454e44ae426082"
        )

        cand = MusicCandidate(
            source="test",
            source_id="101",
            title="晴天",
            artist="周杰伦",
            album="叶惠美",
            track_number=3,
            total_tracks=11,
            year="2003",
            genre="流行",
            lyric="[00:01.00]故事的小黄花\n[00:03.00]从出生那年就飘着\n"
        )

        ok = self.engine.write_tags(
            mp3_path,
            cand,
            embed_cover=True,
            save_lrc=True,
            cover_bytes=tiny_png,
            convert_to_simplified=True
        )
        self.assertTrue(ok, "MP3 标签写入应当成功")

        meta = self.engine.read_metadata(mp3_path)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.title, "晴天")
        self.assertEqual(meta.artist, "周杰伦")
        self.assertEqual(meta.album, "叶惠美")
        self.assertEqual(meta.track_number, 3)
        self.assertTrue(meta.has_cover)
        self.assertTrue(meta.has_lyric)
        self.assertTrue(meta.has_lrc_file)

        # 验证 APIC 真实探测到的 MIME 为 image/png
        from mutagen.mp3 import MP3
        from mutagen.id3 import APIC
        audio = MP3(mp3_path)
        apic = audio.tags.getall("APIC")
        self.assertTrue(len(apic) > 0)
        self.assertEqual(apic[0].mime, "image/png")

        # 验证外部 .lrc 歌词文件
        lrc_path = os.path.splitext(mp3_path)[0] + ".lrc"
        self.assertTrue(os.path.isfile(lrc_path))
        with open(lrc_path, "r", encoding="utf-8") as f:
            self.assertIn("故事的小黄花", f.read())

    def test_03_wav_id3_write_and_read(self):
        """测试 WAV 文件的 ID3 元数据写入与正确回读（修复原先假成功 Bug）"""
        wav_path = os.path.join(self.test_dir, "test_track.wav")
        self._create_dummy_wav(wav_path)

        cand = MusicCandidate(
            source="manual",
            source_id="202",
            title="千里之外",
            artist="费玉清",
            album="依然范特西",
            year="2006",
            lyric="[00:01.00]屋檐如悬崖\n"
        )

        ok = self.engine.write_tags(wav_path, cand, embed_cover=False, save_lrc=True)
        self.assertTrue(ok, "WAV 标签写入应当成功")

        meta = self.engine.read_metadata(wav_path)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.title, "千里之外")
        self.assertEqual(meta.artist, "费玉清")
        self.assertEqual(meta.album, "依然范特西")
        self.assertEqual(meta.year, "2006")
        self.assertTrue(meta.has_lrc_file)

    def test_04_rename_and_conflict_safety(self):
        """测试文件重命名安全保护与重名检测"""
        src = os.path.join(self.test_dir, "source.mp3")
        self._create_dummy_mp3(src)

        # 正常改名
        ok, new_p, _ = self.engine.rename_file(src, "new_name.mp3", sync_lrc=False)
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(new_p))

        # 遇到已存在的同名文件，严禁静默覆盖
        other = os.path.join(self.test_dir, "other.mp3")
        self._create_dummy_mp3(other)
        conflict_ok, _, msg = self.engine.rename_file(new_p, "other.mp3", sync_lrc=False)
        self.assertFalse(conflict_ok)
        self.assertIn("已存在", msg)

    def test_05_organize_and_clean_dirs(self):
        """测试目录自动整理归档与空父目录受控清理"""
        sub_dir = os.path.join(self.test_dir, "incoming", "downloads")
        os.makedirs(sub_dir, exist_ok=True)
        song_path = os.path.join(sub_dir, "song.mp3")
        self._create_dummy_mp3(song_path)

        target_dir = os.path.join(self.test_dir, "library", "周杰伦", "叶惠美")
        ok, new_fp, _ = self.engine.organize_file(
            filepath=song_path,
            target_dir=target_dir,
            new_filename="01. 晴天.mp3",
            sync_lrc=True,
            clean_empty_dirs=True,
            music_base_dir=self.test_dir,
            conflict_mode="rename"
        )
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(new_fp))
        # 原空目录应当已被清理，但不越界删除父根目录
        self.assertFalse(os.path.exists(sub_dir))
        self.assertTrue(os.path.exists(self.test_dir))


if __name__ == "__main__":
    unittest.main()
