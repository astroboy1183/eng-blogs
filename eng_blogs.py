#!/usr/bin/env python3
"""Engineering-blog digest.

One Telegram message every evening (~19:07 IST via GitHub Actions): new
posts from the company engineering blogs worth a data engineer's time —
what each post is about, the technical takeaway, and a link.

Blogs post rarely (0–5 posts/day across all feeds), so unlike the tech
briefing this agent is SILENT on days with no new posts — a message always
means there's something to read. Set ENG_BLOGS_FORCE=1 to send regardless
(used for testing).

Every gathered post is also archived to data/posts-YYYY-MM.jsonl
(committed back by the workflow) — the growing corpus for the planned
ask-my-library RAG project. The corpus only exists from the day
collection starts, so it starts now.

Same fleet pattern as tech-news: own repo, own schedule, fails alone.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
from dotenv import load_dotenv

from agentlib import ask_llm, send_telegram

BASE_DIR = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")

# category → [(source name, feed url)] — URLs verified 4 Jul 2026
# (Stripe/Dropbox/Canva added + verified 7 Jul 2026. Stripe's feed is the
# whole blog, not just engineering — the drop-pure-marketing rule in the
# prompt handles the mix.)
FEEDS = {
    "Data & Analytics": [
        ("Databricks", "https://www.databricks.com/feed"),
        ("Confluent", "https://www.confluent.io/rss.xml"),
        ("Snowflake", "https://www.snowflake.com/feed/"),
        ("AWS Big Data", "https://aws.amazon.com/blogs/big-data/feed/"),
        ("dbt", "https://www.getdbt.com/blog/rss.xml"),
        ("DuckDB", "https://duckdb.org/feed.xml"),
    ],
    "Systems & Scale": [
        ("Netflix", "https://netflixtechblog.com/feed"),
        ("Uber", "https://eng.uber.com/feed/"),
        ("Meta", "https://engineering.fb.com/feed/"),
        ("Cloudflare", "https://blog.cloudflare.com/rss/"),
        ("Discord", "https://discord.com/blog/rss.xml"),
        ("Slack", "https://slack.engineering/feed/"),
        ("Stripe", "https://stripe.com/blog/feed.rss"),
        ("Dropbox", "https://dropbox.tech/feed"),
    ],
    "Product & ML Eng": [
        ("Spotify", "https://engineering.atspotify.com/feed/"),
        ("Airbnb", "https://medium.com/feed/airbnb-engineering"),
        ("Pinterest", "https://medium.com/feed/pinterest-engineering"),
        ("Canva", "https://www.canva.dev/blog/engineering/feed.xml"),
    ],
}
ENTRIES_PER_FEED = 8
# Whole-blog / high-volume sources publish several posts a day, so the
# default cap can truncate them before fresh() ever runs. Give the busy
# feeds more headroom so a busy day isn't clipped ahead of the freshness
# check.
PER_FEED_LIMIT = {
    "Databricks": 30,
    "AWS Big Data": 30,
    "Stripe": 30,
}
SUMMARY_CHARS = 400  # blogs have meaty abstracts; keep more than for news
DEFAULT_LOOKBACK_HOURS = 24
FETCH_TIMEOUT = 20  # seconds per feed — one hanging host must not stall the run
# A plain User-Agent; some corporate feeds reject the bare python-requests one.
FETCH_HEADERS = {"User-Agent": "eng-blogs-digest/1.0 (+https://github.com/astroboy1183/eng-blogs)"}

TAG_RE = re.compile(r"<[^>]+>")

DATA_DIR = BASE_DIR / "data"
FULLTEXT_CHARS = 20000  # per post; plenty for embedding, bounded for git


def fetch_full_text(link):
    """Readable text of a post, tags stripped — '' on any failure.

    The corpus should hold what the RAG project will embed: the post
    itself, not just its feed abstract. Script/style/nav blocks are
    dropped before tag-stripping so boilerplate doesn't drown the
    content. Hosts that block bots or paywall (some Medium blogs) fall
    back to the abstract-only record."""
    try:
        resp = requests.get(link, timeout=FETCH_TIMEOUT, headers=FETCH_HEADERS)
        resp.raise_for_status()
        html = re.sub(
            r"(?is)<(script|style|head|nav|footer|header)[^>]*>.*?</\1>",
            " ",
            resp.text,
        )
        text = re.sub(r"<[^>]+>", " ", html)
        return " ".join(text.split())[:FULLTEXT_CHARS]
    except Exception:
        return ""


def archive_posts(posts):
    """Append today's posts to a monthly JSONL corpus file.

    Raw material for the future ask-my-library RAG project: date,
    category, source, title, abstract, link — plus the post's full text
    (fetched per new post; volume is 0-5/day). Deduped by link against
    the current month's file. Best-effort: an archive failure must never
    cost the digest."""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        path = DATA_DIR / f"posts-{datetime.now(timezone.utc):%Y-%m}.jsonl"
        have = set()
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    have.add(json.loads(line).get("link"))
                except ValueError:
                    continue
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with path.open("a") as fh:
            for category, entries in posts.items():
                for p in entries:
                    if p["link"] and p["link"] not in have:
                        record = {"date": today, "category": category, **p}
                        record["text"] = fetch_full_text(p["link"])
                        fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def clean(html):
    """Strip tags and collapse whitespace — feed summaries arrive as HTML."""
    return " ".join(TAG_RE.sub(" ", html or "").split())


def fresh(entry, cutoff):
    """Keep entries newer than cutoff; undated entries are DROPPED here —
    corporate feeds are reliably dated, and an undated stale post repeating
    daily is worse than missing one.

    Prefer the publish date; fall back to the updated date ONLY when there
    is no publish date at all. Otherwise a lightly edited OLD post carries a
    fresh updated_parsed, re-enters the 24h window, and reappears in the
    digest days after it first ran."""
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    if not stamp:
        return False
    return datetime(*stamp[:6], tzinfo=timezone.utc) >= cutoff


def gather_posts(lookback_hours):
    """Returns ({category: [{source, title, summary, link}, ...]}, failed).

    Each feed is fetched over HTTP with an explicit timeout so one hanging
    host cannot stall the whole run until the 15-min job timeout. `failed`
    lists feeds that erred, returned a non-200 status, or yielded no usable
    entries this run, so the digest can surface feed rot loudly."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    out, failed = {}, []
    for category, sources in FEEDS.items():
        posts = []
        for name, url in sources:
            try:
                resp = requests.get(url, timeout=FETCH_TIMEOUT, headers=FETCH_HEADERS)
                resp.raise_for_status()
            except Exception as exc:  # timeout / DNS / non-200 → note and move on
                failed.append(f"{name} ({type(exc).__name__})")
                continue
            feed = feedparser.parse(resp.content)
            if not feed.entries:
                # No usable entries: either a parse failure (bozo) or an empty
                # feed — either way this source gave us nothing this run.
                failed.append(f"{name} ({'malformed' if feed.bozo else 'no entries'})")
                continue
            limit = PER_FEED_LIMIT.get(name, ENTRIES_PER_FEED)
            for e in feed.entries[:limit]:
                if not fresh(e, cutoff):
                    continue
                posts.append(
                    {
                        "source": name,
                        "title": e.get("title", "(untitled)"),
                        "summary": clean(e.get("summary", ""))[:SUMMARY_CHARS],
                        "link": e.get("link", ""),
                    }
                )
        out[category] = posts
    return out, failed


