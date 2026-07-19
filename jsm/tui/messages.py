"""App-wide Textual messages."""

from __future__ import annotations

from textual.message import Message

from jsm.database.models import QueueJob
from jsm.scanner.filesystem import ScanStats


class JobUpdated(Message):
    def __init__(self, job: QueueJob):
        super().__init__()
        self.job = job


class ScanFinished(Message):
    def __init__(self, directory: str, stats: ScanStats):
        super().__init__()
        self.directory = directory
        self.stats = stats
