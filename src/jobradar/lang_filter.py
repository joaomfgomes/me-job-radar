"""Deterministic (non-LLM) language check on a job description's full text.

Runs before notify, on the JD text the pipeline already fetches -- so a
posting written in a language the candidate doesn't speak is suppressed
like any other prefilter dealbreaker: never reaches Telegram, never costs
an LLM call. Title alone can't do this job -- LinkedIn job titles for tech
roles are almost always kept in English regardless of the posting's actual
language, so the signal only exists in the description body.

Same fail-open posture as the rest of the pipeline: a failed or ambiguous
detection just lets the job through rather than risk a false suppression.
"""

from __future__ import annotations

import logging

from langdetect import DetectorFactory, LangDetectException, detect

log = logging.getLogger(__name__)

# langdetect's detector is otherwise randomized per call -- pin it so a
# given JD text always classifies the same way (matters for tests, but also
# for not flip-flopping on the same job across cycles).
DetectorFactory.seed = 0

# profile.yaml uses human-readable language names; langdetect returns ISO
# 639-1 codes. Only languages we can map get enforced -- an unrecognized
# name in profile.yaml just means that language isn't enforced, not a crash.
_LANGUAGE_CODES = {
    "english": "en",
    "spanish": "es",
    "portuguese": "pt",
    "dutch": "nl",
    "german": "de",
    "french": "fr",
    "italian": "it",
}

# Detection on short strings is unreliable (a few words can look like
# almost any language). Real job descriptions are always far longer than
# this once truncation kicks in (see linkedin_jd.MAX_DESCRIPTION_CHARS) --
# this guard just avoids acting on noise for edge-case short postings.
MIN_CHARS_FOR_DETECTION = 200


def allowed_language_codes(languages: list[str]) -> set[str]:
    codes = set()
    for lang in languages:
        code = _LANGUAGE_CODES.get(lang.strip().lower())
        if code:
            codes.add(code)
        else:
            log.warning("Unrecognized language '%s' in profile.yaml -- not enforced.", lang)
    return codes


def detect_language_mismatch(jd_text: str, allowed_languages: list[str]) -> str | None:
    """Returns a suppression reason if jd_text is confidently NOT written in
    one of allowed_languages, else None (pass -- includes detection
    failure/uncertainty, per the fail-open policy above)."""
    allowed = allowed_language_codes(allowed_languages)
    if not allowed or len(jd_text) < MIN_CHARS_FOR_DETECTION:
        return None

    try:
        detected = detect(jd_text)
    except LangDetectException as exc:
        log.warning("Language detection failed: %s -- letting the job through", exc)
        return None

    if detected in allowed:
        return None
    return f"job description detected as '{detected}', not in candidate's languages ({', '.join(sorted(allowed))})"
