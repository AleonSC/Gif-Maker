"""The "Open link" window: download a video from a site and open it."""

import os
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from . import downloader
from . import theme
from .tooltip import Tooltip


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

        ttk.Label(main, text="Save in").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Entry(main, textvariable=self.folder).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Button(main, text="Browse...", command=self._browse).grid(
            row=2, column=2, sticky="w", padx=(6, 0), pady=4)

        self.progress = ttk.Progressbar(main, maximum=1.0)
        self.progress.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 2))
        ttk.Label(main, textvariable=self.status, style="Muted.TLabel", width=1).grid(
            row=4, column=0, columnspan=3, sticky="ew")

        buttons = ttk.Frame(main)
        buttons.grid(row=5, column=0, columnspan=3, sticky="e", pady=(10, 0))
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
        self.busy = True
        self.go_btn.config(state="disabled")
        self._set_status("Starting...", None)
        quality, ffmpeg = self.quality.get(), self.app.ffmpeg

        def later(fn):
            try:
                self.after(0, fn)
            except (RuntimeError, tk.TclError):
                pass

        def status(text, frac=None):
            later(lambda: self._set_status(text, frac))

        def work():
            try:
                cmd = downloader.find_ytdlp() or downloader.fetch_ytdlp(status)
                downloader.update_if_due(cmd, status)
                status("Finding the video...", None)
                path = downloader.run(
                    downloader.build_command(cmd, url, folder, quality, ffmpeg),
                    lambda frac, text: status(text, frac), lambda: self.cancelled)
                later(lambda: self._finished(path, None))
            except downloader.DownloadError as e:
                later(lambda msg=str(e): self._finished(None, msg))
            except OSError as e:
                later(lambda msg=f"Couldn't run yt-dlp: {e}": self._finished(None, msg))

        threading.Thread(target=work, daemon=True).start()

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
