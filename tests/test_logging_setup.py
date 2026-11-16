"""The log must never contain the bot token or API key, whoever logs it."""

import logging
import sys

import httpx

from jobradar.logging_setup import RedactingFormatter, redact, setup_logging

TOKEN = "123456789:AAExampleExampleExampleExampleExample"


def test_redacts_bot_token_in_api_url_even_when_not_registered():
    line = f'HTTP Request: POST https://api.telegram.org/bot{TOKEN}/sendMessage "HTTP/1.1 200 OK"'
    out = redact(line)
    assert TOKEN not in out
    assert "/bot<redacted>/sendMessage" in out


def test_redacts_registered_secrets_and_openai_style_keys():
    assert redact("key=hunter2-value", secrets=("hunter2-value",)) == "key=<redacted>"
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz" not in redact("Bearer sk-proj-abcdefghijklmnopqrstuvwxyz")


def test_formatter_redacts_tracebacks_too():
    request = httpx.Request("GET", f"https://api.telegram.org/bot{TOKEN}/getUpdates")
    try:
        httpx.Response(409, request=request).raise_for_status()
    except httpx.HTTPStatusError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
    out = RedactingFormatter(secrets=(TOKEN,)).format(record)
    assert "HTTPStatusError" in out
    assert TOKEN not in out


def test_setup_logging_writes_redacted_owner_only_file_without_http_noise(tmp_path):
    log_file = tmp_path / "app.log"
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        setup_logging(str(log_file), secrets=(TOKEN,))
        logging.getLogger("jobradar.test").info("calling https://api.telegram.org/bot%s/getUpdates", TOKEN)
        logging.getLogger("httpx").info("per-request noise")
        for handler in root.handlers:
            handler.flush()
        content = log_file.read_text()
        assert TOKEN not in content
        assert "/bot<redacted>/getUpdates" in content
        assert "per-request noise" not in content
        assert (log_file.stat().st_mode & 0o777) == 0o600
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        for name in ("httpx", "httpcore", "httpx2", "httpcore2", "apscheduler.executors.default"):
            logging.getLogger(name).setLevel(logging.NOTSET)
