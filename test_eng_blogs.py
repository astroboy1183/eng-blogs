#!/usr/bin/env python3
"""Offline tests for eng_blogs — no network, no model, no tokens.

Covers the reading-pool tiers, the served memory, per-source diversity in
both selection paths, deterministic read-times and composition (code owns
links — a hallucinated URL is structurally impossible), blurb parsing,
and the fresh-only archive gate."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import eng_blogs as eb

NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


def post(title="T", source="Netflix", days_old=1, link=None, summary="sum"):
    return {
        "source": source, "title": title, "summary": summary,
        "link": link or f"https://x/{source}/{title}",
        "published": NOW - timedelta(days=days_old),
    }


class PoolTierTest(unittest.TestCase):
    def test_fresh_window_used_when_full(self):
        pool = [post(title=f"p{i}", days_old=i % 10) for i in range(40)]
        cands = eb.candidates_from(pool, NOW)
        self.assertGreaterEqual(len(cands), eb.MIN_CANDIDATES)
        self.assertTrue(all(
            (NOW - p["published"]).days <= eb.POOL_TIERS[0] for p in cands))

    def test_quiet_weeks_widen_to_archive(self):
        pool = [post(title=f"old{i}", days_old=100 + i) for i in range(35)]
        cands = eb.candidates_from(pool, NOW)
        self.assertGreaterEqual(len(cands), eb.MIN_CANDIDATES)  # tier 120d

    def test_tiny_pool_returns_everything(self):
        pool = [post(title="only", days_old=500)]
        self.assertEqual(len(eb.candidates_from(pool, NOW)), 1)


class ServedMemoryTest(unittest.TestCase):
    def test_prunes_and_survives_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved_dir, saved_file = eb.STATE_DIR, eb.SERVED_FILE
            eb.STATE_DIR = Path(tmp)
            eb.SERVED_FILE = Path(tmp) / "served.json"
            try:
                today = NOW.strftime("%Y-%m-%d")
                eb.save_served({"https://a": today, "https://old": "2020-01-01"})
                served = eb.load_served()
                self.assertIn("https://a", served)
                self.assertNotIn("https://old", served)
                eb.SERVED_FILE.write_text("not json")
                self.assertEqual(eb.load_served(), {})
            finally:
                eb.STATE_DIR, eb.SERVED_FILE = saved_dir, saved_file


class SelectionTest(unittest.TestCase):
    def _cands(self):
        return ([post(title=f"n{i}", source="Netflix", days_old=i) for i in range(5)]
                + [post(title=f"u{i}", source="Uber", days_old=i) for i in range(5)]
                + [post(title=f"g{i}", source="Grab", days_old=i) for i in range(5)])

    def test_selector_ranks_and_caps_per_source(self):
        reply = json.dumps(list(range(15)))  # model tries to take everything
        with mock.patch.object(eb, "ask_llm", return_value=reply):
            picks = eb.select_picks(self._cands(), "m")
        self.assertLessEqual(len(picks), eb.PICKS)
        for src in ("Netflix", "Uber", "Grab"):
            self.assertLessEqual(
                sum(1 for p in picks if p["source"] == src), eb.MAX_PER_SOURCE)

    def test_unparseable_selector_falls_back_with_diversity(self):
        with mock.patch.object(eb, "ask_llm", return_value="no json here"):
            picks = eb.select_picks(self._cands(), "m")
        self.assertEqual(len(picks), 6)  # 3 sources × cap 2
        self.assertLessEqual(
            max(sum(1 for p in picks if p["source"] == s)
                for s in ("Netflix", "Uber", "Grab")), eb.MAX_PER_SOURCE)

    def test_invalid_indices_ignored_and_topped_up(self):
        with mock.patch.object(eb, "ask_llm", return_value="[0, 99, -3, 1]"):
            picks = eb.select_picks(self._cands(), "m")
        self.assertEqual([p["title"] for p in picks[:2]], ["n0", "n1"])
        # 15 candidates, cap 2/source over 3 sources → the top-up fills to 6
        self.assertEqual(len(picks), 6)

    def test_under_delivering_selector_topped_up_to_ten(self):
        cands = [post(title=f"p{i}", source=f"S{i}", days_old=i)
                 for i in range(15)]
        with mock.patch.object(eb, "ask_llm", return_value="[3]"):
            picks = eb.select_picks(cands, "m")
        self.assertEqual(len(picks), eb.PICKS)
        self.assertEqual(picks[0]["title"], "p3")  # ranked pick stays first


class BlurbTest(unittest.TestCase):
    def test_markers_parsed_and_whitespace_collapsed(self):
        reply = "<<<1>>>\nFirst  blurb\nacross lines\n<<<2>>>\nSecond blurb"
        with mock.patch.object(eb, "ask_llm", return_value=reply):
            blurbs = eb.write_blurbs([post(), post(title="B")], "m")
        self.assertEqual(blurbs[1], "First blurb across lines")
        self.assertEqual(blurbs[2], "Second blurb")

    def test_missing_blurb_falls_back_in_compose(self):
        p = post(summary="the abstract")
        text = eb.compose([p], {}, pool_size=5, now=NOW)
        self.assertIn("the abstract", text)


class CategoryTest(unittest.TestCase):
    def test_selector_category_wins_fallback_covers_rest(self):
        self.assertEqual(eb.category_of({"source": "Netflix", "cat": "data"}), "data")
        self.assertEqual(eb.category_of({"source": "Netflix", "cat": "bogus"}), "systems")
        self.assertEqual(eb.category_of({"source": "Chip Huyen"}), "ai")
        self.assertEqual(eb.category_of({"source": "Unknown Blog"}), "systems")

    def test_object_reply_parsed_with_categories(self):
        cands = [post(title=f"p{i}", source=f"S{i}") for i in range(12)]
        reply = '[{"i": 2, "cat": "data"}, {"i": 0, "cat": "craft"}]'
        with mock.patch.object(eb, "ask_llm", return_value=reply):
            picks = eb.select_picks(cands, "m")
        self.assertEqual(picks[0]["cat"], "data")
        self.assertEqual(picks[1]["cat"], "craft")
        self.assertEqual(len(picks), eb.PICKS)  # topped up

    def test_compose_groups_under_headers_with_running_numbers(self):
        picks = [
            dict(post(title="A", source="Netflix"), cat="systems"),
            dict(post(title="B", source="dbt"), cat="data"),
            dict(post(title="C", source="Uber"), cat="systems"),
        ]
        text = eb.compose(picks, {1: "a.", 2: "b.", 3: "c."}, 9, NOW)
        self.assertIn("📊 DATA & ANALYTICS", text)
        self.assertIn("⚙️ SYSTEMS & SCALE", text)
        self.assertNotIn("🤖 AI & ML ENG", text)  # empty section omitted
        # data section first; numbering runs 1..3 through the message
        self.assertLess(text.index("📊"), text.index("⚙️"))
        self.assertIn("1. dbt — B", text)
        self.assertIn("2. Netflix — A", text)
        self.assertIn("3. Uber — C", text)
        # blurbs follow their pick's original rank
        self.assertLess(text.index("1. dbt — B"), text.index("b."))


class ComposeTest(unittest.TestCase):
    def test_numbered_deterministic_with_readtime_and_link(self):
        p = post(title="Kafka tuning", source="Jack Vanlightly")
        p["text"] = "word " * 460  # → 2 min at 230 wpm
        text = eb.compose([p], {1: "Great deep-dive."}, pool_size=42, now=NOW)
        self.assertIn("1. Jack Vanlightly — Kafka tuning (2 min ·", text)
        self.assertIn("Great deep-dive.", text)
        self.assertIn(p["link"], text)
        self.assertIn("42 unread posts", text)

    def test_read_minutes_floors_at_one(self):
        self.assertEqual(eb.read_minutes("three words here"), 1)
        self.assertEqual(eb.read_minutes("", "also tiny"), 1)


class ArchiveGateTest(unittest.TestCase):
    def test_only_fresh_posts_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = eb.DATA_DIR
            eb.DATA_DIR = Path(tmp)
            try:
                with mock.patch.object(eb, "fetch_full_text", return_value="body"):
                    eb.archive_posts([
                        post(title="fresh", days_old=0),
                        post(title="ancient", days_old=300),
                    ])
                files = list(Path(tmp).glob("posts-*.jsonl"))
                self.assertEqual(len(files), 1)
                records = [json.loads(l) for l in files[0].read_text().splitlines()]
            finally:
                eb.DATA_DIR = saved
        titles = [r["title"] for r in records]
        self.assertIn("fresh", titles)
        self.assertNotIn("ancient", titles)

    def test_no_fresh_posts_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = eb.DATA_DIR
            eb.DATA_DIR = Path(tmp)
            try:
                eb.archive_posts([post(days_old=300)])
                self.assertEqual(list(Path(tmp).glob("*.jsonl")), [])
            finally:
                eb.DATA_DIR = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
