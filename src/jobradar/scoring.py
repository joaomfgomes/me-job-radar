"""LLM relevance scoring via OpenAI's gpt-4o-mini.

Uses the official `openai` SDK's Structured Outputs (`beta.chat.completions.parse`
with a Pydantic response schema) rather than hand-rolled JSON parsing --
the API is constrained to emit exactly {score, reason}, so there is no
free-text output to fail on.

Fail-open: any scoring failure (network error, timeout, rate limit,
refusal) returns an "unscored" ScoreResult rather than raising. Missing a
real job because the scorer had a bad moment is worse than one unfiltered
alert reaching Telegram -- the same failure-mode philosophy as the
LinkedIn source's RateLimitError handling and the pipeline's
notify-before-mark-seen ordering.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from openai import OpenAI
from pydantic import BaseModel

from jobradar.models import JobPosting
from jobradar.profile import Profile

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o-mini"

SYSTEM_PROMPT = (
    "You screen a single LinkedIn job posting for relevance against a candidate's "
    "profile. Score from 1 (irrelevant) to 10 (excellent match) and give a one-sentence "
    "reason. Respond only via the structured schema provided."
)

# Deep-scoring stage (step 4): runs only on jobs that clear a low bar on the
# cheap title-only pass above, now with the full job description and (when
# available) the candidate's actual project experience -- both far more
# informative than a title, so the prompt says so explicitly rather than
# letting the model anchor on the preliminary score.
SYSTEM_PROMPT_DEEP = (
    "You assess how well a candidate matches a job posting, using the job's FULL "
    "description. Do all of the following, and respond only via the structured "
    "schema provided.\n\n"
    "1. REQUIREMENTS: Extract the posting's must-have requirements -- the things a "
    "candidate genuinely needs to be considered. Include hard requirements of every "
    "kind: specific technologies, years of experience, degrees/certifications, and "
    "language requirements. EXCLUDE nice-to-haves, 'bonus'/'plus' items, and generic "
    "soft skills (teamwork, communication). Aim for the 4-10 requirements that "
    "actually gate the application. For each one set met=true only if the candidate's "
    "CV, project experience, or tech stack below clearly demonstrates it -- do NOT "
    "give credit for adjacent or assumed knowledge. In `evidence`, cite the specific "
    "thing that demonstrates it, or state plainly what is missing.\n\n"
    "2. YEARS_REQUIRED: The MINIMUM years of professional experience the posting states "
    "or clearly implies as a requirement (e.g. '4+ years', '3-5 years', 'at least 5 "
    "years'). Extract the number exactly as stated -- do not round down or interpret "
    "generously. null if the posting states no number at all.\n\n"
    "3. IS_JUNIOR_APPROPRIATE: This candidate is entry-to-mid level. Set false if EITHER "
    "of these is true, with zero benefit of the doubt -- when genuinely ambiguous, "
    "treat it as false, not true:\n"
    "   - the posting requires more experience than the candidate's ceiling (see "
    "YEARS_REQUIRED above and the numeric ceiling given below), even if the title "
    "doesn't say 'Senior'\n"
    "   - the role centres on leading/mentoring a team, owning architecture decisions, "
    "or being the most senior/sole expert on the team, regardless of years stated\n\n"
    "4. SCORE: 1 (irrelevant) to 10 (excellent match) for how well the ROLE ITSELF "
    "fits the candidate's target roles -- this is about relevance/desirability, "
    "separate from whether they meet the requirements. Give a one-sentence reason."
)


class _ScoreSchema(BaseModel):
    """The exact shape the title-pass model is constrained to return."""

    score: int
    reason: str


class _RequirementSchema(BaseModel):
    requirement: str
    met: bool
    evidence: str  # what in the CV/profile supports `met` (or what's missing)


class _DeepScoreSchema(BaseModel):
    """Deep pass. Note it does NOT return a coverage percentage: the model
    enumerates the posting's must-have requirements and judges each one,
    and coverage is computed from that list in Python (see
    DeepScoreResult.coverage_pct). Asking a model for a percentage directly
    yields poorly-calibrated round numbers that can't be audited; a list of
    met/unmet requirements can be shown to the user and checked."""

    requirements: list[_RequirementSchema]
    years_required: int | None
    is_junior_appropriate: bool
    score: int
    reason: str


@dataclass(frozen=True)
class ScoreResult:
    score: int | None
    reason: str
    unscored: bool = False  # True if scoring failed -- caller should fail open (notify anyway)


@dataclass(frozen=True)
class Requirement:
    requirement: str
    met: bool
    evidence: str


@dataclass(frozen=True)
class DeepScoreResult:
    score: int | None
    reason: str
    requirements: tuple[Requirement, ...] = ()
    years_required: int | None = None  # JD-stated minimum years, extracted verbatim -- see SYSTEM_PROMPT_DEEP
    is_junior_appropriate: bool = True  # fail-open default: don't reject on a missing judgment
    unscored: bool = False

    @property
    def coverage_pct(self) -> int | None:
        """Share of the posting's must-have requirements the candidate
        meets. None when the model returned no requirements at all -- the
        caller must fail open on that rather than treat it as 0%."""
        if not self.requirements:
            return None
        met = sum(1 for r in self.requirements if r.met)
        return round(100 * met / len(self.requirements))

    @property
    def matched(self) -> tuple[Requirement, ...]:
        return tuple(r for r in self.requirements if r.met)

    @property
    def missing(self) -> tuple[Requirement, ...]:
        return tuple(r for r in self.requirements if not r.met)


def _profile_context_lines(profile: Profile, include_language_note: bool = True) -> list[str]:
    lines = [
        f"Target roles: {', '.join(profile.target_roles)}",
        f"Tech stack: {', '.join(profile.tech_stack)}",
        f"Seniority ceiling: {profile.seniority}",
    ]
    if profile.max_years_experience is not None:
        lines.append(
            f"Maximum years of experience the candidate has: {profile.max_years_experience}. "
            "A posting requiring more than this is above the ceiling -- not a judgment call."
        )
    if profile.languages and include_language_note:
        lines.append(
            f"Candidate only reads/speaks: {', '.join(profile.languages)} -- if the job "
            "posting's description is not written in one of these languages, "
            "score it very low (1-2)."
        )
    if profile.adjacent_roles_require_llm_tie:
        lines.append(
            "These role titles are only relevant if the posting is clearly tied to LLM/GenAI "
            f"application work -- otherwise score them low (2-4): "
            f"{', '.join(profile.adjacent_roles_require_llm_tie)}"
        )
    if profile.notes:
        lines.append(f"Additional context: {profile.notes.strip()}")
    return lines


def build_prompt(posting: JobPosting, profile: Profile) -> str:
    # No description at this stage -- see JobPosting/module docstrings on why
    # a title rarely reveals the posting's actual language. Language is
    # already enforced deterministically (lang_filter.py, on the real JD
    # body) *before* this title pass ever runs -- see pipeline.py's cascade
    # order -- so repeating the instruction here only invited the model to
    # guess at language from a title it can't actually judge that from,
    # sometimes fabricating a language complaint as part of its reasoning
    # and dragging down an otherwise-fair relevance score. Observed directly
    # in production: "AI Compute Engineer" at Mistral scored 2/10 partly on
    # a fabricated "not in English or Spanish" claim.
    lines = _profile_context_lines(profile, include_language_note=False)
    lines += [
        "",
        "Job posting:",
        f"Title: {posting.title}",
        f"Company: {posting.company}",
        f"Location: {posting.location}",
    ]
    return "\n".join(lines)


def build_deep_prompt(
    posting: JobPosting,
    profile: Profile,
    jd_text: str,
    title_score: int | None = None,
    title_score_reason: str | None = None,
    cv_context: str | None = None,
) -> str:
    lines = _profile_context_lines(profile)
    if cv_context:
        lines.append(f"Candidate's relevant project/experience details:\n{cv_context}")
    if title_score is not None:
        lines.append(
            f'Preliminary title-only score: {title_score}/10 ("{title_score_reason}") -- '
            "the full description below is far more informative than the title alone."
        )
    lines += [
        "",
        "Job posting:",
        f"Title: {posting.title}",
        f"Company: {posting.company}",
        f"Location: {posting.location}",
        "",
        "Full job description:",
        jd_text,
    ]
    return "\n".join(lines)


class OpenAIScorer:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        deep_model: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._model = model
        # The deep pass enumerates and judges every requirement -- a much
        # harder task than the title pass's single score -- and only runs on
        # the ~13% of jobs that clear deep_score_threshold, so it's worth a
        # stronger model. Defaults to the same model when not set.
        self._deep_model = deep_model or model
        self._client = OpenAI(api_key=api_key, timeout=timeout)

    @classmethod
    def from_env(cls, model: str = DEFAULT_MODEL, deep_model: str | None = None) -> OpenAIScorer:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY must be set in .env (see .env.example).")
        return cls(api_key=api_key, model=model, deep_model=deep_model)

    @property
    def model(self) -> str:
        return self._model

    @property
    def deep_model(self) -> str:
        return self._deep_model

    def close(self) -> None:
        self._client.close()

    def score(self, posting: JobPosting, profile: Profile) -> ScoreResult:
        prompt = build_prompt(posting, profile)
        try:
            parsed = self._parse(self._model, SYSTEM_PROMPT, prompt, _ScoreSchema)
        except Exception as exc:
            log.error("OpenAI scoring failed for job %s: %s", posting.job_id, exc)
            return ScoreResult(score=None, reason=f"scoring unavailable: {exc}", unscored=True)
        return ScoreResult(score=parsed.score, reason=parsed.reason)

    def score_deep(
        self,
        posting: JobPosting,
        profile: Profile,
        jd_text: str,
        title_score: int | None = None,
        title_score_reason: str | None = None,
        cv_context: str | None = None,
    ) -> DeepScoreResult:
        prompt = build_deep_prompt(posting, profile, jd_text, title_score, title_score_reason, cv_context)
        try:
            parsed = self._parse(self._deep_model, SYSTEM_PROMPT_DEEP, prompt, _DeepScoreSchema)
        except Exception as exc:
            log.error("OpenAI deep scoring failed for job %s: %s", posting.job_id, exc)
            return DeepScoreResult(score=None, reason=f"deep scoring unavailable: {exc}", unscored=True)
        return DeepScoreResult(
            score=parsed.score,
            reason=parsed.reason,
            requirements=tuple(
                Requirement(requirement=r.requirement, met=r.met, evidence=r.evidence)
                for r in parsed.requirements
            ),
            years_required=parsed.years_required,
            is_junior_appropriate=parsed.is_junior_appropriate,
        )

    def _parse(self, model: str, system_prompt: str, user_prompt: str, schema: type[BaseModel]) -> BaseModel:
        """Raises on any failure -- callers convert that into their own
        fail-open result type."""
        completion = self._client.beta.chat.completions.parse(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format=schema,
            temperature=0,
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            # Refusal or a finish_reason other than "stop" -- treat like any other failure.
            raise ValueError(f"no parsed output (finish_reason={completion.choices[0].finish_reason})")
        return parsed
