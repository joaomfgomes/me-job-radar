# me-job-radar

Near-real-time job-posting alerts from LinkedIn and Wellfound, matched against
your CV by Claude and delivered to Telegram within minutes of a posting going
live -- so you can apply while it's still fresh, and only hear about the ones
worth opening. Every posting it reads also feeds a small market database:
which skills are being asked for, and which of them your CV does not show.

New here? [docs/GUIA.md](docs/GUIA.md) walks through the code in Portuguese,
and [docs/MELHORIAS.md](docs/MELHORIAS.md) lists what is still missing.

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
  LinkedIn / Wellfound     Claude API        Telegram Bot API
  (no login, no cookies)   (scoring)         (alerts + commands)
```

No LinkedIn login, no cookies, no session -- so there's no account to get
banned. See **Limitations & risks** for what that trade-off means.

### The evaluation cascade

A new posting is fully evaluated *before* it is alerted
([evaluation.py](src/jobradar/evaluation.py)). Stages run cheapest first, and
every stage that depends on something external **fails open**: an outage lets
the job through, labelled as not filtered, rather than silently swallowing it.

| # | Stage | Cost | Rejects |
|---|---|---|---|
| 1 | Prefilter (`prefilter.py`) | free | Title/employer dealbreakers, seniority terms above your ceiling |
| 2 | Stated experience | free | Boards that state a years floor above `max_years_experience` |
| 3 | Title score | 1 small Claude call | Roles that are plainly off-target (below `deep_score_threshold`) |
| 4 | Description fetch (`sources/linkedin_jd.py`) | 1 request | Nothing -- a failed fetch alerts the job, flagged as unverified |
| 5 | Language check (`lang_filter.py`) | free | Descriptions not written in a language you read |
| 6 | Deep score | 1 Claude call | Years above your ceiling, not junior-appropriate, location does not fit `location_preferences`, or requirement coverage below `min_coverage_pct` |

The title score runs before the description is fetched so that irrelevant
titles never cost a LinkedIn request.

The deep pass returns three things in one call ([scoring.py](src/jobradar/scoring.py)):

- **The match.** The posting's must-have requirements, each marked met or
  unmet against your CV with the evidence. Coverage is computed in Python
  from that list, not asked of the model as a number, so it is auditable.
- **The chance.** An estimated probability of getting an interview, anchored
  to a rubric in the prompt. It is a model estimate -- shown in the alert,
  never used to decide whether to alert.
- **Market data.** Skills (required or nice-to-have, on your CV or not), role
  family, seniority, work mode, contract type and stated salary. Stored for
  `/market` and `/gaps`.

Jobs held back are still recorded: near-misses go into a daily digest,
`/rejected` shows the rest, and `/stats` counts where each one stopped.

Filtering degrades in steps. Without `profile.yaml` every new posting is
alerted. With a profile but no `ANTHROPIC_API_KEY`, only the prefilter runs.

### Models and cost

Both passes default to **Claude Haiku 5.5** (`claude-haiku-5-5`), set by
`title_model` / `deep_model` in `config.yaml`. The instructions and your
profile/CV are sent as a cached system prompt; only the posting changes per
call. At Haiku's prices a deep read of one posting costs a small fraction of
a cent. To buy a stronger judgment on the deep pass only, set `deep_model`
to `claude-sonnet-5-5`.

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
   `ANTHROPIC_API_KEY` (a Claude API key, starting with `sk-ant-`).

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

   `location_preferences` is where you say where you can work ("on-site
   only in Porto; remote from Portugal"). It is the only place location is
   enforced: LinkedIn's guest search ignores its own remote filter.

   To give the deep pass your real CV to match against, put it at `cv.pdf`
   and run:

   ```bash
   uv run python -m jobradar.cv_ingest
   ```

   This writes `profile.generated.yaml` (experience, projects, skills,
   certifications, education, languages), which the deep pass reads on every
   call. Review it; `profile.yaml` stays hand-owned. Re-run it whenever the
   CV changes.

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
| `/stats [days]` | The funnel: how many jobs were seen, where each was filtered, average coverage and interview chance, best matches |
| `/market [days]` | What postings ask for: most requested skills (✅ on your CV, ❌ not), work mode, seniority, roles, stated salary |
| `/gaps [days]` | The most requested skills your CV does not show |
| `/status` | Uptime, poll health, per-search counts |
| `/help` | Command list |

`[days]` defaults to 30. Changes made through `/countries` live in `state.db`
and take effect on the next poll cycle; `config.yaml` is never rewritten. If
a location exists in both, `config.yaml` wins.

## Statistics and market data

Every posting records where its evaluation ended (`seen_jobs.outcome`), and
every posting read in full adds a row to `job_insights` and one row per skill
to `job_skills`. The three commands above are views over those tables; the
data is plain SQLite, so it can also be queried directly:

```bash
sqlite3 state.db "SELECT name, COUNT(*) FROM job_skills WHERE candidate_has = 0 GROUP BY skill_key ORDER BY 2 DESC LIMIT 20"
```

Two things to keep in mind when reading it. Only roles whose title looked
relevant are read in full, so the market view describes *your target
market*, not all jobs. And "on your CV" means what `profile.yaml` and
`profile.generated.yaml` show -- a skill you have but never wrote down
counts as a gap until you add it.

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

```bash
uv run --extra dev ruff check src tests
```

Everything runs offline. Parsers are tested against real captured responses
in `tests/fixtures/`, which is what matters most here: the parsers are what
break when a board changes its markup. The Claude and Telegram clients are
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
- **Fail-open means noise when scoring is down.** If the Claude call fails
  (outage, bad key, exhausted credits), jobs are alerted with a "Not
  filtered" line rather than dropped, and `/stats` says so.
- **The interview chance is an estimate, not a measurement.** The model
  follows a rubric, which makes its numbers consistent with each other, but
  nothing has calibrated them against real outcomes yet.
- **"Remote" cannot be searched for.** LinkedIn's guest search ignores the
  workplace filter, so remote roles are found by searching wide (a country,
  the EU) and letting the deep pass reject what does not fit
  `location_preferences`. A posting that does not say where it can be done
  is let through.
- **Scoring is a model's judgment.** The prefilter and the years ceiling are
  deterministic; requirement matching is not. Descriptions are passed to the
  model as data with an instruction to ignore instructions inside them, but
  a posting could still be written to game its score; the worst case is one
  irrelevant alert.
- **`state.db` grows without retention.** A few MB per month at typical
  volume. It is safe to delete -- the next run re-seeds silently.
- **Legal/ToS gray area.** This reads publicly visible pages without
  authenticating. Low personal-use volume keeps it in hobby-project
  territory, but it isn't sanctioned by either board -- don't scale it up or
  redistribute scraped data.

## Roadmap

- [x] LLM relevance scoring against `profile.yaml`, enforced before alerting.
- [x] Second job board (Wellfound).
- [x] Interview-chance estimate, funnel statistics and a market/skill-gap database.
- [ ] Record applications and their outcomes, to calibrate the chance estimate.
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
├── pipeline.py             # one poll cycle: fetch → dedup → evaluate → notify → record
├── evaluation.py           # the cascade that decides alert vs. hold back
├── llm.py                  # the only module that calls the Claude API
├── prefilter.py            # deterministic dealbreakers
├── lang_filter.py          # description language check
├── scoring.py              # prompts and schemas: match, interview chance, market data
├── cv_ingest.py            # cv.pdf → profile.generated.yaml (manual tool)
├── location_validation.py  # live check that a location resolves on a board
├── store.py                # SQLite: seen jobs, verdicts, statistics, market data, locations
├── notify.py               # Telegram sendMessage / getUpdates
├── listener.py             # command long-poll loop
├── commands.py             # command handlers (pure, no network)
└── sources/
    ├── base.py             # JobSource protocol, RateLimitedError
    ├── linkedin.py         # LinkedIn guest search
    ├── linkedin_jd.py      # LinkedIn guest job description
    └── wellfound.py        # Wellfound (__NEXT_DATA__ Apollo cache)
docs/
├── GUIA.md                 # code walkthrough (Portuguese)
└── MELHORIAS.md            # what could be better / is still missing (Portuguese)
tests/
├── fixtures/               # real captured responses
└── test_*.py               # offline tests, one file per module
```
