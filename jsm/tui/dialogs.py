"""Modal dialogs: bulk confirmation and manual search."""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, RadioButton, RadioSet

BULK_CONFIRM_PHRASE = "DOWNLOAD ALL"


@dataclass
class BulkChoice:
    min_confidence: float
    sync: bool
    dry_run: bool


class BulkConfirmDialog(ModalScreen[BulkChoice | None]):
    """Typed-confirmation gate for bulk downloads."""

    BINDINGS = [("escape", "dismiss(None)", "Cancel")]

    def __init__(self, file_count: int, default_confidence: float, default_sync: bool):
        super().__init__()
        self.file_count = file_count
        self.default_confidence = default_confidence
        self.default_sync = default_sync

    def compose(self) -> ComposeResult:
        with Vertical(id="bulk-dialog", classes="dialog"):
            yield Label(
                f"[b]Bulk download[/b] - {self.file_count} file(s) will be queued",
                id="bulk-title",
            )
            yield Label("Minimum match confidence:")
            with RadioSet(id="bulk-confidence"):
                yield RadioButton(
                    "99% - hash matches only (recommended)",
                    value=self.default_confidence >= 0.99, name="0.99",
                )
                yield RadioButton(
                    "90% - very close filename matches",
                    value=0.90 <= self.default_confidence < 0.99, name="0.90",
                )
                yield RadioButton(
                    "Any - best available match", value=self.default_confidence < 0.90,
                    name="0.0",
                )
            yield Checkbox("Synchronize after download (ffsubsync)",
                           value=self.default_sync, id="bulk-sync")
            yield Checkbox("Dry run - preview only, download nothing",
                           value=False, id="bulk-dry")
            yield Label(f"Type [b]{BULK_CONFIRM_PHRASE}[/b] to confirm:")
            yield Input(placeholder=BULK_CONFIRM_PHRASE, id="bulk-confirm-input")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Start", variant="warning", id="bulk-start", disabled=True)
                yield Button("Cancel", id="bulk-cancel")

    def on_input_changed(self, event: Input.Changed) -> None:
        dry = self.query_one("#bulk-dry", Checkbox).value
        ok = event.value.strip() == BULK_CONFIRM_PHRASE or dry
        self.query_one("#bulk-start", Button).disabled = not ok

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "bulk-dry":
            typed = self.query_one("#bulk-confirm-input", Input).value.strip()
            ok = typed == BULK_CONFIRM_PHRASE or event.value
            self.query_one("#bulk-start", Button).disabled = not ok

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "bulk-cancel":
            self.dismiss(None)
            return
        radio = self.query_one("#bulk-confidence", RadioSet)
        pressed = radio.pressed_button
        confidence = float(pressed.name) if pressed and pressed.name else 0.99
        self.dismiss(
            BulkChoice(
                min_confidence=confidence,
                sync=self.query_one("#bulk-sync", Checkbox).value,
                dry_run=self.query_one("#bulk-dry", Checkbox).value,
            )
        )


@dataclass
class ManualSearch:
    query: str
    year: int | None
    language: str


class ManualSearchDialog(ModalScreen[ManualSearch | None]):
    """Override title/year/language for a tricky file."""

    BINDINGS = [("escape", "dismiss(None)", "Cancel")]

    def __init__(self, filename: str, default_query: str, default_year: int | None,
                 default_language: str):
        super().__init__()
        self.filename = filename
        self.default_query = default_query
        self.default_year = default_year
        self.default_language = default_language

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"[b]Manual search[/b] - {self.filename}")
            yield Label("Title:")
            yield Input(value=self.default_query, id="ms-query")
            yield Label("Year (optional):")
            yield Input(
                value=str(self.default_year) if self.default_year else "",
                id="ms-year", type="integer",
            )
            yield Label("Language (ISO code, e.g. en):")
            yield Input(value=self.default_language, id="ms-lang")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Search && download", variant="primary", id="ms-ok")
                yield Button("Cancel", id="ms-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ms-cancel":
            self.dismiss(None)
            return
        query = self.query_one("#ms-query", Input).value.strip()
        if not query:
            return
        year_text = self.query_one("#ms-year", Input).value.strip()
        language = self.query_one("#ms-lang", Input).value.strip() or self.default_language
        self.dismiss(
            ManualSearch(
                query=query,
                year=int(year_text) if year_text.isdigit() else None,
                language=language,
            )
        )
