"""Hover hints for widgets."""

import tkinter as tk

from . import theme


class Tooltip:
    """Shows a small hint next to a widget while the mouse is over it.

    text can be a string or a function returning one (worked out on hover).
    """

    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _show(self, _event):
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        text = self.text() if callable(self.text) else self.text
        tk.Label(self.tip, text=text, background=theme.colors["tip"],
                 foreground=theme.colors["tip_fg"], relief="solid",
                 borderwidth=1, padx=6, pady=2).pack()

    def _hide(self, _event):
        if self.tip:
            self.tip.destroy()
            self.tip = None
