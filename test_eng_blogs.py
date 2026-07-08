#!/usr/bin/env python3
"""Offline unit tests for eng_blogs — no network, no model.

Covers: the drop-undated freshness rule (and published-over-updated
preference), HTML cleaning, full-text extraction, and the link-deduped
corpus archive.
"""

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import eng_blogs as eb


def stamp(dt):
    return time.struct_time(dt.timetuple())


NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(hours=24)


class FreshnessTest(unittest.TestCase):

    def test_undated_entries_are_dropped(self):
        self.assertFalse(eb.fresh({}, CUTOFF))

    def test_published_date_wins_over_updated(self):
        # An old post lightly edited today must NOT re-enter the window.
        entry = {
            "published_parsed": stamp(NOW - timedelta(days=30)),
            "updated_parsed": stamp(NOW),
        }
        self.assertFalse(eb.fresh(entry, CUTOFF))

    def test_new_post_is_kept(self):
        self.assertTrue(eb.fresh({"published_parsed": stamp(NOW)}, CUTOFF))


class CleanAndFullTextTest(unittest.TestCase):

    def test_clean_strips_tags_and_collapses(self):
        self.assertEqual(eb.clean("<p>a</p>\n\n  <b>b</b>"), "a b")

    def test_full_text_drops_boilerplate_blocks(self):
        html = ("<html><head><title>x</title></head><body>"
                "<script>var junk = 1;</script><style>.c{}</style>"
                "<nav>menu items</nav><p>the actual post body</p></body></html>")
        saved = eb.requests
        eb.requests = SimpleNamespace(
            get=lambda *a, **k: SimpleNamespace(
                text=html, raise_for_status=lambda: None
            )
        )
        try:
            text = eb.fetch_full_text("https://example.com/post")
        finally:
            eb.requests = saved
        self.assertIn("the actual post body", text)
        self.assertNotIn("junk", text)
        self.assertNotIn("menu items", text)

    def test_full_text_failure_returns_empty(self):
        def boom(*a, **k):
            raise OSError("blocked")

        saved = eb.requests
        eb.requests = SimpleNamespace(get=boom)
        try:
            self.assertEqual(eb.fetch_full_text("https://example.com"), "")
        finally:
            eb.requests = saved


class ArchiveTest(unittest.TestCase):

    def test_archive_dedupes_by_link_and_stores_text(self):
        posts = {"Data & Analytics": [
            {"source": "DuckDB", "title": "T", "summary": "S", "link": "https://x/p1"},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            saved_dir, saved_fetch = eb.DATA_DIR, eb.fetch_full_text
            eb.DATA_DIR = Path(tmp)
            eb.fetch_full_text = lambda link: "FULL BODY"
            try:
                eb.archive_posts(posts)
                eb.archive_posts(posts)  # second run must not duplicate
                files = list(Path(tmp).glob("posts-*.jsonl"))
                lines = files[0].read_text().splitlines()
            finally:
                eb.DATA_DIR, eb.fetch_full_text = saved_dir, saved_fetch
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["text"], "FULL BODY")
        self.assertEqual(record["category"], "Data & Analytics")


if __name__ == "__main__":
    unittest.main(verbosity=2)
