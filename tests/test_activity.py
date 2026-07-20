"""The shared activity log: in-memory tiers, file sink, trace and tracebacks."""

from jsm.activity import ActivityLog


def test_memory_log_and_levels():
    log = ActivityLog()
    log.info("hello")
    log.ok("done")
    log.warn("careful")
    log.error("boom")
    levels = [e.level for e in log.entries()]
    assert levels == ["info", "ok", "warn", "error"]


def test_trace_is_file_only(tmp_path):
    log = ActivityLog()
    path = tmp_path / "logs" / "synclayer.log"
    log.attach_file(path)
    log.info("shown in ui")
    log.trace("verbose detail only in file")
    # trace never reaches the in-memory/UI log...
    messages = [e.message for e in log.entries()]
    assert "shown in ui" in messages
    assert all("verbose detail" not in m for m in messages)
    # ...but it is written to the file.
    log.close()
    text = path.read_text()
    assert "shown in ui" in text
    assert "verbose detail only in file" in text
    assert "TRACE" in text


def test_exception_writes_traceback_to_file(tmp_path):
    log = ActivityLog()
    path = tmp_path / "synclayer.log"
    log.attach_file(path)
    try:
        raise ValueError("kaboom")
    except ValueError as exc:
        entry = log.exception("job failed", exc)
    # UI sees a short error line, no traceback noise.
    assert entry.level == "error"
    assert entry.message == "job failed"
    log.close()
    text = path.read_text()
    assert "Traceback (most recent call last)" in text
    assert "ValueError: kaboom" in text


def test_listeners_and_file_survive_bad_listener(tmp_path):
    log = ActivityLog()
    path = tmp_path / "synclayer.log"
    log.attach_file(path)
    seen = []
    log.subscribe(lambda e: seen.append(e.message))
    log.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("bad listener")))
    log.info("still works")
    assert seen == ["still works"]
    log.close()
    assert "still works" in path.read_text()


def test_attach_file_never_raises_on_bad_path(tmp_path):
    log = ActivityLog()
    # A path whose parent cannot be created should be swallowed.
    log.attach_file(tmp_path / "a-file")  # make a file, then nest under it
    (tmp_path / "blocker").write_text("x")
    log2 = ActivityLog()
    log2.attach_file(tmp_path / "blocker" / "nested" / "log.txt")
    log2.info("does not crash")  # no exception
