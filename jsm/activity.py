"""A small in-memory activity log shared across the app.

Everything that "does something" (search, download, clean, sync, scan, config
changes) records a line here so the TUI can show a single, honest, scrollable
history - answering questions like "did that subtitle actually get synced, or
was it already in sync?" that a fleeting toast never could.

It is deliberately tiny and dependency-free: a bounded deque plus listeners.
Listeners are notified synchronously on the thread that called :meth:`add`;
the TUI's listener just posts a Textual message, which is thread-safe, so the
log works whether the entry came from the event loop or a worker thread.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Iterable

# Severity levels, in rough ascending order of "you should notice this".
INFO = "info"
OK = "ok"
WARN = "warn"
ERROR = "error"


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
    def __init__(self, maxlen: int = 1000):
        self._entries: Deque[LogEntry] = deque(maxlen=maxlen)
        self._listeners: list[Listener] = []

    def add(self, message: str, level: str = INFO) -> LogEntry:
        entry = LogEntry(time.time(), level, message)
        self._entries.append(entry)
        for listener in list(self._listeners):
            try:
                listener(entry)
            except Exception:
                # A misbehaving listener must never break the thing being logged.
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

    def entries(self) -> Iterable[LogEntry]:
        return tuple(self._entries)

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass
