"""
飞牛 NAS 原生音乐刮削服务 - 刮削引擎单元与集成测试套件
===================================================
测试覆盖：
1. 繁简归一化无歧义字符级对齐（修复原本配对错位 Bug）；
2. 置信度评分算法及标题不匹配时对完整度加分的合理抑制；
3. 多源并发搜索集成测试（可选在线模式，默认严格断言离线算法）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

from scraper_engine import MusicCandidate, MusicScraperEngine

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class TestScraperEngine(unittest.TestCase):

    def setUp(self):
        self.engine = MusicScraperEngine(timeout=5.0)

    def test_01_traditional_simplified_normalization(self):
        """测试简繁对齐归一化算法（周杰倫、張學友、愛在西元前等）"""
        cases = [
            ("周杰倫", "周杰伦"),
            ("陳奕迅", "陈奕迅"),
            ("張學友", "张学友"),
            ("鄧麗君", "邓丽君"),
            ("劉德華", "刘德华"),
            ("葉惠美", "叶惠美"),
            ("無與倫比", "无与伦比"),
            ("說好的幸福呢", "说好的幸福呢"),
            ("愛在西元前", "爱在西元前"),
        ]
        for trad, simp in cases:
            norm_trad = self.engine.normalize_text(trad)
            norm_simp = self.engine.normalize_text(simp)
            self.assertEqual(
                norm_trad,
                norm_simp,
                f"简繁归一化不一致: '{trad}'({norm_trad}) != '{simp}'({norm_simp})"
            )

    def test_02_scoring_logic_and_bonus_suppression(self):
        """测试置信度评分：完全匹配、相似匹配、无关歌曲完整度加分抑制"""
        # 1. 标题歌手完全一致
        exact = MusicCandidate(source="qqmusic", source_id="1", title="晴天", artist="周杰伦")
        score_exact = self.engine.calculate_score("晴天", "周杰伦", exact.title, exact.artist, exact)
        self.assertEqual(score_exact, 85.0)

        # 2. 标题完全一致且带完整元数据
        full = MusicCandidate(
            source="qqmusic",
            source_id="2",
            title="晴天",
            artist="周杰伦",
            album="叶惠美",
            year="2003",
            cover_url="https://example.com/cover.jpg",
            lyric="[00:01.00]test"
        )
        score_full = self.engine.calculate_score("晴天", "周杰伦", full.title, full.artist, full)
        self.assertEqual(score_full, 100.0)

        # 3. 完全无关的歌曲即使携带全量元数据，由于 title_sim < 0.4，严禁赋予加分
        unrelated = MusicCandidate(
            source="netease",
            source_id="3",
            title="完全无关的随机曲目",
            artist="陌生歌手",
            album="未知专辑",
            year="2020",
            cover_url="https://example.com/c.jpg",
            lyric="test"
        )
        score_unrelated = self.engine.calculate_score("晴天", "周杰伦", unrelated.title, unrelated.artist, unrelated)
        self.assertLess(score_unrelated, 30.0, "无关歌曲评分不得因元数据完整度虚高")

    def test_03_online_search_smoke(self):
        """在线冒烟测试：仅当环境变量 MUSICTAG_ONLINE_TEST=1 时请求外网 API"""
        if os.environ.get("MUSICTAG_ONLINE_TEST") != "1":
            self.skipTest("默认跳过在线外网测试，设置 MUSICTAG_ONLINE_TEST=1 可开启")

        async def _run():
            cands = await self.engine.search(title="晴天", artist="周杰伦", sources=["qqmusic", "itunes"], limit_per_source=2)
            self.assertTrue(len(cands) > 0, "在线搜索应当返回有效候选")
            best = cands[0]
            self.assertGreaterEqual(best.score, 85.0, "最佳匹配得分应达到默认阈值")

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
