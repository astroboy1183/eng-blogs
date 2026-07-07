# eng-blogs

Evening engineering-blog digest → Telegram, ~19:07 IST daily via GitHub
Actions.

15 company engineering blogs — data platforms, systems at scale, product
& ML engineering — filtered and summarized for a data engineer. Silent
on days with no new posts. One agent, one task, one bot:
`@jayanth_eng_blogs_bot`.

## How the code works

`eng_blogs.py`, in pipeline order:

- **`FEEDS`** — `{category: [(source name, feed url)]}`: Data &
  Analytics (Databricks, Confluent, Snowflake, AWS Big Data, dbt,
  DuckDB), Systems & Scale (Netflix, Uber, Meta, Cloudflare, Discord,
  Slack), Product & ML Eng (Spotify, Airbnb, Pinterest). Named tuples so
  the digest can credit the source.
- **`clean(html)`** — strips tags, collapses whitespace.
- **`fresh(entry, cutoff)`** — keeps entries newer than the lookback.
  Undated entries are **dropped** here (opposite of tech-news):
  corporate feeds are reliably dated, and an undated stale post
  repeating every day is worse than missing one.
- **`gather_posts(lookback_hours)`** — per-feed `try/except`, collecting
  `{source, title, summary, link}`. `SUMMARY_CHARS = 400` — blogs have
  meaty abstracts, so they get more room than news entries.
- **`summarize(posts)`** — one model call. Unlike the news agents it
  includes EVERY post (volume is 0–5/day) unless one is pure marketing;
  each post gets "Source — title", 1–2 sentences with the technical
  takeaway for a data engineer, and the link. Deep-dives and postmortems
  rank above release notes.
- **`main()`** — reads `ENG_BLOGS_LOOKBACK_HOURS` *after* `load_dotenv`
  (so `.env` values work; a manual workflow run passes a wider window to
  catch up missed days), then gathers. Zero posts → print to the log and
  stay silent, unless `ENG_BLOGS_FORCE=1`.
- **`agentlib.py`** (vendored) — `ask_llm()` one-shot model call;
  `send_telegram()` chunked sends.

## Design notes

- Silent-by-default: blogs post rarely, so a message always means
  there's something to read.
- Two crons + dedupe guard: backup at 20:07 IST delivers only if the
  19:07 primary was dropped or failed.

## Ops

- Schedule: `.github/workflows/eng-blogs.yml` (`37 13 * * *` UTC = 19:07 IST; backup 20:07)
- Run now (custom window): `gh workflow run eng-blogs.yml -R astroboy1183/eng-blogs -f lookback_hours=72`
- Secrets (Actions): `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Local test: `ENG_BLOGS_FORCE=1 .venv/bin/python eng_blogs.py`
