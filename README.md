# job-radar

Near-real-time LinkedIn job-posting alerts, delivered to Telegram within
minutes of a new posting going live -- so you can apply while it's still
fresh, not after it's buried under 200 other applicants.

## How it works

```
config.yaml (saved searches)          state.db (SQLite: seen job IDs)
        │                                       ▲
        ▼                                       │
┌───────────────────────────────────────────────────┐
│  main.py -- long-running scheduler (APScheduler)   │
│                                                     │
│  every ~10 min (± jitter):                         │
│    for each saved search:                          │
│      1. fetch LinkedIn's public guest job-search    │
│         endpoint, filtered to "posted in last 30m"  │
│      2. parse job cards (BeautifulSoup)              │
│      3. drop IDs already in state.db                │
│      4. send a Telegram alert per new job            │
│      5. mark it seen (only after a successful send)  │
└───────────────────────────────────────────────────┘
                        │
                        ▼
               Telegram Bot API
```

No LinkedIn login, no cookies, no session -- so there's no account to get
banned. It calls the same unauthenticated endpoint LinkedIn's own logged-out
job search page uses. See **Limitations & risks** below for what that
trade-off actually means.

## Setup

1. **Install dependencies** (using [uv](https://docs.astral.sh/uv/)):

   ```bash
   uv sync
   ```

2. **Create a Telegram bot and get your chat ID.** See [Telegram setup](#telegram-setup) below.

3. **Configure secrets:**

   ```bash
   cp .env.example .env
   # edit .env: TELEGRAM_BOT_TOKEN=..., TELEGRAM_CHAT_ID=...
   ```

4. **Configure your searches:**

   ```bash
   cp config.example.yaml config.yaml
   # edit config.yaml: keywords, location, workplace_type, experience_levels
   ```

5. **Run it:**

   ```bash
   uv run python -m jobradar.main
   ```

   First run per search seeds the database silently (no alerts for jobs
   already in the time window) -- you'll get a "🚀 job-radar started" ping
   immediately, then real alerts start from the next genuinely new posting.

## Telegram setup

1. Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`,
   follow the prompts. It gives you a **bot token**
   (looks like `123456789:AAF...`).
2. Send your new bot any message (e.g. "hi") so it has a chat to talk in.
3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
   -- find `"chat":{"id":123456789,...}` in the response. That number is
   your **chat ID**.
4. Put both in `.env`.

## Testing

```bash
uv run pytest
```

Parser tests run entirely offline against `tests/fixtures/search_response.html`,
a real response captured from LinkedIn. This is the one test suite that
actually matters here: the parser is what breaks when LinkedIn changes its
markup, and testing against a saved real fixture means you can verify the
parser without hitting LinkedIn (or waiting for a live job to test against).

## Limitations & risks

- **Not an official API.** The guest endpoint is unauthenticated and
  publicly reachable, but undocumented and unsupported. It can change or
  disappear without notice. The `JobSource` interface
  (`src/jobradar/sources/base.py`) exists so a fallback source (e.g. Adzuna,
  JSearch, a company career-page RSS feed) is a new class, not a rewrite.
- **Rate limiting.** Polite polling (10-min interval, jitter, sequential
  requests, realistic headers) keeps request volume far below anything
  that normally triggers blocking, but LinkedIn can still 429/999 an IP.
  The pipeline backs off and alerts you after repeated failures rather than
  failing silently.
- **Silent breakage is the real risk**, not blocking. If LinkedIn changes
  its HTML structure, the parser could start returning zero results without
  erroring. `Pipeline._handle_empty` tracks consecutive empty cycles per
  search and sends a Telegram warning if a search goes ~4 hours without a
  single result -- a heartbeat, not a guarantee.
- **False positives/negatives in matching** come from LinkedIn's own
  keyword search, not this tool -- broaden your `keywords`/`location` and
  let dedup handle the extra noise rather than narrowing and risking missed
  jobs.
- **Legal/ToS gray area.** This scrapes publicly visible pages without
  authenticating. Low personal-use volume keeps this in "hobby project"
  territory, but it isn't sanctioned by LinkedIn -- don't scale this up or
  redistribute scraped data.

## Roadmap

- [ ] **LLM relevance scoring** (phase 2): score each new posting against a
      `profile.yaml` of your skills/target roles using Claude, and only
      notify above a threshold -- cuts noise from broad-but-safe keyword
      searches.
- [ ] Deploy to a small always-on VPS (systemd service) for 24/7 uptime
      beyond a laptop's battery/sleep cycle.
- [ ] Nightly SQLite backup / Litestream if state.db loss ever matters more
      than the first-run-seeding safety net already covers.

## Project structure

```
src/jobradar/
├── models.py          # JobPosting, SearchConfig dataclasses
├── config.py           # config.yaml + .env loader
├── store.py             # SQLite dedup (+ first-run seeding)
├── notify.py             # Telegram sendMessage wrapper
├── pipeline.py             # fetch -> dedup -> notify -> mark_seen, backoff, heartbeat
├── main.py                  # scheduler entry point
└── sources/
    ├── base.py                # JobSource interface (pluggable)
    └── linkedin.py              # LinkedIn guest-endpoint implementation
tests/
├── fixtures/search_response.html   # real captured response
└── test_linkedin_parser.py           # offline parser tests
```
