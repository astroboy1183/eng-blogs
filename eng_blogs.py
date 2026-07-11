#!/usr/bin/env python3
"""Engineering reads — 10 hand-picked blog posts every morning.

One Telegram message every morning (6:00 IST via GitHub Actions): the 10
best unread engineering-blog posts for me, ranked against my interests
(BLOG_INTERESTS secret), each with what it covers, why it's worth my
time, and a deterministic read-time. Numbered 1-10, best first.

Engineering blogs post rarely (~40 posts/fortnight across 33 feeds), so
10 FRESH posts a day is impossible — instead the agent keeps a READING
POOL: every post I haven't been served yet, from a window that widens
(14 → 45 → 120 → 730 days) until there are enough candidates. Quiet
weeks surface timeless archive pieces instead of padding; a served post
is never repeated (state/served.json).

The message is assembled DETERMINISTICALLY: code writes the numbered
header lines (source, title, read-time, date) and the links; the model
only ranks candidates and writes each pick's 2-3 sentence blurb — so a
hallucinated link is structurally impossible.

Every fresh post (<48h) is also archived to data/posts-YYYY-MM.jsonl
(committed back by the workflow) — the growing corpus for the planned
ask-my-library RAG project.

Same fleet pattern as the rest: own repo, own schedule, fails alone.
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

# (source name, feed url) — every URL probed before inclusion (sweeps
# 4-11 Jul 2026). Tested and REJECTED: LinkedIn Eng (404), DoorDash
# (403), Shopify Eng (empty), ClickHouse + PlanetScale blog feeds (404).
# Simon Willison and the GitHub blog live in tech-news; not duplicated.
FEEDS = [
    # data & analytics platforms
    ("Databricks", "https://www.databricks.com/feed"),
    ("Confluent", "https://www.confluent.io/rss.xml"),
    ("Snowflake", "https://www.snowflake.com/feed/"),
    ("AWS Big Data", "https://aws.amazon.com/blogs/big-data/feed/"),
    ("dbt", "https://www.getdbt.com/blog/rss.xml"),
    ("DuckDB", "https://duckdb.org/feed.xml"),
    # systems & scale
    ("Netflix", "https://netflixtechblog.com/feed"),
    ("Uber", "https://eng.uber.com/feed/"),
    ("Meta", "https://engineering.fb.com/feed/"),
    ("Cloudflare", "https://blog.cloudflare.com/rss/"),
    ("Discord", "https://discord.com/blog/rss.xml"),
    ("Slack", "https://slack.engineering/feed/"),
    ("Stripe", "https://stripe.com/blog/feed.rss"),
    ("Dropbox", "https://dropbox.tech/feed"),
    ("Lyft", "https://eng.lyft.com/feed"),
    ("Grab", "https://engineering.grab.com/feed.xml"),
    ("fly.io", "https://fly.io/blog/feed.xml"),
    ("Tailscale", "https://tailscale.com/blog/index.xml"),
    ("High Scalability", "https://highscalability.com/rss/"),
    # product & ML eng
    ("Spotify", "https://engineering.atspotify.com/feed/"),
    ("Airbnb", "https://medium.com/feed/airbnb-engineering"),
    ("Pinterest", "https://medium.com/feed/pinterest-engineering"),
    ("Canva", "https://www.canva.dev/blog/engineering/feed.xml"),
    # the individuals every good engineer reads
    ("Dan Luu", "https://danluu.com/atom.xml"),
    ("Julia Evans", "https://jvns.ca/atom.xml"),
    ("Chip Huyen", "https://huyenchip.com/feed.xml"),
    ("Eugene Yan", "https://eugeneyan.com/rss/"),
    ("Jack Vanlightly", "https://jack-vanlightly.com/blog?format=rss"),
    ("Brendan Gregg", "https://www.brendangregg.com/blog/rss.xml"),
    ("Murat Demirbas", "https://muratbuffalo.blogspot.com/feeds/posts/default"),
    ("Martin Fowler", "https://martinfowler.com/feed.atom"),
    ("Mitchell Hashimoto", "https://mitchellh.com/feed.xml"),
    ("Pragmatic Engineer", "https://blog.pragmaticengineer.com/rss/"),
]

# Message sections, in display order. Picks are categorized PER POST by
# the selector (a Netflix post can be a data post); unknown/missing
# categories fall back to the source's home category below.
CATEGORIES = {
    "data": "📊 DATA & ANALYTICS",
    "systems": "⚙️ SYSTEMS & SCALE",
    "ai": "🤖 AI & ML ENG",
    "craft": "🧰 CRAFT & CAREER",
}
SOURCE_CATEGORY = {
    "Databricks": "data", "Confluent": "data", "Snowflake": "data",
    "AWS Big Data": "data", "dbt": "data", "DuckDB": "data",
    "Jack Vanlightly": "data", "Grab": "data",
    "Netflix": "systems", "Uber": "systems", "Meta": "systems",
    "Cloudflare": "systems", "Discord": "systems", "Slack": "systems",
    "Stripe": "systems", "Dropbox": "systems", "Lyft": "systems",
    "fly.io": "systems", "Tailscale": "systems",
    "High Scalability": "systems", "Brendan Gregg": "systems",
    "Murat Demirbas": "systems", "Mitchell Hashimoto": "systems",
    "Spotify": "systems", "Airbnb": "systems", "Pinterest": "systems",
    "Canva": "systems",
    "Chip Huyen": "ai", "Eugene Yan": "ai",
    "Julia Evans": "craft", "Martin Fowler": "craft",
    "Pragmatic Engineer": "craft", "Dan Luu": "craft",
}


def category_of(pick):
    """The pick's section: selector's judgment, else its source's home."""
    cat = pick.get("cat")
    if cat in CATEGORIES:
        return cat
    return SOURCE_CATEGORY.get(pick["source"], "systems")


