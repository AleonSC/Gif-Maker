"""Right-click a thumbnail in Files: a pop-up at the mouse, like Explorer's right-click
menu, with a big preview of the file and its menu docked beside it.

The picture (or the video, playing silently on a loop) is on the left, the menu
(open, rename, move or copy to a folder, show in folder, copy path, delete) on the
right. Like a menu, it closes when you pick something, press Esc or click anywhere
else.
"""

import os
import threading
import time
import tkinter as tk
from tkinter import font, ttk

from . import ffmpeg_tools as ff
from . import seen
from . import sort_keys
from . import theme
from . import ui_thread
from .player import Player
from .screen_recorder import cursor_monitor

MAX_FPS = 30
MIN_PREVIEW = (240, 180)  # the preview area is never smaller than this


def _human_size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


class QuickLook(tk.Toplevel):
    """One pop-up, reused: show() points it at a file and places it at the mouse."""

    def __init__(self, app):
        super().__init__(app.root)
        self.withdraw()
        self.overrideredirect(True)  # no title bar: a pop-up, like a menu
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.app = app
        self.path = None
        self.info = None
        self.token = 0  # bumped on each file: stale frame grabs are dropped
        self.photo = None
        self.anchor = (0, 0)  # where the mouse was
        self.screen = (0, 0, 0, 0)  # the monitor it was opened on: x, y, width, height
        self.renaming = None
        self.shown_at = 0.0
        self.items = []  # (row frame, labels) of the menu, for hover colours
        self.player = Player(self, app.ffmpeg, self._on_frame, self._on_end)
        self.menu_font = font.nametofont("TkMenuFont")
        self.bold_font = self.menu_font.copy()
        self.bold_font.configure(weight="bold")
        self._build()
        self.bind("<Escape>", lambda e: self._escape())
        self.bind("<Return>", lambda e: self._key(e, self.open_in_main))
        self.bind("<F2>", lambda e: self._key(e, self.start_rename))
        self.bind("<Delete>", lambda e: self._key(e, self.delete))
        self.bind("<Control-e>", lambda e: self._key(e, self.reveal))
        for key, state in (("s", seen.SEEN), ("u", seen.UNSEEN)):
            for k in (key, key.upper()):
                self.bind(k, lambda e, st=state: self._key(e, lambda: self.mark_as(st)))
        self.bind("<ButtonPress>", self._press, add="+")
        self.bind("<FocusOut>", lambda e: self.after(80, self._check_focus))

    # ------------------------------------------------------------ layout
    def _build(self):
        self.border = tk.Frame(self, bd=0, highlightthickness=1)
        self.border.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(self.border, bg="#111", highlightthickness=0,
                                width=MIN_PREVIEW[0], height=MIN_PREVIEW[1])
        self.canvas.pack(side="left", fill="both")
        self.canvas_img = self.canvas.create_image(0, 0, anchor="center")
        self.canvas_text = self.canvas.create_text(0, 0, fill="#888", anchor="center")
        self.canvas.bind("<Double-Button-1>", lambda e: self.open_in_main())
        self.menu = tk.Frame(self.border, bd=0)
        self.menu.pack(side="left", fill="y")

    def _colors(self):
        c = theme.colors
        return c.get("bg", "#f0f0f0"), c.get("fg", "#000"), c.get("muted", "#666"), \
            c.get("select", "#0078d7"), c.get("select_fg", "#fff"), c.get("border", "#999")

    def _fill_menu(self):
        bg, fg, muted, _, _, border = self._colors()
        self.border.config(bg=bg, highlightbackground=border, highlightcolor=border)
        self.menu.config(bg=bg)
        for child in self.menu.winfo_children():
            child.destroy()
        self.items = []
        head = tk.Frame(self.menu, bg=bg, padx=12, pady=8)
        head.pack(fill="x")
        self.name_label = tk.Label(head, bg=bg, fg=fg, font=self.bold_font, anchor="w",
                                   justify="left", wraplength=260)
        self.name_label.pack(fill="x")
        self.rename_holder = tk.Frame(head, bg=bg)
        self.rename_holder.pack(fill="x")
        self.details = tk.Label(head, bg=bg, fg=muted, font=self.menu_font, anchor="w",
                                justify="left", wraplength=260)
        self.details.pack(fill="x")
        self._separator()
        self._item("Open", "Enter", self.open_in_main, bold=True)
        self._separator()
        state = seen.get(self.path) if self.path else None
        if state != seen.SEEN:
            self._item("Mark as seen", "S", lambda: self.mark_as(seen.SEEN),
                       color=seen.COLORS[seen.SEEN])
        if state != seen.UNSEEN:
            self._item("Mark as unseen", "U", lambda: self.mark_as(seen.UNSEEN),
                       color=seen.COLORS[seen.UNSEEN])
        if state:
            self._item("Clear mark", "", lambda: self.mark_as(None))
        self._separator()
        self._item("Rename", "F2", self.start_rename)
        self._item("Move to folder...", "", lambda: self.move_or_copy(copy=False))
        self._item("Copy to folder...", "", lambda: self.move_or_copy(copy=True))
        keys = getattr(self.app, "sort_keys", {}) or {}
        for key, folder in sort_keys._ordered(keys):
            name = os.path.basename(os.path.normpath(folder)) or folder
            self._item(f"Move to {name}", sort_keys.label(key),
                       lambda f=folder: self.move_to(f))
        self._separator()
        self._item("Show in folder", "Ctrl+E", self.reveal)
        self._item("Copy path", "", self.copy_path)
        self._separator()
        self._item("Delete", "Del", self.delete)
        tk.Frame(self.menu, bg=bg, height=4).pack(fill="x")

    def _separator(self):
        bg, _, _, _, _, border = self._colors()
        holder = tk.Frame(self.menu, bg=bg, padx=8, pady=3)
        holder.pack(fill="x")
        tk.Frame(holder, bg=border, height=1).pack(fill="x")

    def _item(self, text, shortcut, command, bold=False, color=None):
        bg, fg, muted, sel, sel_fg, _ = self._colors()
        row = tk.Frame(self.menu, bg=bg, padx=12, pady=4, cursor="hand2")
        row.pack(fill="x", padx=3)
        if color:  # a swatch of the mark's colour
            swatch = tk.Frame(row, bg=color, width=10, height=10)
            swatch.pack(side="left", padx=(0, 6))
            swatch.bind("<ButtonRelease-1>", lambda e: command())
        name = tk.Label(row, text=text, bg=bg, fg=fg, anchor="w",
                        font=self.bold_font if bold else self.menu_font)
        name.pack(side="left")
        key = tk.Label(row, text=shortcut, bg=bg, fg=muted, anchor="e", font=self.menu_font)
        key.pack(side="right", padx=(30, 0))
        widgets = (row, name, key)

        def hover(on):
            for w in widgets:
                w.config(bg=sel if on else bg)
            name.config(fg=sel_fg if on else fg)
            key.config(fg=sel_fg if on else muted)

        for w in widgets:
            w.bind("<Enter>", lambda e: hover(True))
            w.bind("<Leave>", lambda e: hover(False))
            w.bind("<ButtonRelease-1>", lambda e: command())
        self.items.append(widgets)

    # ----------------------------------------------------------- showing
    def show(self, path, x, y):
        """Pop up for path with its corner at the mouse (x, y on the screen)."""
        self._cancel_rename()
        self.player.stop()
        self.token += 1
        self.shown_at = time.monotonic()
        self.path = path
        self.info = None
        self.photo = None
        self.anchor = (x, y)
        self.screen = cursor_monitor(self.app.root)
        self._fill_menu()
        self._update_labels()
        self._size_preview(*MIN_PREVIEW)
        self.canvas.itemconfig(self.canvas_img, image="")
        self.canvas.itemconfig(self.canvas_text, text="Loading...")
        self.deiconify()
        self._place()
        self.lift()
        self.focus_force()
        try:
            self.grab_set()  # clicks elsewhere in GifMaker come here, to close it
        except tk.TclError:
            pass
        token = self.token

        def work():
            try:
                info = ff.probe(self.app.ffmpeg, path)
            except Exception as e:  # noqa: BLE001 - any failure: say so in the pop-up
                info = e
            ui_thread.call(lambda: self._probed(token, info))

        threading.Thread(target=work, daemon=True).start()

    def _max_preview(self):
        """The biggest the picture can be: the monitor less the menu beside it."""
        _, _, sw, sh = self.screen
        self.update_idletasks()
        margin = 40  # breathing room and the border
        return (max(MIN_PREVIEW[0], sw - self.menu.winfo_reqwidth() - margin),
                max(MIN_PREVIEW[1], sh - margin))

    def _size_preview(self, w, h):
        self.canvas.config(width=w, height=h)
        self.canvas.coords(self.canvas_img, w // 2, h // 2)
        self.canvas.coords(self.canvas_text, w // 2, h // 2)
        self.canvas.itemconfig(self.canvas_text, width=max(100, w - 20))

    def _place(self):
        """Put it at the mouse, flipped left or up where it would go off the screen."""
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        left, top, sw, sh = self.screen
        x, y = self.anchor
        if x + w > left + sw:
            x = x - w
        if y + h > top + sh:
            y = top + sh - h
        x = min(max(x, left), left + max(0, sw - w))
        y = min(max(y, top), top + max(0, sh - h))
        self.geometry(f"+{x}+{y}")

    def _probed(self, token, info):
        if token != self.token or not self.winfo_exists():
            return
        if isinstance(info, Exception):
            self.canvas.itemconfig(self.canvas_text, text=f"Can't show this file.\n\n{info}"[:300])
            return
        self.info = info
        self._update_labels()
        w, h = ff.fit_size(info.width, info.height, *self._max_preview(), upscale=True)
        self._size_preview(max(w, MIN_PREVIEW[0]), max(h, MIN_PREVIEW[1]))
        self._place()
        if info.duration > 0 and not ff.is_image(self.path):
            self.player.muted = True
            self.player.start(self.path, 0.0, (w, h), min(info.fps or 25.0, MAX_FPS))
            return
        path = self.path

        def work():
            data = ff.grab_frame(self.app.ffmpeg, path, 0.0, w, h)
            ui_thread.call(lambda: self._show_still(token, data))

        threading.Thread(target=work, daemon=True).start()

    def _update_labels(self):
        path = self.path
        self.name_label.config(text=os.path.basename(path) if path else "")
        parts = []
        try:
            parts.append(_human_size(os.stat(path).st_size))
        except (OSError, TypeError):
            pass
        info = self.info
        if info:
            if info.width:
                parts.append(f"{info.width} × {info.height}")
            if info.duration and not ff.is_image(path):
                parts.append(ff.format_time(info.duration).split(".")[0])
        self.details.config(text="   ".join(parts))

    def _show_still(self, token, data):
        if token != self.token or not self.winfo_exists():
            return
        if not data:
            self.canvas.itemconfig(self.canvas_text, text="Can't show this file.")
            return
        self._on_frame(0, data)

    def _on_frame(self, _t, data):
        self.photo = tk.PhotoImage(master=self, data=data, format="ppm")
        self.canvas.itemconfig(self.canvas_img, image=self.photo)
        self.canvas.itemconfig(self.canvas_text, text="")

    def _on_end(self):
        if self.path and self.info:
            token = self.token
            self.after(10, lambda: token == self.token and self._probed(token, self.info))

    # ----------------------------------------------------- closing it
    def _press(self, event):
        """A click outside the pop-up (it has the grab, so they all come here) closes it."""
        x, y = event.x_root, event.y_root
        left, top = self.winfo_rootx(), self.winfo_rooty()
        if not (left <= x < left + self.winfo_width() and top <= y < top + self.winfo_height()):
            self.close()
            return "break"
        return None

    def _check_focus(self):
        # Focus can blink away for a moment when the menu is rebuilt or the rename box
        # goes: only close if it has really gone to another window or app.
        if self.path is None or time.monotonic() - self.shown_at < 0.5:
            return
        try:
            under = self.winfo_containing(*self.winfo_pointerxy())
        except (KeyError, tk.TclError):
            under = None
        if under is not None and str(under).startswith(str(self)):
            return
        try:
            focus = self.focus_get()
        except (KeyError, tk.TclError):
            focus = None
        if focus is None or not str(focus).startswith(str(self)):
            self.close()  # another window or app was clicked

    def _key(self, event, action):
        if isinstance(event.widget, (tk.Entry, ttk.Entry)):
            return None
        action()
        return "break"

    def _escape(self):
        if self.renaming is not None:
            self._cancel_rename()
        else:
            self.close()

    def close(self):
        self._cancel_rename()
        self.player.stop()
        self.token += 1
        self.path = None
        self.photo = None
        self.canvas.itemconfig(self.canvas_img, image="")
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.withdraw()  # kept for next time: opens instantly

    def _take(self):
        """Close the pop-up and return the file it was for (None if it's gone)."""
        path = self.path
        self.close()
        if not path:
            return None
        if self.app.busy:
            self.app.status.set("Wait for the save to finish (or cancel it) first.")
            return None
        if not os.path.exists(path):
            self.app.status.set(f"{os.path.basename(path)} isn't there any more.")
            return None
        return path

    # ----------------------------------------------------------- actions
    # (done by app.actions, a FileActions, so the library does them the same way)
    def mark_as(self, state):
        path = self.path
        self.close()
        self.app.actions.mark(path, state)

    def open_in_main(self):
        path = self.path
        self.close()
        if path:
            self.app._browser_open(path)

    def reveal(self):
        path = self._take()
        self.app.actions.reveal(path)

    def copy_path(self):
        path = self.path
        self.close()
        self.app.actions.copy_path(path)

    def delete(self):
        path = self._take()
        self.app.actions.delete(path)

    def move_or_copy(self, copy):
        path = self._take()
        if path:
            self.app.actions.move_or_copy(path, copy)

    def move_to(self, folder):
        path = self._take()
        if path:
            self.app.actions.move_to(path, folder)

    # ------------------------------------------------------------ rename
    def start_rename(self):
        """Edit the name in place, at the top of the menu; Enter renames."""
        if not self.path or self.renaming is not None:
            return
        stem = os.path.splitext(os.path.basename(self.path))[0]
        entry = self.renaming = ttk.Entry(self.rename_holder, width=30)
        entry.insert(0, stem)
        entry.select_range(0, "end")
        self.name_label.pack_forget()
        entry.pack(fill="x", pady=(0, 2))
        entry.focus_set()
        entry.bind("<Return>", lambda e: self._finish_rename() or "break")
        entry.bind("<Escape>", lambda e: self._cancel_rename() or "break")

    def _cancel_rename(self):
        entry, self.renaming = self.renaming, None
        if entry is not None:
            entry.destroy()
            self.name_label.pack(fill="x", before=self.rename_holder)
            self.focus_set()

    def _finish_rename(self):
        entry = self.renaming
        if entry is None:
            return
        name = entry.get()
        path = self._take()  # closes the pop-up (and the entry)
        if path:
            self.app.actions.rename(path, name)
