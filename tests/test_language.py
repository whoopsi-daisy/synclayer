from jsm.subtitles.language import (
    language_name,
    normalize_language,
    parse_subtitle_filename,
    to_iso639_2,
)


def test_normalize():
    assert normalize_language("en") == "en"
    assert normalize_language("eng") == "en"
    assert normalize_language("English") == "en"
    assert normalize_language("pt-BR") == "pt"
    assert normalize_language("deu") == "de"
    assert normalize_language("klingon") is None
    assert normalize_language(None) is None


def test_round_trip():
    assert to_iso639_2("en") == "eng"
    assert language_name("de") == "German"


def test_parse_subtitle_filename():
    assert parse_subtitle_filename("Movie (2010).en.srt") == ("en", False, False)
    assert parse_subtitle_filename("Movie (2010).eng.forced.srt") == ("en", True, False)
    assert parse_subtitle_filename("Movie.de.sdh.srt") == ("de", False, True)
    assert parse_subtitle_filename("Movie (2010).srt") == (None, False, False)
    assert parse_subtitle_filename("Some.Movie.2010.srt") == (None, False, False)
