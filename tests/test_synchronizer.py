"""ffsubsync wrapper: progress streaming and safe replacement."""

import os
import stat

import pytest

from jsm import tools
from jsm.subtitles import synchronizer


@pytest.fixture(autouse=True)
def _clear_paths():
    tools._configured_paths.clear()
    yield
    tools._configured_paths.clear()


def _fake_ffsubsync(tmp_path, body: str):
    script = tmp_path / "ffsubsync"
    script.write_text("#!/usr/bin/env python3\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    tools._configured_paths["ffsubsync"] = str(script)
    return script


async def test_progress_lines_streamed(tmp_path):
    _fake_ffsubsync(tmp_path, (
        "import sys\n"
        "a = sys.argv\n"
        "out = a[a.index('-o')+1]; inp = a[a.index('-i')+1]\n"
        "for l in ['Parsing video...', 'Extracting speech segments...', 'offset 0.4s']:\n"
        "    print(l, file=sys.stderr, flush=True)\n"
        "open(out,'w').write(open(inp).read())\n"
    ))
    sub = tmp_path / "m.srt"
    sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    vid = tmp_path / "m.mkv"
    vid.write_bytes(b"\0" * 500)

    seen = []
    ok, message = await synchronizer.synchronize(
        vid, sub, on_progress=lambda line: seen.append(line)
    )
    assert ok, message
    assert "Extracting speech segments..." in seen
    assert any("offset" in s for s in seen)
    assert (tmp_path / "m.srt.bak").exists()  # original preserved


async def test_nonzero_exit_reports_error(tmp_path):
    _fake_ffsubsync(tmp_path, (
        "import sys\n"
        "print('something went wrong', file=sys.stderr)\n"
        "sys.exit(3)\n"
    ))
    sub = tmp_path / "m.srt"
    sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    vid = tmp_path / "m.mkv"
    vid.write_bytes(b"\0" * 500)
    ok, message = await synchronizer.synchronize(vid, sub)
    assert ok is False
    assert "exit 3" in message
    assert "something went wrong" in message
    # A failed sync must not touch the original.
    assert not (tmp_path / "m.srt.bak").exists()


async def test_missing_tool_is_graceful(tmp_path, monkeypatch):
    monkeypatch.setattr(synchronizer, "ffsubsync_available", lambda: False)
    sub = tmp_path / "m.srt"
    sub.write_text("x")
    vid = tmp_path / "m.mkv"
    vid.write_bytes(b"\0")
    ok, message = await synchronizer.synchronize(vid, sub)
    assert ok is False
    assert "ffsubsync" in message