def summarize(posts):
    """One model call: raw posts in, compact reading guide out."""
    blocks = []
    for category, entries in posts.items():
        lines = "\n".join(
            f"- [{p['source']}] {p['title']} | {p['summary']} | {p['link']}"
            for p in entries
        )
        blocks.append(f"=== {category} ===\n{lines or '(no new posts)'}")

    prompt = (
        "You are composing my evening engineering-blog digest. Below are "
        "today's new posts from company engineering blogs, grouped by "
        "category ([source] title | abstract | link). I am a data engineer. "
        "Plain text only — no markdown headers or bold.\n\n"
        + "\n\n".join(blocks)
        + "\n\n"
        "Produce this structure, using ONLY sections that have posts (skip "
        "empty ones entirely):\n\n"
        "🗄 DATA & ANALYTICS\n\n"
        "⚙️ SYSTEMS & SCALE\n\n"
        "🚀 PRODUCT & ML ENG\n\n"
        "Rules:\n"
        "- Include EVERY post (volume is low) unless one is pure marketing "
        "with no engineering content — drop those silently.\n"
        "- Each post: 'Source — title' on one line, then 1–2 sentences: "
        "what the post covers and the single technical takeaway for a data "
        "engineer, then the link on its own line.\n"
        "- Rank within a section: architecture deep-dives and postmortems "
        "first, release notes and how-tos after.\n"
        "- Blank line between posts."
    )
    return ask_llm(prompt, max_tokens=3000)


def main():
    load_dotenv(BASE_DIR / ".env")
    # Read after load_dotenv so .env can set it too; a manual workflow run
    # passes a wider window here to catch up after missed days.
    lookback = int(os.environ.get("ENG_BLOGS_LOOKBACK_HOURS", DEFAULT_LOOKBACK_HOURS))
    posts, failed = gather_posts(lookback)
    total = sum(len(v) for v in posts.values())
    force = os.environ.get("ENG_BLOGS_FORCE")
    if total:
        archive_posts(posts)  # grow the RAG corpus even before summarizing

    # Silent only when there is genuinely nothing to report: no new posts AND
    # no feed rot to flag. A dead feed still gets surfaced on a quiet day —
    # otherwise the rot hides forever behind the silence.
    if total == 0 and not failed and not force:
        print(f"no new posts in the last {lookback}h — staying silent")
        return

    header = (
        f"📚 Engineering blogs — {datetime.now(IST):%a %d %b %Y}\n"
        f"({total} new posts in the last {lookback}h)\n\n"
    )
    if total:
        body = summarize(posts)
    elif force:
        body = "No new posts today (forced send)."
    else:
        body = "No new posts today."
    if failed:
        body += "\n\n⚠️ feeds not responding: " + ", ".join(failed)
    send_telegram(header + body)


if __name__ == "__main__":
    main()