PICKS = 10                 # the daily reading list length
MAX_PER_SOURCE = 2         # diversity: one blog must not fill the list
POOL_TIERS = (14, 45, 120, 730)  # widen the unread window until enough
MIN_CANDIDATES = 30        # stop widening once the pool holds this many
MAX_CANDIDATES = 120       # prompt bound for the selector
ENTRIES_PER_FEED = 25      # feeds carry weeks of history; take it
SUMMARY_CHARS = 400
EXCERPT_CHARS = 3000       # per pick, for the blurb writer
ARCHIVE_HOURS = 48         # corpus keeps posts as they appear, not backfill
READ_WPM = 230             # deterministic read-time estimate

FETCH_TIMEOUT = 20
FETCH_HEADERS = {"User-Agent": "eng-blogs-digest/1.0 (+https://github.com/astroboy1183/eng-blogs)"}

TAG_RE = re.compile(r"<[^>]+>")
MARKER_RE = re.compile(r"<<<(\d+)>>>")

DATA_DIR = BASE_DIR / "data"
STATE_DIR = BASE_DIR / "state"
SERVED_FILE = STATE_DIR / "served.json"
SERVED_DAYS = 800  # longer than the widest pool tier, so no re-serves
FULLTEXT_CHARS = 20000  # per corpus record; plenty for embedding


def clean(html):
    """Strip tags and collapse whitespace — feed summaries arrive as HTML."""
    return " ".join(TAG_RE.sub(" ", html or "").split())


def load_served():
    """{link: 'YYYY-MM-DD'} of posts already served, pruned to window."""
    try:
        served = json.loads(SERVED_FILE.read_text())
    except (OSError, ValueError):
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SERVED_DAYS)).strftime(
        "%Y-%m-%d"
    )
    return {k: v for k, v in served.items() if isinstance(v, str) and v >= cutoff}


def save_served(served):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SERVED_FILE.write_text(json.dumps(served, indent=0, sort_keys=True) + "\n")


def fetch_full_text(link):
    """Readable text of a post, tags stripped — '' on any failure."""
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


def read_minutes(text, fallback_summary=""):
    """Deterministic read-time from the fetched word count."""
    words = len((text or fallback_summary).split())
    return max(1, round(words / READ_WPM))


def gather_pool(served):
    """Every unserved, dated post across all feeds, newest first.

    Returns (posts, failed): each post carries source/title/summary/link
    and an aware UTC `published`. Undated entries are dropped — a pool
    without dates can't tier. `failed` lists feed rot to surface."""
    pool, failed, seen_links = [], [], set()
    for name, url in FEEDS:
        try:
            resp = requests.get(url, timeout=FETCH_TIMEOUT, headers=FETCH_HEADERS)
            resp.raise_for_status()
        except Exception as exc:
            failed.append(f"{name} ({type(exc).__name__})")
            continue
        feed = feedparser.parse(resp.content)
        if not feed.entries:
            failed.append(f"{name} ({'malformed' if feed.bozo else 'no entries'})")
            continue
        for e in feed.entries[:ENTRIES_PER_FEED]:
            stamp = e.get("published_parsed") or e.get("updated_parsed")
            link = e.get("link", "")
            if not stamp or not link or link in served or link in seen_links:
                continue
            seen_links.add(link)
            pool.append(
                {
                    "source": name,
                    "title": e.get("title", "(untitled)"),
                    "summary": clean(e.get("summary", ""))[:SUMMARY_CHARS],
                    "link": link,
                    "published": datetime(*stamp[:6], tzinfo=timezone.utc),
                }
            )
    pool.sort(key=lambda p: p["published"], reverse=True)
    return pool, failed


