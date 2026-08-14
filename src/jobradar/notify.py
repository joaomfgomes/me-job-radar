"""Telegram notifications via the Bot API (sendMessage, HTTP POST, no SDK needed)."""

from __future__ import annotations

import logging
from html import escape

import httpx

from jobradar.models import JobPosting

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: float = 10.0) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

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
                self._url,
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
