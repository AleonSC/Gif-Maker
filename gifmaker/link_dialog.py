"""The "Open link" window: download a video from a site and open it."""

import os
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from . import downloader
from . import theme
from . import ui_thread
from .tooltip import Tooltip


NO_LOGIN = "No (public videos)"


class LinkDialog(tk.Toplevel):
    """Downloads url (or what's pasted) with yt-dlp, then calls on_done(path)."""

    def __init__(self, app, url="", on_done=None):
        super().__init__(app.root)
        self.app, self.on_done = app, on_done
        self.title("Open link")
        self.transient(app.root)
        self.resizable(True, False)
        theme.style_window(self)
        self.busy = False
        self.cancelled = False
        self.url = tk.StringVar(value=url or self._clipboard_link())
        self.quality = tk.StringVar(value=theme.load_setting("download_quality", "1080p"))
        if self.quality.get() not in downloader.QUALITIES:
            self.quality.set("1080p")
        self.login = tk.StringVar(value=theme.load_setting("download_login", NO_LOGIN))
        if self.login.get() not in downloader.BROWSERS:
            self.login.set(NO_LOGIN)
        self.folder = tk.StringVar(value=theme.load_setting("download_folder", "")
                                   or downloader.default_folder())
        self.status = tk.StringVar()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        self.bind("<Return>", lambda e: self.start())
        self.entry.focus_set()
        self.entry.select_range(0, "end")
        if url:  # handed a link (dropped or pasted on the main window): go straight away
            self.after(50, self.start)

    def _clipboard_link(self):
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return ""
        return text if downloader.looks_like_link(text) else ""

    def _build(self):
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)
        ttk.Label(main, text="Link").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=4)
        self.entry = ttk.Entry(main, textvariable=self.url, width=64)
        self.entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)
        Tooltip(self.entry, "The page of the video (YouTube and most video sites work)")

        ttk.Label(main, text="Quality").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        quality = ttk.Combobox(main, textvariable=self.quality, state="readonly", width=8,
                               values=list(downloader.QUALITIES))
        quality.grid(row=1, column=1, sticky="w", pady=4)
        Tooltip(quality, "The highest quality to get (lower downloads faster). "
                         "Saved as MP4 where the site allows.")

        ttk.Label(main, text="Logged in").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=4)
        login = ttk.Combobox(main, textvariable=self.login, state="readonly", width=18,
                             values=[NO_LOGIN, *downloader.BROWSERS])
        login.grid(row=2, column=1, sticky="w", pady=4)
        Tooltip(login, "For videos you can only watch logged in (e.g. as a subscriber): pick "
                       "the browser you're logged in to the site with, and the download "
                       "uses that login. Firefox works best; Chrome and Edge lock their "
                       "logins away from other programs.")

        ttk.Label(main, text="Save in").grid(row=3, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Entry(main, textvariable=self.folder).grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Button(main, text="Browse...", command=self._browse).grid(
            row=3, column=2, sticky="w", padx=(6, 0), pady=4)

        self.progress = ttk.Progressbar(main, maximum=1.0)
        self.progress.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 2))
        ttk.Label(main, textvariable=self.status, style="Muted.TLabel", width=1).grid(
            row=5, column=0, columnspan=3, sticky="ew")

        buttons = ttk.Frame(main)
        buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(10, 0))
        self.go_btn = ttk.Button(buttons, text="Download and open", command=self.start)
        self.go_btn.pack(side="left")
        ttk.Button(buttons, text="Cancel", command=self.close).pack(side="left", padx=(6, 0))

    def _browse(self):
        folder = filedialog.askdirectory(parent=self, title="Save downloads in",
                                         initialdir=self.folder.get() or None)
        if folder:
            self.folder.set(os.path.normpath(folder))

    def _set_status(self, text, frac=None):
        if self.winfo_exists():
            self.status.set(text)
            if frac is None:
                self.progress.config(mode="indeterminate")
                self.progress.start(15)
            else:
                self.progress.stop()
                self.progress.config(mode="determinate", value=frac)

    def start(self):
        if self.busy:
            return
        url, folder = self.url.get().strip(), self.folder.get().strip()
        if not downloader.looks_like_link(url):
            self.status.set("Paste the video's link (starting with http).")
            return
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            self.status.set(f"Can't use that folder: {e}")
            return
        theme.save_setting("download_quality", self.quality.get())
        theme.save_setting("download_folder", folder)
        theme.save_setting("download_login", self.login.get())
        self._download(url, folder, item=None)

    def _download(self, url, folder, item):
        """Download url (only video number item, if the page has several; None means
        list them first and ask if there are several)."""
        self.busy = True
        self.go_btn.config(state="disabled")
        self._set_status("Starting...", None)
        quality, ffmpeg = self.quality.get(), self.app.ffmpeg
        login = self.login.get() if self.login.get() in downloader.BROWSERS else None

        def later(fn):
            try:
                ui_thread.call(fn)
            except (RuntimeError, tk.TclError):
                pass

        def status(text, frac=None):
            later(lambda: self._set_status(text, frac))

        def work():
            try:
                cmd = downloader.find_ytdlp() or downloader.fetch_ytdlp(status)
                downloader.update_if_due(cmd, status)
                status("Finding the video...", None)
                chosen = item
                if chosen is None:
                    videos = downloader.list_videos(cmd, url, ffmpeg, login)
                    if self.cancelled:
                        return
                    if videos:  # several on the page: ask which
                        later(lambda: self._choose(url, folder, videos))
                        return
                path = downloader.run(
                    downloader.build_command(cmd, url, folder, quality, ffmpeg, login, chosen),
                    lambda frac, text: status(text, frac), lambda: self.cancelled)
                later(lambda: self._finished(path, None))
            except downloader.DownloadError as e:
                later(lambda msg=str(e): self._finished(None, msg))
            except (OSError, subprocess.SubprocessError) as e:
                later(lambda msg=f"Couldn't run yt-dlp: {e}": self._finished(None, msg))

        threading.Thread(target=work, daemon=True).start()

    def _choose(self, url, folder, videos):
        """The page has several videos: list them and download the one picked."""
        self.busy = False
        if self.cancelled or not self.winfo_exists():
            return
        self.go_btn.config(state="normal")
        self._set_status(f"This page has {len(videos)} videos. Pick one.", 0)
        ChooseVideo(self, videos, lambda n: self._download(url, folder, n))

    def _finished(self, path, error):
        self.busy = False
        if self.cancelled or not self.winfo_exists():
            return
        self.go_btn.config(state="normal")
        if error:
            self._set_status(error, 0)
            return
        self.close()
        if self.on_done:
            self.on_done(path)

    def close(self):
        self.cancelled = self.busy  # stops a download that's under way
        self.destroy()


