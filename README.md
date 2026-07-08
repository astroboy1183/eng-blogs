# eng-blogs

Evening engineering-blog digest → Telegram, ~19:07 IST daily via GitHub
Actions.

18 company engineering blogs — data platforms, systems at scale, product
& ML engineering — filtered and summarized for a data engineer. Silent
on days with no new posts. One agent, one task, one bot:
`@jayanth_eng_blogs_bot`.

## How the code works

`eng_blogs.py`, in pipeline order:

- **`FEEDS`** — `{category: [(source name, feed url)]}`: Data &
  Analytics (Databricks, Confluent, Snowflake, AWS Big Data, dbt,
  DuckDB), Systems & Scale (Netflix, Uber, Meta, Cloudflare, Discord,
  Slack, Stripe, Dropbox), Product & ML Eng (Spotify, Airbnb, Pinterest,
  Canva). Named tuples so the digest can credit the source. Stripe's
  feed is the whole blog, not just engineering — the drop-pure-marketing
  prompt rule handles the mix.
- **`clean(html)`** — strips tags, collapses whitespace.
- **`fresh(entry, cutoff)`** — keeps entries newer than the lookback.
  Undated entries are **dropped** here (opposite of tech-news):
  corporate feeds are reliably dated, and an undated stale post
  repeating every day is worse than missing one. Uses the **publish**
  date, falling back to the updated date only when there's no publish
  date at all — otherwise a lightly edited old post gets a fresh
  `updated_parsed`, re-enters the 24h window, and reappears days later.
- **`gather_posts(lookback_hours)`** — fetches each feed over HTTP with
  an explicit **20-second timeout** (`requests.get`, then
  `feedparser.parse(resp.content)`) so one hanging host can't stall the
  run until the 15-min job timeout. Returns `(posts, failed)`: `posts`
  is `{category: [{source, title, summary, link}]}`; `failed` lists
  feeds that erred, returned a non-200, or yielded no usable entries
  this run. High-volume whole-blog feeds (Databricks, AWS Big Data,
  Stripe) get a raised `PER_FEED_LIMIT` (30 vs the default
  `ENTRIES_PER_FEED = 8`) so a busy day isn't truncated before the
  freshness check. `SUMMARY_CHARS = 400` — blogs have meaty abstracts,
  so they get more room than news entries.
- **`summarize(posts)`** — one model call. Unlike the news agents it
  includes EVERY post (volume is 0–5/day) unless one is pure marketing;
  each post gets "Source — title", 1–2 sentences with the technical
  takeaway for a data engineer, and the link. Deep-dives and postmortems
  rank above release notes.
- **`main()`** — reads `ENG_BLOGS_LOOKBACK_HOURS` *after* `load_dotenv`
  (so `.env` values work; a manual workflow run passes a wider window to
  catch up missed days), then gathers. Zero posts **and** no feed
  failures → print to the log and stay silent, unless `ENG_BLOGS_FORCE=1`.
  When feeds are down it sends even on a quiet day, appending a
  `⚠️ feeds not responding: …` footer so feed rot never hides behind the
  silence (mirrors release-radar's `failed` list; no cross-day state).
- **`agentlib.py`** (vendored) — `ask_llm()` one-shot model call;
  `send_telegram()` chunked sends.

## Design notes

- Silent-by-default: blogs post rarely, so a message almost always means
  there's something to read — the one exception is a feed-rot footer on a
  day that would otherwise be silent.
- Two crons + dedupe guard: backup at 20:07 IST delivers only if the
  19:07 primary was dropped or failed.

- **RAG corpus**: every gathered post is appended to
  `data/posts-YYYY-MM.jsonl` (deduped by link, committed back by the
  workflow) — the raw material for the planned ask-my-library RAG
  project. The corpus only exists from the day collection starts.

- **Full-text corpus**: each archived post now carries the post's
  readable text (fetched once, boilerplate stripped, 20k-char cap) —
  what the RAG project will actually embed. Blocked/paywalled hosts
  fall back to the abstract-only record.
- Tests run in CI on every push (`.github/workflows/tests.yml`).

## Ops

- Schedule: `.github/workflows/eng-blogs.yml` (`37 13 * * *` UTC = 19:07 IST; backup 20:07)
- Run now (custom window): `gh workflow run eng-blogs.yml -R astroboy1183/eng-blogs -f lookback_hours=72`
- Secrets (Actions): `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Local test: `ENG_BLOGS_FORCE=1 .venv/bin/python eng_blogs.py`
