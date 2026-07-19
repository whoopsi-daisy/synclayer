import os
import pathlib

import pytest

from jsm.database.db import Database
from jsm.providers.base import SubtitleCandidate, SubtitleProvider
from jsm.scanner.filesystem import Scanner

SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHello world\n"


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("JSM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("JSM_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def media_tree(tmp_path) -> pathlib.Path:
    root = tmp_path / "media"
    movies = root / "new-movies"
    movies.mkdir(parents=True)
    (movies / "Alien (1979).mkv").write_bytes(b"a" * 200_000)
    (movies / "Alien (1979).en.srt").write_bytes(SRT)
    (movies / "Winnie The Pooh (2011).mkv").write_bytes(b"w" * 200_000)
    (movies / "DuckTales (1990).mkv").write_bytes(b"d" * 200_000)
    (movies / "DuckTales (1990).de.srt").write_bytes(SRT)
    tv = root / "tv"
    tv.mkdir()
    (tv / "Show S01E01.mkv").write_bytes(b"s" * 200_000)
    return root


@pytest.fixture
def scanner(db):
    return Scanner(db, ["en"])


class FakeProvider(SubtitleProvider):
    name = "fake"

    def __init__(self, candidates=None, content=SRT, fail=None):
        self.candidates = candidates
        self.content = content
        self.fail = fail
        self.download_count = 0

    async def search(self, languages, moviehash=None, query=None, year=None):
        if self.fail:
            raise self.fail
        if self.candidates is not None:
            return list(self.candidates)
        return [
            SubtitleCandidate(
                provider="fake", file_id="42", language=languages[0],
                release_name=f"{query} 1080p", moviehash_match=moviehash is not None,
                downloads=10,
            )
        ]

    async def download(self, candidate):
        if self.fail:
            raise self.fail
        self.download_count += 1
        return self.content


@pytest.fixture
def fake_provider():
    return FakeProvider()
