"""Safety-contract tests: the library must be unable to damage media files."""

import pytest

from jsm.subtitles.fileops import (
    UnsafeWriteError,
    backup_path_for,
    next_free_path,
    safe_write_subtitle,
    subtitle_destination,
)


def test_refuses_media_extensions(tmp_path):
    for name in ["movie.mkv", "movie.mp4", "movie.avi", "movie.webm", "movie.exe", "movie"]:
        with pytest.raises(UnsafeWriteError):
            safe_write_subtitle(tmp_path / name, b"data")


def test_never_silently_overwrites(tmp_path):
    dest = tmp_path / "movie.en.srt"
    dest.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        safe_write_subtitle(dest, b"new")
    assert dest.read_bytes() == b"original"


def test_overwrite_creates_backup(tmp_path):
    dest = tmp_path / "movie.en.srt"
    dest.write_bytes(b"original")
    safe_write_subtitle(dest, b"new", overwrite=True)
    assert dest.read_bytes() == b"new"
    assert (tmp_path / "movie.en.srt.bak").read_bytes() == b"original"


def test_backup_never_clobbers_existing_backup(tmp_path):
    dest = tmp_path / "movie.en.srt"
    dest.write_bytes(b"v1")
    safe_write_subtitle(dest, b"v2", overwrite=True)
    safe_write_subtitle(dest, b"v3", overwrite=True)
    assert (tmp_path / "movie.en.srt.bak").read_bytes() == b"v1"
    assert (tmp_path / "movie.en.srt.bak2").read_bytes() == b"v2"


def test_atomic_write_and_no_leftover_temp(tmp_path):
    dest = tmp_path / "movie.en.srt"
    safe_write_subtitle(dest, b"content")
    assert dest.read_bytes() == b"content"
    assert [p.name for p in tmp_path.iterdir()] == ["movie.en.srt"]


def test_next_free_path(tmp_path):
    dest = tmp_path / "movie.en.srt"
    assert next_free_path(dest) == dest
    dest.write_bytes(b"x")
    assert next_free_path(dest).name == "movie.en.2.srt"
    (tmp_path / "movie.en.2.srt").write_bytes(b"x")
    assert next_free_path(dest).name == "movie.en.3.srt"


def test_subtitle_destination(tmp_path):
    media = tmp_path / "Movie (2010).mkv"
    assert subtitle_destination(media, "en").name == "Movie (2010).en.srt"
    assert subtitle_destination(media, "de", ".ass").name == "Movie (2010).de.ass"


def test_media_files_never_written(tmp_path, media_tree):
    """A full download cycle must leave every media file byte-identical."""
    # covered end-to-end in test_queue_downloader; here we assert the guard
    media = media_tree / "new-movies" / "Alien (1979).mkv"
    before = media.read_bytes()
    with pytest.raises(UnsafeWriteError):
        safe_write_subtitle(media, b"malicious", overwrite=True)
    assert media.read_bytes() == before
