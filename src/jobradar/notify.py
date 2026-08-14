"""Telegram notifications via the Bot API (sendMessage, HTTP POST, no SDK needed)."""

from __future__ import annotations

import logging
from html import escape

import httpx

from jobradar.models import JobPosting

log = logging.getLogger(__name__)


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

    def send_job(self, posting: JobPosting) -> bool:
        text = (
            f"🆕 <b>{escape(posting.title)}</b>\n"
            f"{escape(posting.company)} — {escape(posting.location)}\n"
            f"Posted: {escape(posting.posted_label)}\n"
            f"Search: {escape(posting.search_name)}\n"
            f'<a href="{escape(posting.url)}">Open on LinkedIn</a>'
        )
        return self.send_text(text)

    def send_text(self, text: str) -> bool:
        try:
            resp = self._client.post(
                f"{self._base_url}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
            )
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.error("Telegram send failed: %s", exc)
            return False

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