def candidates_from(pool, now):
    """Widen the unread window tier by tier until the pool holds enough
    candidates — quiet weeks reach further back instead of padding."""
    for days in POOL_TIERS:
        cutoff = now - timedelta(days=days)
        cands = [p for p in pool if p["published"] >= cutoff]
        if len(cands) >= MIN_CANDIDATES:
            return cands[:MAX_CANDIDATES]
    return pool[:MAX_CANDIDATES]


def interests():
    """My reading interests, from the BLOG_INTERESTS secret."""
    return os.environ.get("BLOG_INTERESTS") or (
        "data engineering, distributed systems, streaming/Kafka, "
        "databases, ML systems, performance engineering, postmortems"
    )


def fallback_picks(cands):
    """Deterministic selection when the selector reply is unparseable:
    newest first with the per-source cap — quality costs, never the list."""
    picked, per_source = [], {}
    for p in cands:
        if per_source.get(p["source"], 0) >= MAX_PER_SOURCE:
            continue
        picked.append(p)
        per_source[p["source"]] = per_source.get(p["source"], 0) + 1
        if len(picked) == PICKS:
            break
    return picked


def select_picks(cands, model):
    """Stage 1: a cheap model ranks the pool against my interests and
    picks the day's 10, at most MAX_PER_SOURCE per blog."""
    lines = "\n".join(
        f"{i}. [{p['source']}] {p['title']} ({p['published']:%d %b %Y})"
        f"{' | ' + p['summary'][:150] if p['summary'] else ''}"
        for i, p in enumerate(cands)
    )
    reply = ask_llm(
        "You are picking today's engineering reading list for me. I am a "
        f"data & AI engineer; my interests: {interests()}.\n\n"
        f"=== CANDIDATES (unread posts, newest first) ===\n{lines}\n\n"
        f"Pick the {PICKS} posts MOST worth my time, best first. Rules:\n"
        f"- At most {MAX_PER_SOURCE} per source.\n"
        "- Substance over news: deep-dives, postmortems, performance "
        "war stories and architecture write-ups beat announcements.\n"
        "- Prefer my interests but keep 1-2 wildcard picks that a strong "
        "engineer would regret missing.\n"
        "- Older posts are fine — timeless beats recent-but-thin.\n\n"
        "Output ONLY a JSON array, rank order, one object per pick: "
        '[{"i": <candidate index>, "cat": "<data | systems | ai | craft '
        "— judge the POST, not the blog>\"}, …]. No prose.",
        max_tokens=500,
        model=model,
    )
    start, end = reply.find("["), reply.rfind("]")
    try:
        idx = json.loads(reply[start : end + 1])
        picked, per_source = [], {}
        for item in idx:
            # tolerate both bare indices and {"i":…, "cat":…} objects
            i = item.get("i") if isinstance(item, dict) else item
            cat = item.get("cat") if isinstance(item, dict) else None
            if not (isinstance(i, int) and 0 <= i < len(cands)):
                continue
            p = dict(cands[i], cat=cat)
            if per_source.get(p["source"], 0) >= MAX_PER_SOURCE:
                continue
            if any(q["link"] == p["link"] for q in picked):
                continue
            picked.append(p)
            per_source[p["source"]] = per_source.get(p["source"], 0) + 1
            if len(picked) == PICKS:
                break
        # Deterministic top-up: the list is 10 whenever the pool allows,
        # even if the selector under-delivered — newest unpicked first,
        # per-source cap still respected.
        for p in cands:
            if len(picked) == PICKS:
                break
            if per_source.get(p["source"], 0) >= MAX_PER_SOURCE:
                continue
            if any(q["link"] == p["link"] for q in picked):
                continue
            picked.append(dict(p))
            per_source[p["source"]] = per_source.get(p["source"], 0) + 1
        return picked or fallback_picks(cands)
    except (ValueError, TypeError, AttributeError):
        return fallback_picks(cands)


