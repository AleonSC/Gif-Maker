"""Sort keys: a key for each folder, so pressing it moves the file being viewed there."""

import os
import sys
import tkinter as tk
from tkinter import filedialog, ttk

from . import theme
from .tooltip import Tooltip

SETTING = "sort_keys"
# Plain keys only: letters, digits (top row or keypad) and the F keys GifMaker
# doesn't already use. Held Shift, Ctrl or Alt means it's not a sort key.
_F_KEYS = ("F1", "F4", "F6", "F7", "F8", "F9", "F10", "F12")
_MODIFIERS = 0x1 | 0x4 | (0x20000 if sys.platform == "win32" else 0x8)  # Shift, Ctrl, Alt


def key_name(event):
    """The sort key an event is ("a", "7", "F6"...), or None if it can't be one."""
    if event.state & _MODIFIERS:
        return None
    sym = event.keysym
    if sym.startswith("KP_") and sym[3:].isdigit():
        sym = sym[3:]
    if len(sym) == 1 and (sym.isdigit() or ("a" <= sym.lower() <= "z")):
        return sym.lower()  # Caps Lock gives "A"
    return sym if sym in _F_KEYS else None


def label(key):
    return key.upper() if len(key) == 1 else key


def load():
    """{key: folder} as saved."""
    saved = theme.load_setting(SETTING, {})
    return {k: f for k, f in saved.items() if isinstance(f, str)} if isinstance(saved, dict) else {}


def save(keys):
    theme.save_setting(SETTING, keys)


def _ordered(keys):
    return sorted(keys.items(), key=lambda kf: (len(kf[0]) > 1, kf[0].isalpha(), kf[0].zfill(3)))


def summary(keys):
    """A short list of the keys for tooltips."""
    if not keys:
        return "No sort keys yet."
    return "\n".join(f"{label(k)}  →  {f}" for k, f in _ordered(keys))


class SortKeysWindow(tk.Toplevel):
    """Set which key sorts to which folder. on_change(keys) gets each change."""

    def __init__(self, app, on_change):
        super().__init__(app.root)
        self.app, self.on_change = app, on_change
        self.keys = load()
        self.waiting = None  # the folder a key is being pressed for
        self.title("Sort keys")
        self.transient(app.root)
        theme.style_window(self)
        self._build()
        self._fill()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Key>", self._key_pressed)
        self.focus_set()

    def _build(self):
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="Press a key while viewing a file to move it to that key's folder "
                             "and open the next one. Ctrl+Z puts the last one back.",
                  style="Muted.TLabel", wraplength=460, justify="left").pack(anchor="w")
        table = ttk.Frame(main)
        table.pack(fill="both", expand=True, pady=(8, 8))
        self.tree = ttk.Treeview(table, columns=("key", "folder"), show="headings", height=9,
                                 selectmode="browse")
        self.tree.heading("key", text="Key")
        self.tree.heading("folder", text="Folder")
        self.tree.column("key", width=60, stretch=False, anchor="center")
        self.tree.column("folder", width=420)
        bar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.change_folder())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._buttons())

        buttons = ttk.Frame(main)
        buttons.pack(fill="x")
        add = ttk.Button(buttons, text="Add folder...", command=self.add)
        add.pack(side="left")
        Tooltip(add, "Pick a folder, then press the key for it")
        self.key_btn = ttk.Button(buttons, text="Change key", command=self.change_key)
        self.key_btn.pack(side="left", padx=(4, 0))
        self.folder_btn = ttk.Button(buttons, text="Change folder...", command=self.change_folder)
        self.folder_btn.pack(side="left", padx=(4, 0))
        self.remove_btn = ttk.Button(buttons, text="Remove", command=self.remove)
        self.remove_btn.pack(side="left", padx=(4, 0))
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right")
        self.prompt = ttk.Label(main, text="", style="Muted.TLabel")
        self.prompt.pack(anchor="w", pady=(8, 0))

    def _fill(self, select=None):
        self.tree.delete(*self.tree.get_children())
        for key, folder in _ordered(self.keys):
            missing = "" if os.path.isdir(folder) else "   (not found)"
            self.tree.insert("", "end", iid=key, values=(label(key), folder + missing))
        if select in self.keys:
            self.tree.selection_set(select)
            self.tree.see(select)
        self._buttons()

    def _selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _buttons(self):
        state = "normal" if self._selected() else "disabled"
        for button in (self.key_btn, self.folder_btn, self.remove_btn):
            button.config(state=state)

    def _changed(self, select=None):
        save(self.keys)
        self._fill(select)
        self.on_change(dict(self.keys))

    def _pick_folder(self, start=None):
        folder = filedialog.askdirectory(parent=self, title="Sort files into this folder",
                                         initialdir=start or None, mustexist=True)
        return os.path.normpath(folder) if folder else None

    def add(self):
        start = self.app.video_path and os.path.dirname(self.app.video_path)
        folder = self._pick_folder(start)
        if folder:
            self._wait_for_key(folder, None)

    def change_key(self):
        key = self._selected()
        if key:
            self._wait_for_key(self.keys[key], key)

    def change_folder(self):
        key = self._selected()
        if key:
            folder = self._pick_folder(self.keys[key])
            if folder:
                self.keys[key] = folder
                self._changed(key)

    def remove(self):
        key = self._selected()
        if key:
            del self.keys[key]
            self._changed()

    def _wait_for_key(self, folder, old_key):
        self.waiting = (folder, old_key)
        self.prompt.config(text=f"Press the key for {os.path.basename(folder) or folder}  "
                                "(a letter, a number, or F1, F4, F6–F10, F12; Esc to cancel)",
                           style="TLabel")
        self.focus_force()

    def _key_pressed(self, event):
        if not self.waiting:
            return None
        if event.keysym == "Escape":
            self.waiting = None
            self.prompt.config(text="", style="Muted.TLabel")
            return "break"
        key = key_name(event)
        if key is None:
            if event.keysym not in ("Shift_L", "Shift_R", "Control_L", "Control_R",
                                    "Alt_L", "Alt_R", "Caps_Lock", "Num_Lock"):
                self.prompt.config(text="That key can't be used. Try a letter, a number or "
                                        "an F key (Esc to cancel).")
            return "break"
        folder, old_key = self.waiting
        self.waiting = None
        replaced = self.keys.get(key)
        if old_key and old_key != key:
            del self.keys[old_key]
        self.keys[key] = folder
        self._changed(key)
        note = f" (it was {replaced})" if replaced and replaced != folder else ""
        self.prompt.config(text=f"{label(key)} now sorts into {folder}{note}.",
                           style="Muted.TLabel")
        return "break"
