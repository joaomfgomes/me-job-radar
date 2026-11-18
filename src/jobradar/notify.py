"""Telegram notifications via the Bot API (plain HTTP, no SDK needed).

A job is fully evaluated before it reaches here (see evaluation.py), so each
message is built once, with its verdict already known.

Two shapes of job message:
  * evaluated  -- the deep pass read the posting. The header leads with the
    CV coverage, followed by the interview-chance estimate and the list of
    must-have requirements met and missing. That brief is the point of the
    alert: it says why the job is worth opening.
  * unfiltered -- a fail-open path let the job through (description or model
    unavailable). No coverage is claimed, and a "Not filtered" warning says
    why, so an outage does not look like a run of bad matches.

Messages use parse_mode=HTML: everything that came from a job board or from
the model goes through escape(), and links are only built for http(s) URLs.
"""

from __future__ import annotations

import logging
from html import escape

import httpx

from jobradar.models import JobPosting
from jobradar.scoring import DeepScoreResult

log = logging.getLogger(__name__)

# Requirement names are short phrases, but a model can occasionally return a
# whole sentence -- cap them so one verbose requirement can't push a message
# past Telegram's 4096-character limit.
MAX_REQUIREMENT_CHARS = 60
MAX_REQUIREMENTS_SHOWN = 6
MAX_REASON_CHARS = 500

# Telegram rejects (HTTP 400) any message over 4096 characters. Longer text
# -- /status with many searches, /rejected with long reasons -- is split on
# line boundaries; every formatter here keeps its HTML tags within one line,
# so a split never lands inside an open tag.
MAX_MESSAGE_CHARS = 4000


def _split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        line = line[:limit]  # a single over-long line can't be split safely; cap it
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _link(url: str, label: str) -> str:
    """An <a> only for http(s) URLs. The URL comes from scraped markup, and
    an empty or odd-scheme href makes Telegram reject the whole message --
    which the pipeline would then retry (and re-score) every cycle."""
    if url.startswith(("https://", "http://")):
        return f'<a href="{escape(url)}">{label}</a>'
    return label


def _coverage_emoji(coverage_pct: int) -> str:
    if coverage_pct >= 80:
        return "🎯"
    if coverage_pct >= 60:
        return "🟡"
    return "🔴"


def _requirement_names(requirements, limit: int = MAX_REQUIREMENTS_SHOWN) -> str:
    shown = [escape(r.requirement[:MAX_REQUIREMENT_CHARS]) for r in requirements[:limit]]
    overflow = len(requirements) - len(shown)
    if overflow > 0:
        shown.append(f"+{overflow} more")
    return ", ".join(shown)


