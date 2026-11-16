"""Logging configuration: bounded log files that never contain secrets.

Two problems this exists to prevent, both observed in production:

  * The Telegram Bot API carries the bot token in the URL path
    (https://api.telegram.org/bot<token>/sendMessage). httpx logs every
    request URL at INFO, and its exception messages quote the URL too -- so
    a root logger at INFO wrote the token to disk on every single
    getUpdates long-poll. RedactingFormatter scrubs it from everything that
    reaches a handler (message AND traceback), and httpx/httpcore are
    additionally raised to WARNING since their per-request lines are noise
    anyway.
  * A plain FileHandler grows forever (tens of MB within weeks at one
    long-poll line every 25s). RotatingFileHandler caps it.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# Third-party loggers whose INFO output is one line per HTTP request.
_NOISY_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2", "apscheduler.executors.default")

# Shapes of secrets that can show up in URLs / error messages even when the
# exact value wasn't passed to setup_logging(): Telegram bot tokens in a Bot
# API path, and "sk-..." API keys (Anthropic's start with sk-ant-).
_SECRET_PATTERNS = (
    (re.compile(r"/bot\d+:[A-Za-z0-9_-]+"), "/bot<redacted>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "sk-<redacted>"),
)


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Scrubs secrets from the fully formatted record -- message, args and
    traceback alike -- so it holds no matter which library did the logging."""

    def __init__(self, fmt: str = LOG_FORMAT, secrets: tuple[str, ...] = ()) -> None:
        super().__init__(fmt)
        self._secrets = tuple(s for s in secrets if s)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)


def setup_logging(log_path: str, secrets: tuple[str, ...] = ()) -> None:
    formatter = RedactingFormatter(secrets=secrets)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    handlers: list[logging.Handler] = [file_handler]
    # Only mirror to stdout when a human is watching. Under launchd/nohup
    # stdout is redirected to a file nothing rotates, which would just be a
    # second, unbounded copy of the log.
    if sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    for handler in handlers:
        handler.setFormatter(formatter)

    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # Logs name the jobs/companies being targeted -- owner-only, best effort.
    try:
        os.chmod(log_path, 0o600)
    except OSError:
        pass
