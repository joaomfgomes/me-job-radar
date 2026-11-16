"""Offline tests for the deterministic (non-LLM) language check that runs
before notify. No network calls -- langdetect is a local model.
"""

from jobradar.lang_filter import allowed_language_codes, detect_language_mismatch

DUTCH_JD = (
    "Wij zoeken een gedreven Software Engineer om ons team te versterken. "
    "In deze functie werk je aan de ontwikkeling van innovatieve softwareoplossingen "
    "binnen een agile team. Je hebt ervaring met Python en bent bekend met moderne "
    "cloudtechnologieën. Je werkt nauw samen met collega's uit verschillende "
    "disciplines om onze producten naar een hoger niveau te tillen. Wij bieden een "
    "prettige werkomgeving, goede arbeidsvoorwaarden en volop ruimte voor groei."
)

ENGLISH_JD = (
    "We are looking for a driven Software Engineer to join our team. In this role "
    "you will work on the development of innovative software solutions within an "
    "agile team. You have experience with Python and are familiar with modern "
    "cloud technologies. You work closely with colleagues from different "
    "disciplines to take our products to the next level. We offer a pleasant "
    "working environment, good employment conditions, and plenty of room to grow."
)


def test_allowed_language_codes_maps_known_names():
    assert allowed_language_codes(["English", "Spanish"]) == {"en", "es"}


def test_allowed_language_codes_ignores_unrecognized_names():
    assert allowed_language_codes(["Klingon"]) == set()


def test_dutch_jd_is_flagged_as_mismatch():
    reason = detect_language_mismatch(DUTCH_JD, ["English", "Spanish"])
    assert reason is not None
    assert "nl" in reason


def test_english_jd_passes():
    assert detect_language_mismatch(ENGLISH_JD, ["English", "Spanish"]) is None


def test_short_text_is_never_flagged():
    # Below MIN_CHARS_FOR_DETECTION -- too unreliable to act on.
    assert detect_language_mismatch("Korte Nederlandse tekst.", ["English"]) is None


def test_no_recognized_languages_configured_disables_enforcement():
    assert detect_language_mismatch(DUTCH_JD, ["Klingon"]) is None
