"""Offline tests for TelegramNotifier message building. httpx calls are
stubbed via monkeypatch -- no real network, no real bot token needed.
"""

import httpx
import pytest

from jobradar.models import JobPosting
from jobradar.notify import TelegramNotifier, _coverage_emoji
from tests.conftest import make_deep


@pytest.fixture
def notifier():
    n = TelegramNotifier(bot_token="x", chat_id="y")
    yield n
    n.close()


def capture(notifier, monkeypatch, message_id=42):
    captured = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"result": {"message_id": message_id}}

    def fake_post(url, json):
        captured["url"], captured["json"] = url, json
        return _Resp()

    monkeypatch.setattr(notifier._client, "post", fake_post)
    return captured


def make_posting(**kw):
    base = dict(job_id="1", title="AI Engineer", company="Acme", location="Remote",
                url="https://example.com/1", posted_label="1h ago", search_name="Search A")
    return JobPosting(**{**base, **kw})


def test_coverage_emoji_bands():
    assert _coverage_emoji(100) == "🎯"
    assert _coverage_emoji(80) == "🎯"
    assert _coverage_emoji(79) == "🟡"
    assert _coverage_emoji(60) == "🟡"
    assert _coverage_emoji(59) == "🔴"


def test_send_job_with_deep_result_leads_with_coverage_and_brief(notifier, monkeypatch):
    captured = capture(notifier, monkeypatch)
    deep = make_deep(met=8, unmet=2, reason="Matches RAG and LangGraph experience.")

    assert notifier.send_job(make_posting(), score=9, deep=deep) == 42

    text = captured["json"]["text"]
    assert "80% match" in text and "🎯" in text
    assert "✅" in text and "req-met-0" in text
    assert "❌" in text and "req-missing-0" in text
    assert "Matches RAG and LangGraph experience." in text
    assert "🆕" not in text


def test_send_job_with_language_unverified_shows_warning(notifier, monkeypatch):
    """The exact failure mode found in production: a JD fetch failure skips
    the language check entirely, and the alert must say so plainly rather
    than look like a normally-screened job."""
    captured = capture(notifier, monkeypatch)

    notifier.send_job(make_posting(), score=None, deep=None, language_unverified=True)

    text = captured["json"]["text"]
    assert "language not verified" in text
    assert "⚠️" in text


def test_send_job_without_language_unverified_omits_warning(notifier, monkeypatch):
    captured = capture(notifier, monkeypatch)
    notifier.send_job(make_posting(), deep=make_deep())
    assert "language not verified" not in captured["json"]["text"]


def test_send_job_without_deep_result_makes_no_coverage_claim(notifier, monkeypatch):
    """Fail-open paths let a job through unevaluated -- the message must not
    imply a match percentage it doesn't have."""
    captured = capture(notifier, monkeypatch)

    notifier.send_job(make_posting(), score=None, deep=None)

    text = captured["json"]["text"]
    assert "🆕" in text
    assert "% match" not in text
    assert "✅" not in text


def test_send_job_with_unscored_deep_result_falls_back_to_plain_header(notifier, monkeypatch):
    captured = capture(notifier, monkeypatch)
    deep = make_deep(unscored=True, score=None, reason="deep scoring unavailable: timeout")

    notifier.send_job(make_posting(), score=5, deep=deep)

    text = captured["json"]["text"]
    assert "🆕" in text
    assert "% match" not in text
    assert "Relevance: 5/10" in text


def test_send_job_shows_source_so_boards_are_distinguishable(notifier, monkeypatch):
    captured = capture(notifier, monkeypatch)
    notifier.send_job(make_posting(source="wellfound"), deep=make_deep())
    assert "wellfound" in captured["json"]["text"]


def test_send_job_omits_missing_line_when_everything_matches(notifier, monkeypatch):
    captured = capture(notifier, monkeypatch)
    notifier.send_job(make_posting(), deep=make_deep(met=5, unmet=0))
    text = captured["json"]["text"]
    assert "100% match" in text
    assert "❌" not in text


def test_send_job_escapes_html_in_untrusted_fields(notifier, monkeypatch):
    captured = capture(notifier, monkeypatch)
    notifier.send_job(make_posting(title="<script>alert(1)</script>"), deep=make_deep())
    assert "<script>" not in captured["json"]["text"]
    assert "&lt;script&gt;" in captured["json"]["text"]


def test_send_job_returns_none_on_failure(notifier, monkeypatch):
    monkeypatch.setattr(notifier._client, "post",
                        lambda url, json: (_ for _ in ()).throw(httpx.HTTPError("boom")))
    assert notifier.send_job(make_posting()) is None


def test_send_digest_lists_rows_with_their_own_urls(notifier, monkeypatch):
    captured = capture(notifier, monkeypatch)
    rows = [
        {"job_id": "wellfound:5", "title": "ML Engineer", "company": "Beta",
         "coverage_pct": 72, "url": "https://wellfound.com/jobs/5-ml", "source": "wellfound"},
    ]

    assert notifier.send_digest(rows, threshold=80) is True

    text = captured["json"]["text"]
    assert "72%" in text
    # the URL must come from the row, not be reconstructed as a LinkedIn one
    assert "https://wellfound.com/jobs/5-ml" in text
    assert "linkedin.com" not in text
    assert "80% alert bar" in text


def test_send_digest_with_no_rows_sends_nothing(notifier, monkeypatch):
    captured = capture(notifier, monkeypatch)
    assert notifier.send_digest([], threshold=80) is True
    assert captured == {}


def test_send_failure_log_never_contains_the_bot_token(monkeypatch, caplog):
    token = "123456789:AAExampleExampleExampleExampleExample"
    n = TelegramNotifier(bot_token=token, chat_id="y")

    def fake_post(url, json):
        resp = httpx.Response(400, json={"description": "Bad Request: can't parse entities"},
                              request=httpx.Request("POST", url))
        resp.raise_for_status()

    monkeypatch.setattr(n._client, "post", fake_post)
    with caplog.at_level("ERROR"):
        assert n.send_text("hi") is False
    n.close()
    assert token not in caplog.text
    assert "can't parse entities" in caplog.text


def test_send_returns_false_on_malformed_success_body(notifier, monkeypatch):
    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"ok": True}  # no "result"

    monkeypatch.setattr(notifier._client, "post", lambda url, json: _Resp())
    assert notifier.send_text("hi") is False


def test_long_messages_are_split_on_line_boundaries(notifier, monkeypatch):
    sent = []

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"result": {"message_id": len(sent)}}

    def fake_post(url, json):
        sent.append(json["text"])
        return _Resp()

    monkeypatch.setattr(notifier._client, "post", fake_post)
    lines = [f"<b>line {i}</b> " + "x" * 80 for i in range(120)]
    assert notifier.send_text("\n".join(lines)) is True
    assert len(sent) > 1
    assert all(len(chunk) <= 4096 for chunk in sent)
    assert "\n".join(sent).split("\n") == lines  # nothing lost, no line cut in half


def test_send_job_omits_link_when_url_is_not_http(notifier, monkeypatch):
    captured = capture(notifier, monkeypatch)
    notifier.send_job(make_posting(url="javascript:alert(1)"))
    assert "<a href" not in captured["json"]["text"]
