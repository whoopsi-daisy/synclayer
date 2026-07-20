"""In-app menu and editors: change config, credentials and theme without
leaving the application.

- :class:`OptionsMenu` is the top-left menu (also reachable with Ctrl+O).
- :class:`FileEditScreen` edits config.toml / accounts.conf in a text area,
  validates on save, writes the file (preserving 0600 on the credentials
  file), and hot-reloads the running context so changes take effect at once.
- :class:`ThemeScreen` switches the color theme live.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    TextArea,
)

from jsm.config import settings as config
from jsm.config.settings import Settings


class OptionsMenu(ModalScreen[None]):
    """The application menu (top-left / Ctrl+O)."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]

    _ITEMS = [
        ("config", "⚙  Edit configuration (config.toml)"),
        ("credentials", "🔑  Edit accounts / credentials"),
        ("theme", "🎨  Change theme"),
        ("log", "📜  Activity log"),
        ("help", "❓  Keys & help"),
        ("quit", "⏻  Quit Synclayer"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="menu-dialog"):
            yield Label("[b]Menu[/b]", id="menu-title")
            with ListView(id="menu-list"):
                for key, label in self._ITEMS:
                    yield ListItem(Static(label), id=f"menu-{key}")
            yield Label("[dim]Enter to choose · Esc to close[/dim]")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        key = (event.item.id or "").removeprefix("menu-")
        self.dismiss(None)
        self.app.run_menu_action(key)


class FileEditScreen(ModalScreen[bool]):
    """Edit a config/credentials file, validate, save and hot-reload."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, path: Path, title: str, *, is_toml: bool,
                 private: bool = False, help_text: str = ""):
        super().__init__()
        self.path = path
        self.title_text = title
        self.is_toml = is_toml
        self.private = private
        self.help_text = help_text

    def compose(self) -> ComposeResult:
        try:
            content = self.path.read_text(encoding="utf-8")
        except OSError:
            content = ""
        with Vertical(classes="dialog", id="edit-dialog"):
            yield Label(f"[b]{self.title_text}[/b]", id="edit-title")
            yield Static(f"[dim]{self.path}[/dim]")
            if self.help_text:
                yield Static(self.help_text, id="edit-help")
            yield TextArea(content, id="edit-area", show_line_numbers=True)
            yield Static("", id="edit-error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Save  (Ctrl+S)", variant="primary", id="edit-save")
                yield Button("Cancel  (Esc)", id="edit-cancel")

    def on_mount(self) -> None:
        self.query_one("#edit-area", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "edit-save":
            self.action_save()
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_save(self) -> None:
        text = self.query_one("#edit-area", TextArea).text
        if self.is_toml:
            try:
                tomllib.loads(text)
            except tomllib.TOMLDecodeError as exc:
                self.query_one("#edit-error", Static).update(
                    f"[red]Not valid TOML - not saved:[/red] {exc}"
                )
                return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(text, encoding="utf-8")
            if self.private:
                os.chmod(self.path, 0o600)
        except OSError as exc:
            self.query_one("#edit-error", Static).update(
                f"[red]Could not write file:[/red] {exc}"
            )
            return
        self.app.ctx.reload_config()
        self.app.notify(f"Saved {self.path.name} and reloaded", timeout=5)
        self.dismiss(True)


class ConfigFormScreen(ModalScreen[bool]):
    """A structured form for config.toml - safer than editing raw TOML."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings

    def compose(self) -> ComposeResult:
        s = self.settings
        with Vertical(classes="dialog", id="configform-dialog"):
            yield Label("[b]Configuration[/b]", id="configform-title")
            with VerticalScroll(id="configform-body"):
                yield Label("Library folders (one per line):")
                yield TextArea("\n".join(s.libraries), id="cf-libraries")
                yield Label("Languages (comma-separated ISO codes, primary first):")
                yield Input(", ".join(s.languages), id="cf-languages")
                yield Label("OpenSubtitles API key (blank = built-in default):")
                yield Input(s.api_key, id="cf-apikey")
                yield Checkbox("Sync with ffsubsync after each download",
                               value=s.sync_by_default, id="cf-sync")
                yield Checkbox("Clean with subscleaner after each download",
                               value=s.clean_by_default, id="cf-clean")
                yield Label("Bulk minimum confidence (0-1, 0.99 = hash only):")
                yield Input(str(s.bulk_min_confidence), id="cf-confidence",
                            type="number")
                yield Label("Concurrent download jobs:")
                yield Input(str(s.queue_concurrency), id="cf-concurrency",
                            type="integer")
                yield Label("subscleaner path (blank = auto-detect):")
                yield Input(s.subscleaner_path, id="cf-subscleaner")
                yield Label("ffsubsync path (blank = auto-detect):")
                yield Input(s.ffsubsync_path, id="cf-ffsubsync")
                yield Label("ffprobe path (blank = auto-detect):")
                yield Input(s.ffprobe_path, id="cf-ffprobe")
            yield Static("", id="cf-error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Save  (Ctrl+S)", variant="primary", id="cf-save")
                yield Button("Edit raw TOML", id="cf-raw")
                yield Button("Cancel  (Esc)", id="cf-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cf-save":
            self.action_save()
        elif event.button.id == "cf-raw":
            # Escape hatch to the raw editor for anything the form omits.
            self.dismiss(False)
            self.app.push_screen(FileEditScreen(
                config.config_file(), "Edit configuration (raw TOML)",
                is_toml=True,
            ))
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def _error(self, message: str) -> None:
        self.query_one("#cf-error", Static).update(f"[red]{message}[/red]")

    def action_save(self) -> None:
        libraries = [
            line.strip()
            for line in self.query_one("#cf-libraries", TextArea).text.splitlines()
            if line.strip()
        ]
        languages = [
            part.strip()
            for part in self.query_one("#cf-languages", Input).value.split(",")
            if part.strip()
        ]
        if not languages:
            self._error("At least one language is required.")
            return
        try:
            confidence = float(self.query_one("#cf-confidence", Input).value or "0")
        except ValueError:
            self._error("Bulk confidence must be a number between 0 and 1.")
            return
        confidence = max(0.0, min(confidence, 1.0))
        try:
            concurrency = int(self.query_one("#cf-concurrency", Input).value or "1")
        except ValueError:
            self._error("Concurrent jobs must be a whole number.")
            return

        new = Settings(
            libraries=libraries,
            languages=languages,
            api_key=self.query_one("#cf-apikey", Input).value.strip(),
            sync_by_default=self.query_one("#cf-sync", Checkbox).value,
            clean_by_default=self.query_one("#cf-clean", Checkbox).value,
            bulk_min_confidence=confidence,
            queue_concurrency=max(1, concurrency),
            subscleaner_path=self.query_one("#cf-subscleaner", Input).value.strip(),
            ffsubsync_path=self.query_one("#cf-ffsubsync", Input).value.strip(),
            ffprobe_path=self.query_one("#cf-ffprobe", Input).value.strip(),
        )
        try:
            config.save_settings(new)
        except OSError as exc:
            self._error(f"Could not write config: {exc}")
            return
        self.app.ctx.reload_config()
        self.app.notify("Configuration saved and reloaded", timeout=5)
        self.dismiss(True)


class ThemeScreen(ModalScreen[None]):
    """Pick a color theme; applies instantly on highlight."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]

    # A curated shortlist (Dracula first) out of Textual's built-ins.
    _THEMES = [
        "dracula", "textual-dark", "nord", "gruvbox", "monokai",
        "tokyo-night", "catppuccin-mocha", "solarized-dark",
        "textual-light", "catppuccin-latte", "solarized-light",
    ]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="theme-dialog"):
            yield Label("[b]Theme[/b]  [dim]arrows preview · Enter keeps · Esc reverts[/dim]")
            with ListView(id="theme-list"):
                for name in self._THEMES:
                    yield ListItem(Static(name), id=f"theme-{name}")

    def on_mount(self) -> None:
        self._original = self.app.theme
        lv = self.query_one("#theme-list", ListView)
        if self.app.theme in self._THEMES:
            lv.index = self._THEMES.index(self.app.theme)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item and event.item.id:
            self.app.theme = event.item.id.removeprefix("theme-")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.app.notify(f"Theme: {self.app.theme}", timeout=3)
        self.dismiss(None)

    def action_dismiss(self, result=None) -> None:  # Esc reverts the preview
        self.app.theme = self._original
        self.dismiss(None)