def write_blurbs(picks, model):
    """Stage 2: 2-3 sentences per pick from the post's real text.

    Marker-delimited plain text (<<<N>>>), parsed defensively — a
    missing blurb falls back to the feed summary, never sinks the list."""
    blocks = []
    for i, p in enumerate(picks, 1):
        body = p.get("text") or p["summary"] or "(title only)"
        blocks.append(
            f"<<<{i}>>> [{p['source']}] {p['title']}\nTEXT: {body[:EXCERPT_CHARS]}"
        )
    reply = ask_llm(
        "You are annotating my daily engineering reading list (I am a "
        "data & AI engineer). For EACH numbered post below, write 2-3 "
        "sentences: what it actually covers (concrete techniques, "
        "numbers, systems named in TEXT), why it is worth my time, and "
        "the one takeaway. Be specific, never generic; where TEXT is "
        "thin, stay conservative.\n\n"
        + "\n\n".join(blocks)
        + "\n\nOutput format — repeat for every post, nothing else:\n"
        "<<<1>>>\n<the 2-3 sentences for post 1>\n"
        "<<<2>>>\n<the 2-3 sentences for post 2>\n…",
        max_tokens=2500,
        model=model,
    )
    blurbs = {}
    parts = MARKER_RE.split(reply)
    # parts = [prefix, "1", text1, "2", text2, …]
    for num, text in zip(parts[1::2], parts[2::2]):
        cleaned = " ".join(text.split())
        if cleaned:
            blurbs[int(num)] = cleaned
    return blurbs


def compose(picks, blurbs, pool_size, now):
    """Deterministic assembly: code owns sections, numbering and links.

    Picks are grouped under their category headers (display order fixed
    by CATEGORIES), rank preserved within a section; numbering runs
    1..N through the whole message. blurbs is keyed by the pick's
    ORIGINAL rank (the order handed to write_blurbs)."""
    lines = [
        f"📚 Eng reads — {now:%a %d %b}",
        f"{len(picks)} picks · {pool_size} unread posts across {len(FEEDS)} blogs",
    ]
    number = 0
    for cat, header in CATEGORIES.items():
        section = [(rank, p) for rank, p in enumerate(picks, 1)
                   if category_of(p) == cat]
        if not section:
            continue
        lines.append("")
        lines.append(header)
        for rank, p in section:
            number += 1
            mins = read_minutes(p.get("text"), p["summary"])
            lines.append(
                f"{number}. {p['source']} — {p['title']} "
                f"({mins} min · {p['published']:%d %b %Y})"
            )
            lines.append(blurbs.get(rank) or p["summary"] or "(no summary available)")
            lines.append(p["link"])
            lines.append("")
    return "\n".join(lines).rstrip()


def archive_posts(posts):
    """Append FRESH posts (<ARCHIVE_HOURS old) to the monthly JSONL corpus
    — the RAG project's raw material keeps growing exactly as before.
    Best-effort: an archive failure must never cost the reading list."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=ARCHIVE_HOURS)
        fresh = [p for p in posts if p["published"] >= cutoff]
        if not fresh:
            return
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
            for p in fresh:
                if p["link"] in have:
                    continue
                record = {
                    "date": today, "source": p["source"], "title": p["title"],
                    "summary": p["summary"], "link": p["link"],
                    "text": fetch_full_text(p["link"]),
                }
                fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def main():
    load_dotenv(BASE_DIR / ".env")
    now = datetime.now(timezone.utc)
    select_model = os.environ.get("BLOG_MODEL_SELECT") or "claude-haiku-4-5"
    write_model = os.environ.get("BLOG_MODEL_WRITE") or "claude-sonnet-5"

    served = load_served()
    pool, failed = gather_pool(served)
    print(f"pool: {len(pool)} unread posts, {len(failed)} feeds failing")
    archive_posts(pool)  # grow the RAG corpus before anything else

    if not pool:
        # Only an all-feeds-dead morning produces this — loud, not silent.
        send_telegram(
            "📚 Eng reads — no readable posts today.\n"
            "⚠️ feeds not responding: " + (", ".join(failed) or "(unknown)")
        )
        return

    cands = candidates_from(pool, now)
    picks = select_picks(cands, select_model)
    for p in picks:  # real text for blurbs + read-times
        p["text"] = fetch_full_text(p["link"])
    blurbs = write_blurbs(picks, write_model)

    body = compose(picks, blurbs, len(pool), datetime.now(IST))
    if failed:
        body += "\n\n⚠️ feeds not responding: " + ", ".join(failed)
    send_telegram(body)

    # Remember what was served — after the send, so a state failure never
    # costs the reading list.
    today = now.strftime("%Y-%m-%d")
    for p in picks:
        served[p["link"]] = today
    try:
        save_served(served)
    except OSError:
        pass


if __name__ == "__main__":
    main()
