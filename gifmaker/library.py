"""The library: GifMaker's home screen. A folder's videos and pictures as a grid,
with filters (seen / unseen / unmarked, videos / pictures), search and sorting.

On the left: recent folders, the sort-key folders (click one, or press its key, to
move the selected file there) and the tools. On the right: the selected file playing
silently, with its actions. Double-click (or Enter) opens a file in the editor.
"""

import os
import threading
import tkinter as tk
from tkinter import filedialog, font, simpledialog, ttk

from . import ffmpeg_tools as ff
from . import files
from . import seen
from . import sort_keys
from . import theme
from . import ui_thread
from .file_browser import FileBrowser
from .player import Player
from .tooltip import Tooltip

SIDEBAR_W = 210
PANEL_W = 320  # the right panel's starting width; drag its left edge to change it
PANEL_MIN_W = 260
PREVIEW_H = 180
MAX_FPS = 30
RECENT = 6  # recent folders remembered
SORTS = ("Name", "Newest first", "Oldest first", "Largest first")


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def default_folder():
    home = os.path.expanduser("~")
    for name in ("Videos", "Pictures"):
        if os.path.isdir(os.path.join(home, name)):
            return os.path.join(home, name)
    return home


def _human_size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


class Preview(tk.Canvas):
    """The selected file, small: a picture, or a video playing silently on a loop."""

    def __init__(self, master, app):
        super().__init__(master, height=PREVIEW_H, bg="#111", highlightthickness=0)
        self.app = app
        self.path = None
        self.info = None
        self.token = 0
        self.photo = None
        self.player = Player(self, app.ffmpeg, self._on_frame, self._on_end)
        self.img = self.create_image(0, 0, anchor="center")
        self.text = self.create_text(0, 0, fill="#888", anchor="center")
        self.bind("<Configure>", lambda e: self._resized())
        self.resize_job = None

    def show(self, path, on_info=None):
        self.stop()
        self.path = path
        self.info = None
        self.photo = None
        self.itemconfig(self.img, image="")
        self.itemconfig(self.text, text="" if path else "Nothing selected")
        if not path:
            return
        token = self.token

        def work():
            try:
                info = ff.probe(self.app.ffmpeg, path)
            except Exception:  # noqa: BLE001 - can't show it: say so
                info = None
            ui_thread.call(lambda: self._probed(token, info, on_info))

        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        self.player.stop()
        self.token += 1

    def _probed(self, token, info, on_info):
        if token != self.token:
            return
        self.info = info
        if on_info:
            on_info(info)
        if info is None:
            self.itemconfig(self.text, text="Can't show this file")
            return
        self._start()

    def _size(self):
        return max(2, self.winfo_width()), max(2, self.winfo_height())

    def _start(self):
        info, path = self.info, self.path
        if not info or not path or not self.winfo_viewable():
            return
        w, h = ff.fit_size(info.width, info.height, *self._size(), upscale=True)
        if info.duration > 0 and not ff.is_image(path):
            self.player.muted = True
            self.player.start(path, 0.0, (w, h), min(info.fps or 25.0, MAX_FPS))
            return
        token = self.token

        def work():
            data = ff.grab_frame(self.app.ffmpeg, path, 0.0, w, h)
            ui_thread.call(lambda: token == self.token and data and self._on_frame(0, data))

        threading.Thread(target=work, daemon=True).start()

    def _on_frame(self, _t, data):
        try:
            self.photo = tk.PhotoImage(master=self, data=data, format="ppm")
        except tk.TclError:
            return
        self.itemconfig(self.img, image=self.photo)

    def _on_end(self):
        token = self.token
        self.after(10, lambda: token == self.token and self._start())  # loop

    def _resized(self):
        w, h = self._size()
        self.coords(self.img, w // 2, h // 2)
        self.coords(self.text, w // 2, h // 2)
        if self.resize_job:
            self.after_cancel(self.resize_job)
        self.resize_job = self.after(200, self._restart)

    def _restart(self):
        self.resize_job = None
        if self.info and self.path:
            self.player.stop()
            self.token += 1
            self._start()


class LibraryView(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.folder = None
        self.listed = []  # every file in the folder (natural order)
        self.paths = []  # what's shown: filtered, searched and sorted
        self.selected = None
        self.stats = {}  # path -> os.stat_result, for sorting by date or size
        self.mark_filter = tk.StringVar(value="all")
        self.kind_filter = tk.StringVar(value="all")
        self.sort = tk.StringVar(value=theme.load_setting("library_sort", SORTS[0]))
        self.search = tk.StringVar()
        self.search_job = None
        self.small = font.nametofont("TkDefaultFont").copy()
        self.small.configure(size=max(7, self.small.cget("size") - 1), weight="bold")
        self.bold = font.nametofont("TkDefaultFont").copy()
        self.bold.configure(weight="bold")
        self.big_bold = font.nametofont("TkDefaultFont").copy()
        self.big_bold.configure(weight="bold", size=self.big_bold.cget("size") + 2)
        self._build()
        theme.on_change(self._recolor)
        for var in (self.mark_filter, self.kind_filter):
            var.trace_add("write", lambda *_: self.refilter())
        self.sort.trace_add("write", lambda *_: self._sort_changed())
        self.search.trace_add("write", lambda *_: self._search_soon())

    # ------------------------------------------------------------ layout
    def _build(self):
        head = ttk.Frame(self, padding=(12, 8))
        head.pack(side="top", fill="x")
        ttk.Label(head, text="GifMaker", font=self.big_bold).pack(side="left")
        self.crumb = ttk.Label(head, style="Muted.TLabel")
        self.crumb.pack(side="left", padx=(16, 16))
        ttk.Label(head, text="Search").pack(side="left", padx=(0, 6))
        search = self.search_entry = ttk.Entry(head, textvariable=self.search, width=34)
        search.pack(side="left")
        Tooltip(search, "Find files in this folder by name")
        search.bind("<Escape>", lambda e: (self.search.set(""), self.browser.canvas.focus_set()))
        search.bind("<Return>", lambda e: self.browser.canvas.focus_set())
        settings = ttk.Button(head, text="Settings", command=self.app.open_settings)
        settings.pack(side="right")
        Tooltip(settings, "Dark mode, autoplay, slideshow, sort keys, recording, downloads "
                          "and updates")
        ttk.Separator(self, orient="horizontal").pack(side="top", fill="x")

        status = ttk.Frame(self, padding=(12, 4))
        status.pack(side="bottom", fill="x")
        ttk.Label(status, textvariable=self.app.status, style="Muted.TLabel",
                  width=1).pack(side="left", fill="x", expand=True)
        self.update_btn = ttk.Button(status, command=self.app.install_update)
        self.count_label = ttk.Label(status, style="Muted.TLabel")
        self.count_label.pack(side="right")
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x")

        body = ttk.Frame(self)
        body.pack(side="top", fill="both", expand=True)
        self.sidebar = tk.Frame(body, width=SIDEBAR_W, bd=0)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        ttk.Separator(body, orient="vertical").pack(side="left", fill="y")
        self.body = body
        self.panel_w = int(theme.load_setting("library_panel_w", PANEL_W) or PANEL_W)
        self.panel = ttk.Frame(body, width=self.panel_w, padding=12)
        self.panel.pack(side="right", fill="y")
        self.panel.pack_propagate(False)
        # A handle on the panel's left edge: drag it to make the panel (and preview) wider.
        handle = tk.Frame(body, width=7, cursor="sb_h_double_arrow", bd=0)
        handle.pack(side="right", fill="y")
        ttk.Separator(handle, orient="vertical").place(relx=0.5, y=0, relheight=1)
        handle.bind("<B1-Motion>", self._drag_panel)
        handle.bind("<ButtonRelease-1>",
                    lambda e: theme.save_setting("library_panel_w", self.panel_w))
        Tooltip(handle, "Drag to resize the preview")
        main = ttk.Frame(body, padding=(12, 10, 6, 0))
        main.pack(side="left", fill="both", expand=True)

        bar = ttk.Frame(main)
        bar.pack(fill="x", pady=(0, 8))
        self.mark_buttons = {}
        for value, text in (("all", "All"), ("unseen", "Unseen"), ("seen", "Seen"),
                            ("unmarked", "Unmarked")):
            b = ttk.Radiobutton(bar, text=text, value=value, variable=self.mark_filter,
                                style="Toolbutton")
            b.pack(side="left", padx=(0, 2))
            self.mark_buttons[value] = (b, text)
        ttk.Frame(bar, width=14).pack(side="left")
        for value, text in (("all", "Videos & pictures"), ("videos", "Videos"),
                            ("pictures", "Pictures")):
            ttk.Radiobutton(bar, text=text, value=value, variable=self.kind_filter,
                            style="Toolbutton").pack(side="left", padx=(0, 2))
        size = ttk.Scale(bar, from_=80, to=320, length=90,
                         command=lambda v: self.browser._size_changed())
        size.pack(side="right")
        Tooltip(size, "Thumbnail size (or Ctrl + mouse wheel over them)")
        ttk.Label(bar, text="Size").pack(side="right", padx=(12, 4))
        ttk.Combobox(bar, textvariable=self.sort, values=SORTS, state="readonly",
                     width=13).pack(side="right")
        ttk.Label(bar, text="Sort").pack(side="right", padx=(0, 4))

        self.browser = FileBrowser(main, self.app.ffmpeg, self.select, None,
                                   on_look=self.app._quick_look, on_activate=self.open_in_editor,
                                   head=False, empty_text="No videos or pictures here")
        self.browser.pack(fill="both", expand=True)
        size.configure(variable=self.browser.size)
        c = self.browser.canvas
        c.bind("<Delete>", lambda e: self._act(self.app.actions.delete))
        c.bind("<F2>", lambda e: self._act(self.rename))
        c.bind("<Control-e>", lambda e: self._act(self.app.actions.reveal))
        c.bind("<Control-z>", lambda e: self.undo_move() or "break")
        c.bind("<Key>", self._key)

        self._build_panel()
        self._fill_sidebar()

    def _drag_panel(self, event):
        body_w = self.body.winfo_width()
        right = self.body.winfo_rootx() + body_w
        widest = max(PANEL_MIN_W, body_w - SIDEBAR_W - 300)  # keep room for the files
        self.panel_w = int(min(max(right - event.x_root, PANEL_MIN_W), widest))
        self.panel.config(width=self.panel_w)
        wrap = self.panel_w - 30
        for label in (self.name_label, self.details, self.hint):
            label.config(wraplength=wrap)

    def _build_panel(self):
        # Everything under the preview is packed from the bottom up first, and the preview
        # last, so it takes whatever room is left: bigger panel, bigger preview, and the
        # buttons never get pushed out of sight.
        p = self.panel
        wrap = self.panel_w - 30
        self.hint = ttk.Label(p, style="Muted.TLabel", wraplength=wrap, justify="left",
                              text="Double-click to edit  ·  S / U to mark")
        self.hint.pack(side="bottom", anchor="w", fill="x")
        self.actions_box = tk.Frame(p, bd=0)
        self.actions_box.pack(side="bottom", fill="x")
        marks = ttk.Frame(p)
        marks.pack(side="bottom", fill="x", pady=(8, 8))
        self.open_btn = ttk.Button(p, text="Open in editor  (Enter)",
                                   command=lambda: self.open_in_editor(self.selected))
        self.open_btn.pack(side="bottom", fill="x", ipady=4)
        self.details = ttk.Label(p, style="Muted.TLabel", wraplength=wrap, justify="left")
        self.details.pack(side="bottom", anchor="w", fill="x", pady=(2, 10))
        self.name_label = ttk.Label(p, font=self.bold, wraplength=wrap, justify="left")
        self.name_label.pack(side="bottom", anchor="w", fill="x", pady=(10, 0))
        self.preview = Preview(p, self.app)
        self.preview.pack(side="top", fill="both", expand=True)
        self.preview.bind("<Double-Button-1>", lambda e: self.open_in_editor(self.selected))
        self.mark_btns = {}
        for state, label, key in ((seen.SEEN, "Seen", "S"), (seen.UNSEEN, "Unseen", "U")):
            b = tk.Button(marks, text=f"{label}  ({key})", relief="flat", bd=0, cursor="hand2",
                          command=lambda s=state: self._toggle_mark(s))
            b.pack(side="left", fill="x", expand=True, padx=(0, 4) if state == seen.SEEN else 0,
                   ipady=5)
            self.mark_btns[state] = b

    # ------------------------------------------------- sidebar and actions
    def _colors(self):
        c = theme.colors
        return (c.get("bg", "#f0f0f0"), c.get("fg", "#000"), c.get("muted", "#666"),
                c.get("select", "#0078d7"), c.get("select_fg", "#fff"),
                c.get("button_hover", "#dcdcdc" if not theme.dark else "#3a3a3a"),
                c.get("border", "#b0b0b0"))

    def _heading(self, parent, text):
        bg, _, muted, *_ = self._colors()
        tk.Label(parent, text=text.upper(), bg=bg, fg=muted, font=self.small,
                 anchor="w").pack(fill="x", padx=10, pady=(14, 4))

    def _row(self, parent, text, command, badge=None, selected=False, fg=None, tip=None):
        """A clickable line, like a menu item: text, and a key on the right if given."""
        bg, text_fg, muted, sel, sel_fg, hover, border = self._colors()
        base = hover if selected else bg
        row = tk.Frame(parent, bg=base, cursor="hand2", padx=8, pady=5)
        row.pack(fill="x", padx=4)
        widgets = [row]
        if badge:
            b = tk.Label(row, text=badge, bg=base, fg=text_fg, font=self.small, width=3,
                         relief="solid", bd=1, highlightthickness=0)
            b.pack(side="left", padx=(0, 8))
            widgets.append(b)
        label = tk.Label(row, text=text, bg=base, fg=fg or text_fg, anchor="w")
        label.pack(side="left", fill="x", expand=True)
        widgets.append(label)

        def paint(on):
            for w in widgets:
                w.config(bg=hover if on or selected else bg)

        for w in widgets:
            w.bind("<Enter>", lambda e: paint(True))
            w.bind("<Leave>", lambda e: paint(False))
            w.bind("<ButtonRelease-1>", lambda e: command())
        if tip:
            Tooltip(row, tip)
        return row

    def _fill_sidebar(self):
        s = self.sidebar
        bg = self._colors()[0]
        s.config(bg=bg)
        for child in s.winfo_children():
            child.destroy()
        self._heading(s, "Recent folders")
        current = _norm(self.folder) if self.folder else None
        for folder in self._recent():
            name = os.path.basename(os.path.normpath(folder)) or folder
            self._row(s, name, lambda f=folder: self.set_folder(f),
                      selected=_norm(folder) == current, tip=folder)
        self._row(s, "Open folder...", self.choose_folder, fg=self._colors()[2])
        self._heading(s, "Sort into")
        keys = sort_keys._ordered(self.app.sort_keys)
        for key, folder in keys:
            name = os.path.basename(os.path.normpath(folder)) or folder
            self._row(s, name, lambda f=folder: self._act(lambda p: self.app.actions.move_to(p, f)),
                      badge=sort_keys.label(key),
                      tip=f"Move the selected file to {folder}  (press {sort_keys.label(key)})")
        self._row(s, "+ Add sort key" if not keys else "Edit sort keys...",
                  self.app.open_sort_keys, fg=self._colors()[2])
        self._heading(s, "Tools")
        for text, command, tip in (
                ("Download link", self.app.open_link,
                 "Download a video from YouTube or another site and open it  (Ctrl+L)"),
                ("Record screen", self.app.open_recorder,
                 "Record the screen (or part of it), with the computer's sound"),
                ("Slideshow", self.slideshow, "Go through the folder's files by themselves"),
                ("Grid player", self.app.open_grid, "Play several videos at once"),
                ("Timeline edit", self.app.open_sequence,
                 "Join parts of several videos and pictures into one video"),
                ("Rename files", lambda: self.app.open_rename_tool(self.folder),
                 "Rename everything in this folder with a numbered pattern")):
            self._row(s, text, command, tip=tip)

    def _fill_actions(self):
        box = self.actions_box
        bg, _, _, _, _, _, border = self._colors()
        box.config(bg=bg, highlightthickness=1, highlightbackground=border)
        for child in box.winfo_children():
            child.destroy()
        for key, folder in sort_keys._ordered(self.app.sort_keys):
            name = os.path.basename(os.path.normpath(folder)) or folder
            self._row(box, f"Move to {name}",
                      lambda f=folder: self._act(lambda p: self.app.actions.move_to(p, f)),
                      badge=sort_keys.label(key))
        for text, command, fg in (
                ("Move to folder...", lambda p: self.app.actions.move_or_copy(p, False), None),
                ("Copy to folder...", lambda p: self.app.actions.move_or_copy(p, True), None),
                ("Rename...   (F2)", self.rename, None),
                ("Show in folder   (Ctrl+E)", self.app.actions.reveal, None),
                ("Copy path", self.app.actions.copy_path, None),
                ("Delete   (Del)", self.app.actions.delete, theme.colors.get("error"))):
            self._row(box, text, lambda c=command: self._act(c), fg=fg)

    def _paint_marks(self):
        state = seen.get(self.selected) if self.selected else None
        for s, b in self.mark_btns.items():
            on = state == s
            color = seen.COLORS[s]
            if on:
                b.config(bg=color, fg="#ffffff", activebackground=color, activeforeground="#ffffff")
            else:
                bg = theme.colors.get("button", "#e1e1e1")
                b.config(bg=bg, fg=color if theme.dark else seen.DARKER[s],
                         activebackground=theme.colors.get("button_hover", "#d0d0d0"),
                         activeforeground=theme.colors["fg"])
            b.config(state="normal" if self.selected else "disabled")

    def _recolor(self):
        self._fill_sidebar()
        self._fill_actions()
        self._paint_marks()

    def show_update(self, found):
        """A newer GifMaker is out: offer it in the status bar."""
        self.update_btn.config(text=f"Update to {found['version']}")
        self.update_btn.pack(side="right", padx=(10, 0), before=self.count_label)

    # ---------------------------------------------------------- folders
    def _recent(self):
        saved = theme.load_setting("recent_folders", [])
        return [f for f in saved if isinstance(f, str) and os.path.isdir(f)][:RECENT]

    def _remember(self, folder):
        recent = [f for f in self._recent() if _norm(f) != _norm(folder)]
        theme.save_setting("recent_folders", [folder] + recent[:RECENT - 1])
        theme.save_setting("library_folder", folder)

    def choose_folder(self):
        folder = filedialog.askdirectory(parent=self.app.root, initialdir=self.folder or None,
                                         title="Show this folder")
        if folder:
            self.set_folder(os.path.normpath(folder))

    def start_folder(self):
        saved = theme.load_setting("library_folder", "")
        return saved if isinstance(saved, str) and os.path.isdir(saved) else default_folder()

    def set_folder(self, folder, select=None):
        folder = os.path.normpath(folder)
        if not self.folder or _norm(folder) != _norm(self.folder):
            self.search.set("")
        self.folder = folder
        self._remember(folder)
        parent = os.path.basename(os.path.dirname(folder))
        self.crumb.config(text=(f"{parent}  ›  " if parent else "")
                          + (os.path.basename(folder) or folder))
        self._fill_sidebar()
        self.reload(select=select)

    def reload(self, select=None):
        """List the folder again (files were added, moved, renamed or deleted)."""
        if not self.folder:
            return
        before = self.selected
        self.app.listing = None
        self.listed = self.app._list_folder(self.folder) if os.path.isdir(self.folder) else []
        self.stats = {}
        self.refilter(select=select or before)

    # ---------------------------------------------------------- filters
    def _search_soon(self):
        if self.search_job:
            self.after_cancel(self.search_job)
        self.search_job = self.after(200, self.refilter)

    def _sort_changed(self):
        theme.save_setting("library_sort", self.sort.get())
        self.refilter()

    def refilter(self, select=None):
        """Show the folder's files that pass the filters, in the chosen order."""
        self.search_job = None
        select = select or self.selected
        old_index = self.paths.index(self.selected) if self.selected in self.paths else 0
        kind = self.kind_filter.get()
        text = self.search.get().strip().lower()
        counts = {"all": 0, "seen": 0, "unseen": 0, "unmarked": 0}
        shown = []
        want = self.mark_filter.get()
        for path in self.listed:
            is_pic = ff.is_image(path)
            if kind == "videos" and is_pic or kind == "pictures" and not is_pic:
                continue
            if text and text not in os.path.basename(path).lower():
                continue
            state = seen.get(path, look_further=False) or "unmarked"
            counts["all"] += 1
            counts[state] += 1
            if want in ("all", state):
                shown.append(path)
        order = self.sort.get()
        if order != SORTS[0]:
            for path in shown:
                if path not in self.stats:
                    try:
                        self.stats[path] = os.stat(path)
                    except OSError:
                        self.stats[path] = None

            def key(p):
                st = self.stats.get(p)
                if order == "Largest first":
                    return -(st.st_size if st else 0)
                t = st.st_mtime if st else 0
                return -t if order == "Newest first" else t

            shown.sort(key=key)
        self.paths = shown
        for value, (button, label) in self.mark_buttons.items():
            button.config(text=f"{label} {counts[value]}")
        total = len(self.listed)
        self.count_label.config(text=f"{len(shown)} of {total} files" if len(shown) != total
                                else f"{total} files")
        if select in shown:
            pick = select
        elif shown:  # the one that took its place
            pick = shown[min(old_index, len(shown) - 1)]
        else:
            pick = None
        self.select(pick)

    # --------------------------------------------------------- selection
    def select(self, path):
        self.browser.show(self.paths, path)
        if path == self.selected and self.preview.path == path:
            self._paint_marks()
            return
        self.selected = path
        self.name_label.config(text=os.path.basename(path) if path else "No file selected")
        self.details.config(text="")
        self.open_btn.config(state="normal" if path else "disabled")
        self._fill_actions()
        self._paint_marks()
        if self.app.view == "library":
            self.preview.show(path, self._show_details)

    def _show_details(self, info):
        path = self.selected
        parts = []
        if info:
            if info.width:
                parts.append(f"{info.width} × {info.height}")
            if info.duration and not ff.is_image(path):
                parts.append(ff.format_time(info.duration).split(".")[0])
        try:
            parts.append(_human_size(os.stat(path).st_size))
        except (OSError, TypeError):
            pass
        self.details.config(text="  ·  ".join(parts))

    def _act(self, action):
        """Do action(selected path), if a file is selected."""
        if self.selected:
            action(self.selected)
        return "break"

    def _toggle_mark(self, state):
        path = self.selected
        if path:
            self.app.actions.mark(path, None if seen.get(path) == state else state)

    def _key(self, event):
        """S / U mark the selected file; a sort key moves it to its folder."""
        if event.state & 0x4:  # Ctrl+something: not ours
            return None
        sym = event.keysym.lower()
        if sym in ("s", "u") and not event.state & 0x1:
            self._toggle_mark(seen.SEEN if sym == "s" else seen.UNSEEN)
            return "break"
        key = sort_keys.key_name(event)
        if key in self.app.sort_keys:
            return self._act(lambda p: self.app.actions.move_to(p, self.app.sort_keys[key]))
        return None

    def rename(self, path):
        stem = os.path.splitext(os.path.basename(path))[0]
        name = simpledialog.askstring("Rename", "New name:", initialvalue=stem,
                                      parent=self.app.root)
        if name:
            self.app.actions.rename(path, name, on_done=lambda new: self.reload(select=new))

    def undo_move(self):
        """Ctrl+Z: put the last moved file back where it was."""
        if not self.app.last_sort:
            return
        src, dst = self.app.last_sort
        self.app.last_sort = None
        if not os.path.exists(dst):
            self.app.status.set(f"{os.path.basename(dst)} isn't in {os.path.dirname(dst)} any more.")
            return
        back = src if not os.path.lexists(src) else files.free_name(
            os.path.dirname(src), os.path.basename(src))
        self.app.let_go(dst)

        def work():
            try:
                files.move_file(dst, back)
                error = None
            except OSError as e:
                error = e
            ui_thread.call(lambda: self._undone(back, error))

        threading.Thread(target=work, daemon=True).start()

    def _undone(self, back, error):
        if error:
            self.app.status.set(f"Couldn't put it back: {error}")
            return
        self.app.files_changed()
        if self.folder and _norm(os.path.dirname(back)) == _norm(self.folder):
            self.reload(select=back)
        self.app.status.set(f"Put {os.path.basename(back)} back.")

    # ------------------------------------------------------------ hooks
    def open_in_editor(self, path):
        if path:
            self.app.open_video(path)

    def slideshow(self):
        path = self.selected or (self.paths[0] if self.paths else None)
        if not path:
            self.app.status.set("Pick a folder with videos or pictures first.")
            return
        self.app.open_video(path)
        if self.app.video_path and not self.app.slideshow:
            self.app.toggle_slideshow()

    def mark_changed(self, path):
        """path's seen / unseen mark changed."""
        self.browser.redraw(path)
        self.refilter(select=self.selected)  # (it may not belong in this list any more)
        self._paint_marks()

    def release(self, path):
        """Stop reading path (it's about to be moved, renamed or deleted)."""
        if self.preview.path and _norm(self.preview.path) == _norm(path):
            self.preview.stop()
            self.preview.path = None

    def shown(self, folder=None, select=None):
        """The library has just been put on screen."""
        folder = folder or self.folder or self.start_folder()
        if self.folder and _norm(folder) == _norm(self.folder):
            self.reload(select=select)
        else:
            self.set_folder(folder, select=select)
        self.preview.path = None  # start it again
        self.select(self.selected)
        self.browser.canvas.focus_set()

    def hidden(self):
        self.preview.stop()
