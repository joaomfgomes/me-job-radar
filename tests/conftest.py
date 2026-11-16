"""Shared fakes for the offline test suite. Nothing here touches the network:
FakeSource/FakeNotifier/FakeScorer/FakeJDFetcher stand in for LinkedIn,
Telegram, OpenAI and the JD fetcher respectively.
"""

import pytest

from jobradar.config import Settings
from jobradar.models import JobPosting, SearchConfig
from jobradar.profile import Dealbreakers, Profile
from jobradar.scoring import DeepScoreResult, Requirement, ScoreResult


class FakeSource:
    def __init__(self, postings=None):
        self._postings = postings or []
        self.calls: list[str] = []

    def fetch(self, search: SearchConfig):
        self.calls.append(search.name)
        return list(self._postings)


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[JobPosting, int | None, DeepScoreResult | None, bool]] = []
        self.texts: list[str] = []
        self.digests: list[tuple[list[dict], int]] = []
        self._next_message_id = 1

    def send_job(self, posting, score=None, deep=None, language_unverified=False):
        self.sent.append((posting, score, deep, language_unverified))
        message_id = self._next_message_id
        self._next_message_id += 1
        return message_id

    def send_text(self, text):
        self.texts.append(text)
        return True

    def send_digest(self, rows, threshold):
        self.digests.append((rows, threshold))
        return True

    @property
    def sent_postings(self):
        return [p for p, _, _, _ in self.sent]


class FakeScorer:
    def __init__(self, title_result=None, deep_result=None):
        self._title_result = title_result or ScoreResult(score=9, reason="Strong title.")
        self._deep_result = deep_result
        self.score_calls: list[JobPosting] = []
        self.deep_calls: list[tuple[JobPosting, str]] = []

    def score(self, posting, profile):
        self.score_calls.append(posting)
        return self._title_result

    def score_deep(self, posting, profile, jd_text, title_score=None, title_score_reason=None, cv_context=None):
        self.deep_calls.append((posting, jd_text))
        return self._deep_result


class FakeJDFetcher:
    def __init__(self, jd_text=None):
        self._jd_text = jd_text
        self.calls: list[str] = []

    def fetch(self, job_id):
        self.calls.append(job_id)
        return self._jd_text


def make_requirements(met: int, unmet: int):
    return tuple(
        [Requirement(requirement=f"req-met-{i}", met=True, evidence="CV shows it") for i in range(met)]
        + [Requirement(requirement=f"req-missing-{i}", met=False, evidence="not in CV") for i in range(unmet)]
    )


def make_deep(met=8, unmet=2, junior=True, years_required=None, score=9, reason="Good fit.", unscored=False):
    return DeepScoreResult(
        score=score, reason=reason,
        requirements=make_requirements(met, unmet),
        years_required=years_required,
        is_junior_appropriate=junior, unscored=unscored,
    )


@pytest.fixture
def profile():
    return Profile(
        target_roles=["AI Engineer"], tech_stack=["Python"], seniority="entry-to-mid",
        max_years_experience=3, languages=["English"], adjacent_roles_require_llm_tie=[],
        dealbreakers=Dealbreakers(), notes="",
    )


@pytest.fixture
def settings():
    return Settings(
        searches=[SearchConfig(name="Search A", keywords="AI Engineer", location="Remote",
                               source="linkedin", location_name="Remote")],
        telegram_bot_token="x", telegram_chat_id="y",
        deep_score_threshold=4, min_coverage_pct=80, digest_min_coverage_pct=60,
    )


@pytest.fixture
def posting():
    return JobPosting(
        job_id="1", title="AI Engineer", company="Acme", location="Remote",
        url="https://example.com/1", posted_label="1h ago", search_name="Search A",
    )
