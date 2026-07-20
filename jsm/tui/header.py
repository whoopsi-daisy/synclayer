"""A Header whose top-left corner opens the app menu.

Textual's stock Header shows a command-palette icon; we repurpose the whole
header as a click target for the in-app menu (config, credentials, theme, log)
so there is an obvious "top-left corner" affordance, matching the keyboard
shortcut shown in the footer.
"""

from __future__ import annotations

from textual.widgets import Header


class MenuHeader(Header):
    def __init__(self, **kwargs) -> None:
        super().__init__(show_clock=True, **kwargs)

    def on_mount(self) -> None:
        # A hamburger hints "there is a menu here".
        self.icon = "☰"

    def on_click(self) -> None:
        action = getattr(self.app, "action_menu", None)
        if action is not None:
            action()
