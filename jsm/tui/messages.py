"""App-wide Textual messages."""

from __future__ import annotations

from textual.message import Message

from jsm.activity import LogEntry
from jsm.database.models import QueueJob
from jsm.scanner.filesystem import ScanStats


class ActivityLogged(Message):
    """A new line was appended to the shared activity log."""

    def __init__(self, entry: LogEntry):
        super().__init__()
        self.entry = entry


class JobUpdated(Message):
    def __init__(self, job: QueueJob, forwarded: bool = False):
        super().__init__()
        self.job = job
        # True when the app re-posts the update to the active screen. The
        # screen may not handle it, in which case it bubbles back up to the
        # app - the flag lets the app ignore its own echo instead of
        # processing (and forwarding) it again in an endless loop.
        self.forwarded = forwarded


class ScanFinished(Message):
    def __init__(self, directory: str, stats: ScanStats):
        super().__init__()
        self.directory = directory
        self.stats = stats
