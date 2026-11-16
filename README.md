# me-job-radar

Near-real-time job-posting alerts from LinkedIn and Wellfound, scored against
your CV and delivered to Telegram within minutes of a posting going live --
so you can apply while it's still fresh, and only hear about the ones worth
opening.

The bot introduces itself as **job-radar** in Telegram; the Python package is
`jobradar`.

## How it works

One long-running process, two threads:

```
config.yaml   profile.yaml   .env            state.db (SQLite, WAL)
     │             │           │                    ▲
     ▼             ▼           ▼                    │
┌──────────────────────────────────────────────────────────────┐
│ main.py                                                      │
│                                                              │
│  scheduler thread (APScheduler)                              │
│    every ~10 min (± jitter): Pipeline.run_cycle              │
│      for each core search + a rotating batch of secondary:   │
│        fetch → drop already-seen → evaluate → alert → record │
│    daily at digest_hour: near-miss digest                    │
│                                                              │
│  main thread                                                 │
│    CommandListener: long-polls Telegram, answers /commands   │
└──────────────────────────────────────────────────────────────┘
          │                    │                    │
          ▼                    ▼                    ▼
  LinkedIn / Wellfound     OpenAI API        Telegram Bot API
  (no login, no cookies)   (scoring)         (alerts + commands)
```

No LinkedIn login, no cookies, no session -- so there's no account to get
banned. See **Limitations & risks** for what that trade-off means.

### The evaluation cascade

A new posting is fully evaluated *before* it is alerted. Stages run cheapest
first, and every stage **fails open**: an infrastructure problem lets the job
through rather than silently swallowing it.

| # | Stage | Cost | Rejects |
|---|---|---|---|
| 1 | Prefilter (`prefilter.py`) | free | Title/employer dealbreakers, seniority terms above your ceiling |
| 2 | Stated experience | free | Boards that state a years floor above `max_years_experience` |
| 3 | Description fetch (`sources/linkedin_jd.py`) | 1 request | Nothing -- a failed fetch alerts the job unfiltered, flagged as such |
| 4 | Language check (`lang_filter.py`) | free | Descriptions not written in a language you read |
| 5 | Title score (`title_model`) | cheap LLM call | Scores below `deep_score_threshold` |
| 6 | Deep score (`deep_model`) | stronger LLM call | Years required above your ceiling, not junior-appropriate, or requirement coverage below `min_coverage_pct` |

Coverage is computed in Python from the list of must-have requirements the
model returns, not asked of the model as a number, so the threshold is
auditable. Jobs held back are still recorded: near-misses go into a daily
digest and `/rejected` shows the rest.

Scoring is optional. Without `profile.yaml` or `OPENAI_API_KEY`, every new
posting is alerted, as in v1.

## Setup

