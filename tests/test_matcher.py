from jsm.providers.base import SubtitleCandidate
from jsm.subtitles.matcher import guess_media, rank_candidates, score_candidate


def cand(release, **kwargs):
    return SubtitleCandidate(
        provider="fake", file_id="1", language="en", release_name=release, **kwargs
    )


def test_guess_media_parses_scene_names():
    guess = guess_media("Inception.2010.1080p.BluRay.x264-YIFY.mkv")
    assert guess.title == "Inception"
    assert guess.year == 2010
    assert guess.release_group == "YIFY"
    assert guess.screen_size == "1080p"


def test_hash_match_beats_everything():
    guess = guess_media("Inception.2010.1080p.BluRay.x264-YIFY.mkv")
    score, reason = score_candidate(guess, cand("Totally Different Name", moviehash_match=True))
    assert score == 0.99
    assert reason == "hash match"


def test_filename_scoring_orders_sensibly():
    media = "Inception.2010.1080p.BluRay.x264-YIFY.mkv"
    exact = cand("Inception.2010.1080p.BluRay.x264-YIFY")
    same_movie = cand("Inception.2010.720p.WEB-DL")
    wrong_year = cand("Inception.2008.1080p")
    unrelated = cand("Winnie.The.Pooh.2011.720p")
    ranked = rank_candidates(media, [unrelated, wrong_year, same_movie, exact])
    assert ranked[0] is exact
    assert ranked[1] is same_movie
    assert ranked[0].confidence > 0.7
    assert ranked[-1] is unrelated
    assert unrelated.confidence < 0.5


def test_rank_filters_language():
    media = "Inception.2010.mkv"
    en = cand("Inception 2010")
    de = SubtitleCandidate(provider="fake", file_id="2", language="de",
                           release_name="Inception 2010")
    ranked = rank_candidates(media, [en, de], language="en")
    assert ranked == [en]


def test_downloads_break_confidence_ties():
    media = "Inception.2010.mkv"
    a = cand("Inception 2010", downloads=5)
    b = cand("Inception 2010", downloads=500)
    ranked = rank_candidates(media, [a, b])
    assert ranked[0] is b