def _error_description(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("description", ""))[:200]
    except Exception:
        return ""


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: float = 10.0) -> None:
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._chat_id = chat_id
        # separate client for getUpdates: long-polling holds the connection
        # open for up to `timeout` seconds server-side, so it needs its own
        # (longer) httpx timeout independent of the quick send_text calls.
        self._client = httpx.Client(timeout=timeout)
        self._poll_client = httpx.Client(timeout=35.0)

    def close(self) -> None:
        self._client.close()
        self._poll_client.close()

    @property
    def chat_id(self) -> str:
        return self._chat_id

    @staticmethod
    def _format_job_text(
        posting: JobPosting,
        score: int | None = None,
        deep: DeepScoreResult | None = None,
        warnings: tuple[str, ...] = (),
    ) -> str:
        coverage = deep.coverage_pct if deep is not None and not deep.unscored else None

        if coverage is not None:
            header = f"{_coverage_emoji(coverage)} <b>{coverage}% match</b> — <b>{escape(posting.title)}</b>"
        else:
            header = f"🆕 <b>{escape(posting.title)}</b>"

        lines = [
            header,
            f"{escape(posting.company)} — {escape(posting.location)}",
            f"Posted: {escape(posting.posted_label)}",
            f"Source: {escape(posting.source)} · {escape(posting.search_name)}",
        ]

        lines += [f"⚠️ {escape(warning[:200])}" for warning in warnings]

        if deep is not None and not deep.unscored:
            if deep.interview_chance_pct is not None:
                # A model estimate, labelled as one (see scoring.py).
                chance = f"📈 Interview chance: ~{deep.interview_chance_pct}% (estimate)"
                if deep.insights is not None and deep.insights.work_mode != "unspecified":
                    chance += f" · {escape(deep.insights.work_mode)}"
                lines.append(chance)
                if deep.chance_reason:
                    lines.append(f"<i>{escape(deep.chance_reason[:MAX_REASON_CHARS])}</i>")
            if deep.matched:
                lines.append(f"✅ {_requirement_names(deep.matched)}")
            if deep.missing:
                lines.append(f"❌ {_requirement_names(deep.missing)}")
            if deep.reason:
                lines.append(f"<i>{escape(deep.reason[:MAX_REASON_CHARS])}</i>")
        elif score is not None:
            lines.append(f"Relevance: {score}/10")

        link = _link(posting.url, "Open posting")
        if link != "Open posting":
            lines.append(link)
        return "\n".join(lines)

    def send_job(
        self,
        posting: JobPosting,
        score: int | None = None,
        deep: DeepScoreResult | None = None,
        warnings: tuple[str, ...] = (),
    ) -> int | None:
        """Returns the Telegram message_id on success, or None on failure --
        the pipeline treats None as 'not delivered' and retries next cycle.
        Each warning is shown on its own line, flagged with ⚠️."""
        return self._send(self._format_job_text(posting, score, deep, warnings))

    def send_digest(self, rows: list[dict], threshold: int) -> bool:
        """One daily message for near-misses: jobs worth knowing about but
        below the alert bar. Its job is to make an over-tight threshold
        visible rather than silent."""
        if not rows:
            return True

        lines = [
            f"📋 <b>Near misses today</b> ({len(rows)}) — below the {threshold}% alert bar",
            "",
        ]
        for row in rows:
            lines.append(
                f'• <b>{row["coverage_pct"]}%</b> {_link(row["url"] or "", escape(row["title"]))} — '
                f'{escape(row["company"])} ({escape(row["source"])})'
            )
        lines.append("")
        lines.append("Use /rejected to see everything that was held back.")
        return self._send("\n".join(lines), preview=False) is not None

    def send_text(self, text: str) -> bool:
        return self._send(text, preview=False) is not None

    def _send(self, text: str, preview: bool = True) -> int | None:
        """Returns the message_id of the first chunk, or None if any chunk
        failed to send."""
        first_id: int | None = None
        for chunk in _split_message(text):
            message_id = self._send_one(chunk, preview)
            if message_id is None:
                return None
            if first_id is None:
                first_id = message_id
        return first_id

    def _send_one(self, text: str, preview: bool) -> int | None:
        try:
            resp = self._client.post(
                f"{self._base_url}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": not preview,
                },
            )
            resp.raise_for_status()
            return resp.json()["result"]["message_id"]
        except httpx.HTTPStatusError as exc:
            # Never log str(exc): it quotes the request URL, which contains
            # the bot token. Telegram's own `description` says what was wrong
            # (e.g. "can't parse entities") and is safe.
            log.error("Telegram send failed: HTTP %s %s",
                      exc.response.status_code, _error_description(exc.response))
            return None
        except httpx.HTTPError as exc:
            log.error("Telegram send failed: %s", type(exc).__name__)
            return None
        except (ValueError, KeyError, TypeError) as exc:
            # 200 OK with a body that isn't the expected JSON shape.
            log.error("Telegram send returned an unexpected response: %s", type(exc).__name__)
            return None

    def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict]:
        """Long-poll for new messages. Blocks server-side up to `timeout`
        seconds, so a single call is the entire wait -- no local sleep loop.

        Raises httpx.HTTPStatusError (including 409, meaning another process
        is already polling this bot) and httpx.HTTPError (network issues) --
        callers decide how to handle each.
        """
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        resp = self._poll_client.get(f"{self._base_url}/getUpdates", params=params)
        resp.raise_for_status()
        return resp.json().get("result", [])
