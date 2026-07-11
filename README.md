# eng-blogs

**My daily engineering reading list**: the 10 best unread blog posts
for a data & AI engineer, every morning at 6:00 IST — ranked against my
interests, each with what it covers, why it's worth the time, and a
read-time estimate. One agent, one task, one bot:
`@jayanth_eng_blogs_bot`.

```
📚 Eng reads — Sat 12 Jul
10 picks · 497 unread posts across 33 blogs

📊 DATA & ANALYTICS
1. Jack Vanlightly — Apache Kafka performance #1: linger.ms (9 min · 08 Jul 2026)
   What the post covers, why it's worth my time, the one takeaway.
   https://…
2. Grab — Scaling Grab's Data Lake: our journey to Apache Iceberg (12 min · 06 Jul 2026)
   …

⚙️ SYSTEMS & SCALE
3. Netflix — …

🤖 AI & ML ENG
6. Chip Huyen — …

🧰 CRAFT & CAREER
9. Dan Luu — …
```

Picks are grouped under four sections — 📊 DATA & ANALYTICS,
⚙️ SYSTEMS & SCALE, 🤖 AI & ML ENG, 🧰 CRAFT & CAREER — categorized
**per post** by the selector (a Netflix post about their warehouse is a
data post), with a deterministic source-based fallback. Empty sections
are omitted; rank order is preserved within each section.

## The reading-pool design

Engineering blogs post rarely (~40 posts/fortnight across 33 feeds), so
"10 fresh posts a day" is arithmetically impossible. Instead:

- **The pool** — every post I haven't been served yet, across all
  feeds. The candidate window widens tier by tier (14 → 45 → 120 → 730
  days) until it holds enough candidates, so quiet weeks surface
  timeless archive pieces instead of padding.
- **Served memory** (`state/served.json`, committed back) — a post is
  served exactly once, ever.
- **10 guaranteed** — the model ranks; if it under-delivers, a
  deterministic top-up fills the list (newest unpicked first). At most
  2 picks per source per day, so one blog never dominates.

## The roster (33 blogs, every URL probed before inclusion)

Company engineering: Netflix, Uber, Meta, Cloudflare, Stripe, Slack,
Discord, Dropbox, Spotify, Airbnb, Pinterest, Canva, Lyft, Grab,
fly.io, Tailscale, High Scalability. Data platforms: Databricks,
Confluent, Snowflake, AWS Big Data, dbt, DuckDB. **Individuals**: Dan
Luu, Julia Evans, Chip Huyen, Eugene Yan, Jack Vanlightly, Brendan
Gregg, Murat Demirbas, Martin Fowler, Mitchell Hashimoto, the
Pragmatic Engineer. (Rejected as dead/blocked: LinkedIn Eng, DoorDash,
Shopify Eng, ClickHouse and PlanetScale blog feeds. Simon Willison and
the GitHub blog belong to tech-news.)

## How the code works

`eng_blogs.py`, in pipeline order:

- **`gather_pool(served)`** — all dated, unserved posts from all feeds
  (25/feed), newest first; per-feed try/except with a `failed` list so
  feed rot is surfaced in the message footer, never hidden.
- **`candidates_from(pool, now)`** — the tiered window: stop widening
  once `MIN_CANDIDATES = 30` unread posts are in view (capped at 120
  for the selector prompt).
- **`select_picks(cands, model)`** — stage 1 (haiku): ranks candidates
  against `BLOG_INTERESTS` (a secret; substance over news, 1-2
  wildcards, older-is-fine), returns indices; code enforces the
  per-source cap, drops invalid indices, and TOPS UP deterministically
  to 10. An unparseable reply falls back to newest-first-with-cap.
- **`write_blurbs(picks, model)`** — stage 2 (sonnet): 2-3 specific
  sentences per pick from the post's REAL fetched text, returned in
  `<<<N>>>` marker format; a missing blurb falls back to the feed
  abstract.
- **`compose(...)`** — DETERMINISTIC assembly: code writes the numbered
  headers (source — title, read-time from word count at 230 wpm, date)
  and the links. The model never emits a URL, so a hallucinated link is
  structurally impossible — no validator needed.
- **`archive_posts(pool)`** — posts published in the last 48h are
  appended to `data/posts-YYYY-MM.jsonl` (full text fetched, deduped by
  link): the growing corpus for the ask-my-library RAG project,
  unchanged from v1.
- **`agentlib.py`** (vendored) — `ask_llm()`, `send_telegram()`.

## Design notes

- The old agent was silent-when-quiet; this one is a daily ritual — 10
  reads always (the only silent-ish path is every feed being dead,
  which sends a loud feed-rot alert instead).
- Read-times are deterministic (fetched word count), not model guesses.
- `BLOG_MODEL_SELECT` / `BLOG_MODEL_WRITE` override the two model tiers.
- Tests run in CI on every push (`.github/workflows/tests.yml`).

## Ops

- Schedule: fleet-scheduler dispatches 06:00 IST sharp; backup crons
  `30 0` / `30 1 * * *` UTC with the dedupe guard.
- Run now: `gh workflow run eng-blogs.yml -R astroboy1183/eng-blogs`
- Secrets (Actions): `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`, `BLOG_INTERESTS` (comma phrases; change anytime
  with `gh secret set BLOG_INTERESTS`).
- Memories: `state/served.json` (never re-serve) and `data/*.jsonl`
  (the RAG corpus), both committed back by the workflow.
