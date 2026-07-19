from jsm.database.models import MediaStatus, Subtitle, SyncStatus
from jsm.scanner.filesystem import Scanner, compute_status


def test_scan_finds_media_and_status(db, scanner, media_tree):
    stats = scanner.scan(media_tree)
    assert stats.scanned == 4
    assert stats.added == 4
    by_name = {m.filename: m for m in db.all_media()}
    assert by_name["Alien (1979).mkv"].status == MediaStatus.OK
    assert by_name["Winnie The Pooh (2011).mkv"].status == MediaStatus.MISSING
    assert by_name["DuckTales (1990).mkv"].status == MediaStatus.WRONG_LANG
    assert by_name["Show S01E01.mkv"].status == MediaStatus.MISSING


def test_incremental_scan_skips_unchanged(db, scanner, media_tree):
    scanner.scan(media_tree)
    stats = scanner.scan(media_tree)
    assert stats.added == 0
    assert stats.changed == 0
    assert stats.scanned == 4


def test_scan_detects_new_subtitle(db, scanner, media_tree):
    scanner.scan(media_tree)
    movies = media_tree / "new-movies"
    (movies / "Winnie The Pooh (2011).en.srt").write_text("1")
    scanner.scan(media_tree)
    media = db.get_media_by_path(str(movies / "Winnie The Pooh (2011).mkv"))
    assert media.status == MediaStatus.OK


def test_scan_removes_vanished_files(db, scanner, media_tree):
    scanner.scan(media_tree)
    (media_tree / "tv" / "Show S01E01.mkv").unlink()
    stats = scanner.scan(media_tree)
    assert stats.removed == 1
    assert db.get_media_by_path(str(media_tree / "tv" / "Show S01E01.mkv")) is None


def test_changed_file_invalidates_hash(db, scanner, media_tree):
    scanner.scan(media_tree)
    path = media_tree / "new-movies" / "Winnie The Pooh (2011).mkv"
    media = db.get_media_by_path(str(path))
    db.set_media_hash(media.id, "deadbeef00000000")
    path.write_bytes(b"different content" * 20000)
    scanner.scan(media_tree)
    assert db.get_media_by_path(str(path)).hash is None


def test_subtitle_language_variants_matched(db, scanner, media_tree):
    movies = media_tree / "new-movies"
    (movies / "Winnie The Pooh (2011).eng.forced.srt").write_text("1")
    scanner.scan(media_tree)
    media = db.get_media_by_path(str(movies / "Winnie The Pooh (2011).mkv"))
    subs = db.subtitles_for(media.id)
    assert len(subs) == 1
    assert subs[0].language == "en"
    assert subs[0].forced is True
    # a forced-only sub still counts as OK for now (it matches the language)
    assert media.status == MediaStatus.OK


def test_compute_status_unsynced():
    subs = [
        Subtitle(id=None, media_id=1, language="en", path="/x.srt",
                 source="external", sync_status=SyncStatus.UNSYNCED)
    ]
    assert compute_status(subs, ["en"]) == MediaStatus.UNSYNCED
    subs.append(
        Subtitle(id=None, media_id=1, language="en", path="/y.srt",
                 source="external", sync_status=SyncStatus.SYNCED)
    )
    assert compute_status(subs, ["en"]) == MediaStatus.OK


def test_unknown_language_sub_counts_as_ok(db, scanner, media_tree):
    movies = media_tree / "new-movies"
    (movies / "Winnie The Pooh (2011).srt").write_text("1")
    scanner.scan(media_tree)
    media = db.get_media_by_path(str(movies / "Winnie The Pooh (2011).mkv"))
    assert media.status == MediaStatus.OK
