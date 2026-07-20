"""A small activity log shared across the app, with an optional file sink.

Everything that "does something" (search, download, clean, sync, scan, config
changes) records a line here so the TUI can show a single, honest, scrollable
history - answering questions like "did that subtitle actually get synced, or
was it already in sync?" that a fleeting toast never could.

Two tiers, on purpose:

- the **in-memory** log (a bounded deque + listeners) feeds the TUI and stays
  concise - only the lines a human wants to see;
- the **file** sink (``logs/synclayer.log``) is verbose and durable: it gets
  every in-memory line *plus* low-level ``trace()`` progress and full
  ``exception()`` tracebacks, and it survives the process. When something
  breaks, that file is what you send back to get help.

Listeners are notified synchronously on the thread that called :meth:`add`;
the TUI's listener just posts a Textual message, which is thread-safe, so the
log works whether the entry came from the event loop or a worker thread.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Iterable

# Severity levels, in rough ascending order of "you should notice this".
INFO = "info"
OK = "ok"
WARN = "warn"
ERROR = "error"
TRACE = "trace"  # file-only: verbose progress, never shown in the UI


@dataclass(frozen=True)
class LogEntry:
    timestamp: float
    level: str
    message: str

    @property
    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.timestamp))


Listener = Callable[[LogEntry], None]


class ActivityLog:
    def __init__(self, maxlen: int = 2000):
        self._entries: Deque[LogEntry] = deque(maxlen=maxlen)
        self._listeners: list[Listener] = []
        self._file = None
        self._file_lock = threading.Lock()

    # ------------------------------------------------------------- file sink

    def attach_file(self, path: str | Path) -> None:
        """Start appending every entry (incl. trace/exception) to *path*."""
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(p, "a", encoding="utf-8", buffering=1)  # line-buffered
        except OSError:
            self._file = None  # logging must never be fatal

    def close(self) -> None:
        with self._file_lock:
            if self._file is not None:
                try:
                    self._file.close()
                finally:
                    self._file = None

    def _write_file(self, entry: LogEntry) -> None:
        if self._file is None:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.timestamp))
        line = f"{stamp} {entry.level.upper():5} {entry.message}\n"
        with self._file_lock:
            if self._file is not None:
                try:
                    self._file.write(line)
                except OSError:
                    pass

    # ---------------------------------------------------------------- adding

    def add(self, message: str, level: str = INFO) -> LogEntry:
        entry = LogEntry(time.time(), level, message)
        self._write_file(entry)
        # TRACE lines are file-only: they never touch the UI deque/listeners.
        if level != TRACE:
            self._entries.append(entry)
            for listener in list(self._listeners):
                try:
                    listener(entry)
                except Exception:
                    # A misbehaving listener must never break what's logged.
                    pass
        return entry

    # Convenience wrappers.
    def info(self, message: str) -> LogEntry:
        return self.add(message, INFO)

    def ok(self, message: str) -> LogEntry:
        return self.add(message, OK)

    def warn(self, message: str) -> LogEntry:
        return self.add(message, WARN)

    def error(self, message: str) -> LogEntry:
        return self.add(message, ERROR)

    def trace(self, message: str) -> None:
        """Verbose, file-only detail (progress lines, diagnostics)."""
        self.add(message, TRACE)

    def exception(self, message: str, exc: BaseException) -> LogEntry:
        """Record a short error line for the UI and the full traceback to file."""
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.trace(f"{message}\n{tb.rstrip()}")
        return self.error(message)

    # --------------------------------------------------------------- reading

    def entries(self) -> Iterable[LogEntry]:
        return tuple(self._entries)

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass
