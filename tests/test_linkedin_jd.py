"""Offline tests for the JD fetcher. httpx.get is stubbed; the success
path parses a real saved fixture (tests/fixtures/job_description.html --
an actual LinkedIn guest-endpoint response captured during development),
same pattern as test_linkedin_parser.py.
"""

from pathlib import Path

import httpx

from jobradar.sources.linkedin_jd import MAX_DESCRIPTION_CHARS, JobDescriptionFetcher

FIXTURE = (Path(__file__).parent / "fixtures" / "job_description.html").read_text()


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=httpx.Request("GET", "https://x"), response=self)


def test_fetch_extracts_real_description_from_fixture(monkeypatch):
    fetcher = JobDescriptionFetcher()
    monkeypatch.setattr(fetcher._client, "get", lambda *a, **kw: _FakeResponse(FIXTURE))

    text = fetcher.fetch("4455750767")

    assert text is not None
    assert len(text) > 500
    # Known content from the real fixture -- confirms the right container was selected.
    assert "Agentic AI Engineer" in text or "LLM" in text
    fetcher.close()


def test_fetch_returns_none_on_http_error(monkeypatch):
    fetcher = JobDescriptionFetcher()

    def boom(*a, **kw):
        raise httpx.ConnectError("no network", request=httpx.Request("GET", "https://x"))

    monkeypatch.setattr(fetcher._client, "get", boom)

    assert fetcher.fetch("123") is None
    fetcher.close()


def test_fetch_returns_none_on_4xx(monkeypatch):
    fetcher = JobDescriptionFetcher()
    monkeypatch.setattr(fetcher._client, "get", lambda *a, **kw: _FakeResponse("not found", status_code=404))

    assert fetcher.fetch("123") is None
    fetcher.close()


def test_fetch_returns_none_when_description_container_missing(monkeypatch):
    fetcher = JobDescriptionFetcher()
    monkeypatch.setattr(fetcher._client, "get", lambda *a, **kw: _FakeResponse("<html><body>no jd here</body></html>"))

    assert fetcher.fetch("123") is None
    fetcher.close()


def test_fetch_truncates_unusually_long_descriptions(monkeypatch):
    fetcher = JobDescriptionFetcher()
    long_html = f'<div class="description__text">{"word " * 5000}</div>'
    monkeypatch.setattr(fetcher._client, "get", lambda *a, **kw: _FakeResponse(long_html))

    text = fetcher.fetch("123")

    assert text is not None
    assert len(text) <= MAX_DESCRIPTION_CHARS
    fetcher.close()
