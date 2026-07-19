"""Language code normalization and subtitle filename parsing."""

from __future__ import annotations

from pathlib import Path

# ISO 639-1 <-> 639-2/B for the languages commonly seen in subtitle land.
_ISO_639: list[tuple[str, str, str]] = [
    # (639-1, 639-2, display name)
    ("en", "eng", "English"),
    ("de", "ger", "German"),
    ("fr", "fre", "French"),
    ("es", "spa", "Spanish"),
    ("it", "ita", "Italian"),
    ("pt", "por", "Portuguese"),
    ("nl", "dut", "Dutch"),
    ("sv", "swe", "Swedish"),
    ("no", "nor", "Norwegian"),
    ("da", "dan", "Danish"),
    ("fi", "fin", "Finnish"),
    ("pl", "pol", "Polish"),
    ("cs", "cze", "Czech"),
    ("sk", "slo", "Slovak"),
    ("hu", "hun", "Hungarian"),
    ("ro", "rum", "Romanian"),
    ("bg", "bul", "Bulgarian"),
    ("el", "gre", "Greek"),
    ("tr", "tur", "Turkish"),
    ("ru", "rus", "Russian"),
    ("uk", "ukr", "Ukrainian"),
    ("ar", "ara", "Arabic"),
    ("he", "heb", "Hebrew"),
    ("hi", "hin", "Hindi"),
    ("zh", "chi", "Chinese"),
    ("ja", "jpn", "Japanese"),
    ("ko", "kor", "Korean"),
    ("th", "tha", "Thai"),
    ("vi", "vie", "Vietnamese"),
    ("id", "ind", "Indonesian"),
    ("ms", "may", "Malay"),
    ("fa", "per", "Persian"),
    ("hr", "hrv", "Croatian"),
    ("sr", "srp", "Serbian"),
    ("sl", "slv", "Slovenian"),
    ("et", "est", "Estonian"),
    ("lv", "lav", "Latvian"),
    ("lt", "lit", "Lithuanian"),
    ("is", "ice", "Icelandic"),
    ("ca", "cat", "Catalan"),
]

# extra aliases -> 639-1
_ALIASES = {
    "deu": "de", "fra": "fr", "nld": "nl", "ces": "cs", "slk": "sk",
    "ron": "ro", "ell": "el", "zho": "zh", "msa": "ms", "fas": "fa",
    "isl": "is", "pob": "pt", "pt-br": "pt", "pt-pt": "pt", "zh-cn": "zh",
    "zh-tw": "zh", "en-us": "en", "en-gb": "en", "es-la": "es", "es-es": "es",
}

_BY_ANY: dict[str, str] = {}
_TO_3: dict[str, str] = {}
_NAMES: dict[str, str] = {}
for _two, _three, _name in _ISO_639:
    _BY_ANY[_two] = _two
    _BY_ANY[_three] = _two
    _BY_ANY[_name.lower()] = _two
    _TO_3[_two] = _three
    _NAMES[_two] = _name
_BY_ANY.update(_ALIASES)


def normalize_language(code: str | None) -> str | None:
    """Normalize any common language spelling to ISO 639-1, or None if unknown."""
    if not code:
        return None
    return _BY_ANY.get(code.strip().lower())


def to_iso639_2(code: str) -> str:
    two = normalize_language(code) or code
    return _TO_3.get(two, two)


def language_name(code: str) -> str:
    two = normalize_language(code)
    return _NAMES.get(two or "", code)


UNKNOWN_LANGUAGE = "und"


def parse_subtitle_filename(
    sub_path: str | Path, media_stem: str | None = None
) -> tuple[str | None, bool, bool]:
    """Parse ``movie.en.forced.srt``-style names.

    Returns (language | None, forced, hearing_impaired). When *media_stem* is
    given, only the tokens AFTER the media stem are inspected, so titles that
    contain language words ("The.Italian.Job.srt") are not misread.
    """
    stem = Path(sub_path).stem  # strips .srt
    if media_stem is not None:
        if stem == media_stem:
            return None, False, False
        if stem.startswith(media_stem + "."):
            stem = stem[len(media_stem) + 1:]
    tokens = [t.lower() for t in stem.split(".")]
    language: str | None = None
    forced = False
    hi = False
    if media_stem is not None:
        candidates = tokens  # only suffix tokens remain - all are fair game
    else:
        # Look at trailing tokens only - the title itself may contain dots.
        candidates = tokens[-3:] if len(tokens) > 1 else []
    for token in reversed(candidates):
        if token == "forced":
            forced = True
        elif token in ("sdh", "hi", "cc"):
            hi = True
        elif language is None:
            lang = normalize_language(token)
            if lang:
                language = lang
    return language, forced, hi
