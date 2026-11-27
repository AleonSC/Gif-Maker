"""Toolbars in labelled groups, the same in every tool window: each group has a small
heading over its buttons, and a thin line between groups."""

from tkinter import ttk


def group(bar, title, side="left"):
    """A group on bar, headed title; put its buttons in the frame this returns
    (packed side by side)."""
    count = getattr(bar, "_groups", {}).get(side, 0)
    bar._groups = {**getattr(bar, "_groups", {}), side: count + 1}
    if count:  # a line between this group and the one before it on this side
        ttk.Separator(bar, orient="vertical").pack(side=side, fill="y", padx=10, pady=2)
    box = ttk.Frame(bar)
    box.pack(side=side, fill="y")
    ttk.Label(box, text=title, style="Muted.TLabel").pack(anchor="w")
    inner = ttk.Frame(box)
    inner.pack(anchor="w", pady=(2, 0))
    return inner


def add(frame, widget, gap=4):
    """Pack widget into a group made by group(), with a small gap before it. Buttons
    are made only as wide as their text needs."""
    if isinstance(widget, ttk.Button) and not str(widget.cget("width")):
        widget.config(width=max(3, len(str(widget.cget("text")))))
    widget.pack(side="left", padx=(gap if frame.winfo_children()[0] is not widget else 0, 0))
    return widget
