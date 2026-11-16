"""CV ingestion: cv.pdf -> profile.generated.yaml.

One-shot, manual tool (not part of the unattended pipeline) -- run it
yourself when your CV changes, review the output, then fold what you want
into profile.yaml by hand. profile.yaml stays hand-owned: target_roles,
dealbreakers, and notes express intent, which a CV can't tell us. This
module only extracts *facts* (skills, roles held, education, languages).

Anti-fabrication is two-layered, deliberately not just a prompt
instruction:
  1. scrub_placeholders() deterministically replaces any "[PREENCHER: ...]"
     (or "[TODO: ...]" / "[FILL: ...]") span with the literal marker
     "[UNSPECIFIED]" *before* the text ever reaches the model -- so the
     model never even sees the placeholder's instructional text as
     if it were real content to paraphrase.
  2. The system prompt tells the model to carry "[UNSPECIFIED]" straight
     through as the literal string "unspecified", never to infer a value.

Run directly:  uv run python -m jobradar.cv_ingest [cv.pdf] [profile.generated.yaml]
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel
from pypdf import PdfReader

from jobradar.llm import DEFAULT_MODEL, ClaudeClient

log = logging.getLogger(__name__)

# Entries the CV left blank. They are never shown to a model as if they
# were real content (see format_cv_context).
_UNSPECIFIED = ("unspecified", "[UNSPECIFIED]")

# Matches "[PREENCHER: ...]" (Portuguese "fill in"), and the common
# English equivalents, case-insensitively. Extend this if your CV uses a
# different placeholder convention.
_PLACEHOLDER_RE = re.compile(r"\[(?:PREENCHER|TODO|FILL)\s*:[^\]]*\]", re.IGNORECASE)

SYSTEM_PROMPT = (
    "You extract structured facts from a candidate's CV/resume text. Rules:\n"
    "1. Only include information explicitly present in the text.\n"
    "2. Any field whose source content is the literal marker [UNSPECIFIED] -- a "
    "placeholder the candidate has not filled in yet -- MUST be output as the "
    "literal string 'unspecified'. Do not infer, estimate, guess, or paraphrase a "
    "plausible value for it, even if surrounding context makes a guess tempting.\n"
    "3. Never invent skills, dates, employers, or achievements that are not present "
    "in the text.\n"
    "4. tech_stack should list concrete named tools/technologies/frameworks "
    "mentioned anywhere in the text (skills section, experience bullets, or "
    "project descriptions), deduplicated."
)


class LanguageProficiency(BaseModel):
    language: str
    proficiency: str  # e.g. "Native", "Professional working proficiency", or "unspecified"


class ExperienceEntry(BaseModel):
    role: str
    company: str
    period: str
    details: str  # bullet summary, or literally "unspecified" if the source was a placeholder


class ExtractedProfile(BaseModel):
    tech_stack: list[str]
    certifications: list[str]
    experience: list[ExperienceEntry]
    education: list[str]
    languages: list[LanguageProficiency]
    notable_projects: list[str]
    seniority_signal: str  # descriptive (e.g. "~2 years combined research + industry"), not a category


def extract_pdf_text(pdf_path: str | Path) -> str:
    reader = PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def scrub_placeholders(text: str) -> str:
    """Neutralize unfilled CV placeholders before the model ever sees them."""
    return _PLACEHOLDER_RE.sub("[UNSPECIFIED]", text)


class CVIngestor:
    """Turns scrubbed CV text into an ExtractedProfile, via Claude."""

    def __init__(self, client: ClaudeClient, model: str = DEFAULT_MODEL) -> None:
        self._client = client
        self._model = model

    @classmethod
    def from_env(cls, model: str = DEFAULT_MODEL) -> CVIngestor:
        load_dotenv()  # standalone CLI entry point -- main.py's settings loader isn't in play here
        return cls(ClaudeClient.from_env(), model=model)

    def close(self) -> None:
        self._client.close()

    def extract(self, scrubbed_cv_text: str) -> ExtractedProfile:
        """Raises on any failure: this is a manual tool, and a half-read CV
        is worse than an error message."""
        return self._client.parse(
            model=self._model,
            system=SYSTEM_PROMPT,
            user=scrubbed_cv_text,
            schema=ExtractedProfile,
            effort="medium",
        )


def write_generated_profile(profile: ExtractedProfile, output_path: str | Path) -> None:
    data = {
        "tech_stack": profile.tech_stack,
        "certifications": profile.certifications,
        "experience": [e.model_dump() for e in profile.experience],
        "education": profile.education,
        "languages": {lang.language: lang.proficiency for lang in profile.languages},
        "notable_projects": profile.notable_projects,
        "seniority_signal": profile.seniority_signal,
    }
    header = (
        "# Generated from cv.pdf by `python -m jobradar.cv_ingest` -- DO NOT hand-edit.\n"
        "# Re-run the ingestor to regenerate; fold what you want into profile.yaml\n"
        "# yourself. profile.yaml stays hand-owned -- target_roles, dealbreakers, and\n"
        "# notes express intent, which a CV can't tell us.\n"
        "#\n"
        "# Fields with value 'unspecified' were blank/placeholder in the source CV\n"
        "# and were deliberately NOT guessed. See cv_ingest.py's module docstring.\n\n"
    )
    with open(output_path, "w") as f:
        f.write(header)
        yaml.dump(data, f, sort_keys=False, allow_unicode=True)


def _is_real(value: object) -> bool:
    return bool(value) and str(value).strip() not in _UNSPECIFIED


def format_cv_context(path: str | Path = "profile.generated.yaml") -> str | None:
    """Render profile.generated.yaml as text for the deep-scoring prompt:
    experience, projects, skills, certifications, education and languages.

    Returns None if the file doesn't exist, doesn't parse, or has nothing
    usable -- deep scoring still works without it, with less to cite.
    'unspecified' entries are skipped: a blank the candidate never filled
    in must not reach the model as if it were a fact.

    The output must be deterministic for a given file, because it is part
    of the cached prompt prefix (see scoring.candidate_context).
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError:
        log.warning("Could not parse %s for CV context; continuing without it", path)
        return None

    lines: list[str] = []
    for entry in data.get("experience") or []:
        if _is_real(entry.get("details")):
            period = f" ({entry['period']})" if _is_real(entry.get("period")) else ""
            lines.append(f"- {entry.get('role', '')} at {entry.get('company', '')}{period}: {entry['details'].strip()}")

    projects = [p for p in data.get("notable_projects") or [] if _is_real(p)]
    if projects:
        lines.append(f"- Notable projects: {', '.join(projects)}")

    for label, key in (("Skills named in the CV", "tech_stack"),
                       ("Certifications", "certifications"),
                       ("Education", "education")):
        values = [str(v) for v in data.get(key) or [] if _is_real(v)]
        if values:
            lines.append(f"- {label}: {', '.join(values)}")

    languages = data.get("languages") or {}
    if isinstance(languages, dict):
        spoken = [f"{name} ({level})" if _is_real(level) else str(name) for name, level in languages.items()]
        if spoken:
            lines.append(f"- Spoken languages: {', '.join(spoken)}")

    if _is_real(data.get("seniority_signal")):
        lines.append(f"- Experience level: {data['seniority_signal']}")

    return "\n".join(lines) if lines else None


def ingest_cv(
    pdf_path: str | Path,
    output_path: str | Path = "profile.generated.yaml",
    model: str = DEFAULT_MODEL,
) -> ExtractedProfile:
    raw_text = extract_pdf_text(pdf_path)
    scrubbed = scrub_placeholders(raw_text)

    ingestor = CVIngestor.from_env(model=model)
    try:
        profile = ingestor.extract(scrubbed)
    finally:
        ingestor.close()

    write_generated_profile(profile, output_path)
    log.info("Wrote %s from %s", output_path, pdf_path)
    return profile


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    pdf_arg = sys.argv[1] if len(sys.argv) > 1 else "cv.pdf"
    out_arg = sys.argv[2] if len(sys.argv) > 2 else "profile.generated.yaml"
    ingest_cv(pdf_arg, out_arg)
    print(f"Wrote {out_arg} -- review it, then fold what you want into profile.yaml yourself.")