def _length(seconds):
    if not seconds:
        return "?"
    s = int(round(seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class ChooseVideo(tk.Toplevel):
    """Pick one of the videos on a page. on_pick(number) gets the one chosen."""

    def __init__(self, parent, videos, on_pick):
        super().__init__(parent)
        self.on_pick = on_pick
        self.title("Which video?")
        self.transient(parent)
        theme.style_window(self)
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="This page has several videos. The longest is usually the main "
                             "one; the short ones are often previews of other videos.",
                  style="Muted.TLabel", wraplength=520, justify="left").pack(anchor="w")
        table = ttk.Frame(main)
        table.pack(fill="both", expand=True, pady=8)
        self.tree = ttk.Treeview(table, columns=("length", "size", "name"), show="headings",
                                 height=min(12, len(videos)), selectmode="browse")
        for col, text, width, anchor in (("length", "Length", 70, "e"), ("size", "Size", 60, "e"),
                                         ("name", "Video", 400, "w")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=col == "name")
        bar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="left", fill="y")
        for number, name, seconds, height in videos:
            self.tree.insert("", "end", iid=str(number), values=(
                _length(seconds), f"{height}p" if height else "", name))
        longest = max(videos, key=lambda v: v[2] or 0)
        self.tree.selection_set(str(longest[0]))
        self.tree.see(str(longest[0]))
        self.tree.bind("<Double-1>", lambda e: self._pick())
        buttons = ttk.Frame(main)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Download this one", command=self._pick).pack(
            side="right", padx=(0, 6))
        self.bind("<Return>", lambda e: self._pick())
        self.bind("<Escape>", lambda e: self.destroy())
        self.tree.focus_set()
        self.grab_set()

    def _pick(self):
        sel = self.tree.selection()
        if sel:
            self.destroy()
            self.on_pick(int(sel[0]))