1. **Install dependencies** (using [uv](https://docs.astral.sh/uv/)):

   ```bash
   uv sync
   ```

2. **Create a Telegram bot and get your chat ID.** See [Telegram setup](#telegram-setup) below.

3. **Configure secrets:**

   ```bash
   cp .env.example .env && chmod 600 .env
   ```

   Then fill in `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and, for scoring,
   `OPENAI_API_KEY`.

4. **Configure your searches:**

   ```bash
   cp config.example.yaml config.yaml
   ```

   `search_matrix` generates searches from keywords × locations per board.
   Core keywords are polled every cycle; secondary ones rotate
   `secondary_batch_size` at a time. Hand-written `searches` still work for
   one-offs. Values are validated at startup.

5. **Describe yourself (optional, enables scoring):**

   ```bash
   cp profile.example.yaml profile.yaml
   ```

   To give the deep pass your real project experience to cite, put your CV
   at `cv.pdf` and run:

   ```bash
   uv run python -m jobradar.cv_ingest
   ```

   This writes `profile.generated.yaml`. Review it; `profile.yaml` stays
   hand-owned.

6. **Run it:**

   ```bash
   uv run python -m jobradar.main
   ```

   The first run of each search seeds the database silently (no alerts for
   jobs already in the time window). You get a "🚀 job-radar started" ping
   immediately; real alerts start from the next genuinely new posting.

`config.yaml`, `profile.yaml`, `profile.generated.yaml`, `cv.pdf`, `.env`,
`state.db` and logs are all gitignored.

## Telegram setup

1. Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`,
   follow the prompts. It gives you a **bot token**
   (looks like `123456789:AAF...`).
2. Send your new bot any message (e.g. "hi") so it has a chat to talk in.
3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
   -- find `"chat":{"id":123456789,...}` in the response. That number is
   your **chat ID**.
4. Put both in `.env`.

The bot only answers the configured chat ID. Treat the token as a password:
anyone holding it can read and send as your bot. If it leaks, `/revoke` it in
BotFather.

## Commands

| Command | What it does |
|---|---|
| `/jobs` | Last 10 jobs recorded |
| `/rejected` | Jobs that were scored but held back, and why |
| `/countries` | List locations and whether each is active |
| `/countries on\|off <name>` | Start or stop searching a location |
| `/countries add <name>` | Validate a new location against each board, then start searching it |
| `/countries remove <name>` | Drop a location added via `/countries add` |
| `/status` | Uptime, poll health, per-search counts |
| `/help` | Command list |

Changes made through `/countries` live in `state.db` and take effect on the
next poll cycle; `config.yaml` is never rewritten.

## Running it unattended

On macOS, a LaunchAgent that wraps the command in `caffeinate -is`, with
`KeepAlive` and `RunAtLoad`, keeps it running across sleep, crashes and
reboots. Set `WorkingDirectory` to this repository -- the app reads
`config.yaml`, `.env` and `profile.yaml` relative to it, so **moving or
renaming the folder means updating the plist** (and running
`uv sync --reinstall` to refresh the virtualenv's script paths).

Only one process may long-poll a given bot; a second one gets HTTP 409.

## Logging

`jobradar.log` rotates at 5 MB with 3 backups and is created owner-only.
Secrets are redacted from every record, including tracebacks -- the Bot API
carries the token in the request URL, so this is not optional. Per-request
HTTP logging is silenced. Output is mirrored to stdout only when attached to
a terminal, so a service manager's stdout file receives uncaught tracebacks
and nothing else.

## Testing

```bash
uv run --extra dev pytest
```

Everything runs offline. Parsers are tested against real captured responses
in `tests/fixtures/`, which is what matters most here: the parsers are what
break when a board changes its markup. The OpenAI and Telegram clients are
stubbed.

## Limitations & risks

- **Not an official API.** Both boards are read through unauthenticated,
  undocumented endpoints that can change or disappear without notice. The
  `JobSource` interface (`src/jobradar/sources/base.py`) exists so another
  source is a new class, not a rewrite.
- **Rate limiting.** Polite polling (10-minute interval, jitter, sequential
  requests spaced at least 1.2 s apart) keeps volume low, but LinkedIn can
  still 429/999 an IP. The pipeline alerts you after repeated failures
  rather than failing silently.
- **Silent breakage is the real risk**, not blocking. If a board changes its
  markup, the parser can start returning zero results without erroring.
  Consecutive empty cycles are tracked per search and trigger a Telegram
  warning.
- **Fail-open means noise when scoring is down.** If the OpenAI call fails
  (outage, exhausted credits), jobs are alerted unfiltered rather than
  dropped. A sudden flood of irrelevant alerts usually means scoring is
  unavailable -- check the log.
- **Scoring is a model's judgment.** The prefilter and the years ceiling are
  deterministic; requirement matching is not. Job descriptions are passed to
  the model as-is, so a posting could in principle be written to game its
  score; the worst case is one irrelevant alert.
- **`state.db` grows without retention.** A few MB per month at typical
  volume. It is safe to delete -- the next run re-seeds silently.
- **Legal/ToS gray area.** This reads publicly visible pages without
  authenticating. Low personal-use volume keeps it in hobby-project
  territory, but it isn't sanctioned by either board -- don't scale it up or
  redistribute scraped data.

## Roadmap

- [x] LLM relevance scoring against `profile.yaml`, enforced before alerting.
- [x] Second job board (Wellfound).
- [ ] CI (tests, lint, dependency audit) on pull requests.
- [ ] Deploy to a small always-on VPS (systemd service) for uptime beyond a
      laptop's battery/sleep cycle.
- [ ] Retention policy and nightly backup for `state.db`.

## Project structure

```
src/jobradar/
├── main.py                 # entry point: wiring, scheduler, signal handling
├── config.py               # config.yaml + .env loader, search matrix, validation
├── logging_setup.py        # rotating, secret-redacting logging
├── models.py               # JobPosting, SearchConfig
├── profile.py              # profile.yaml loader
├── pipeline.py             # fetch → dedup → evaluate → notify → record; health
├── prefilter.py            # deterministic dealbreakers
├── lang_filter.py          # description language check
├── scoring.py              # title + deep scoring via OpenAI structured outputs
├── cv_ingest.py            # cv.pdf → profile.generated.yaml (manual tool)
├── location_validation.py  # live check that a location resolves on a board
├── store.py                # SQLite: seen jobs, scores, location prefs
├── notify.py               # Telegram sendMessage / getUpdates
├── listener.py             # command long-poll loop
├── commands.py             # command handlers (pure, no network)
└── sources/
    ├── base.py             # JobSource protocol
    ├── linkedin.py         # LinkedIn guest search
    ├── linkedin_jd.py      # LinkedIn guest job description
    └── wellfound.py        # Wellfound (__NEXT_DATA__ Apollo cache)
tests/
├── fixtures/               # real captured responses
└── test_*.py               # offline tests, one file per module
```
