"""Unit tests for pure helpers — no network, no ffmpeg required.

Run:  python -m pytest tests/ -q      (or: python tests/test_helpers.py)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notetransfer.common import (extract_urls, human_seconds, url_slug)
from notetransfer.frames import _dhash, _hamming
from notetransfer.transcribe import context_around


class TestUrlHandling(unittest.TestCase):
    def test_extracts_url_from_douyin_share_token(self):
        text = ("9.23 复制打开抖音，看看【某作者的作品】标题很长。 "
                "https://v.douyin.com/XP1lhnugje4/ :7pm m@q.RX 01/17")
        self.assertEqual(extract_urls(text),
                         ["https://v.douyin.com/XP1lhnugje4/"])

    def test_strips_trailing_chinese_punctuation(self):
        self.assertEqual(extract_urls("见 https://example.com/a，然后"),
                         ["https://example.com/a"])

    def test_slug_is_stable_and_short(self):
        u = "https://v.douyin.com/XP1lhnugje4/"
        self.assertEqual(url_slug(u), url_slug(u))
        self.assertEqual(len(url_slug(u)), 10)

    def test_different_urls_get_different_slugs(self):
        """Guards audit item C3: frame filenames must not collide."""
        self.assertNotEqual(url_slug("https://a.com/1"),
                            url_slug("https://a.com/2"))


class TestTimeFormatting(unittest.TestCase):
    def test_under_an_hour(self):
        self.assertEqual(human_seconds(112), "01:52")

    def test_over_an_hour(self):
        self.assertEqual(human_seconds(3725), "1:02:05")


class TestPerceptualHash(unittest.TestCase):
    def test_identical_buffers_have_zero_distance(self):
        buf = bytes(range(72))
        self.assertEqual(_hamming(_dhash(buf), _dhash(buf)), 0)

    def test_short_buffer_does_not_crash(self):
        self.assertIsInstance(_dhash(b"\x00" * 10), int)

    def test_inverted_gradient_differs(self):
        a = _dhash(bytes(range(72)))
        b = _dhash(bytes(reversed(range(72))))
        self.assertGreater(_hamming(a, b), 0)


class TestContextAlignment(unittest.TestCase):
    """Guards audit item B1: frames must carry transcript context."""

    SEGS = [
        {"start": 0.0, "end": 9.0, "text": "开场介绍"},
        {"start": 10.0, "end": 19.0, "text": "第一个要点"},
        {"start": 100.0, "end": 109.0, "text": "很久之后的内容"},
    ]

    def test_picks_overlapping_segments(self):
        ctx = context_around(self.SEGS, 12.0, window=5.0)
        self.assertIn("第一个要点", ctx)
        self.assertNotIn("很久之后", ctx)

    def test_window_can_span_multiple_segments(self):
        ctx = context_around(self.SEGS, 9.5, window=15.0)
        self.assertIn("开场介绍", ctx)
        self.assertIn("第一个要点", ctx)

    def test_falls_back_to_nearest_when_no_overlap(self):
        ctx = context_around(self.SEGS, 60.0, window=5.0)
        self.assertTrue(ctx, "must not return empty; nearest segment expected")

    def test_empty_segments_return_empty_string(self):
        self.assertEqual(context_around([], 10.0), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
