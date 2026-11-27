"""The Settings window: every preference in one place, in pages picked on the left.

General (look and behaviour), Slideshow, Sort keys, Recording (and the screenshot
key), Downloads and Updates. Changes take effect and are saved straight away.
"""

import os
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from . import downloader
from . import hotkey
from . import screen_recorder
from . import sort_keys
from . import theme
from . import ui_thread
from . import updater
from .tooltip import Tooltip
from .version import __version__

PAGES = ("General", "Slideshow", "Sort keys", "Recording", "Downloads", "Updates")
NO_LOGIN = "No (public videos)"  # (the same words as the Download link window)
WRAP = 440


def _setting_var(kind, name, default):
    """A Tk variable holding a saved setting, saved again whenever it changes."""
    var = kind(value=theme.load_setting(name, default))
    var.trace_add("write", lambda *_: _save(name, var))
    return var


def _save(name, var):
    try:
        value = var.get()
    except (tk.TclError, ValueError):
        return
    if name == "record_fps":
        value = int(value)
    theme.save_setting(name, value)


class SettingsWindow(tk.Toplevel):
    def __init__(self, app, page="General"):
        super().__init__(app.root)
        self.app = app
        self.title("Settings")
        self.transient(app.root)
        theme.style_window(self)
        self.page = tk.StringVar(value=page if page in PAGES else PAGES[0])
        self.pages = {}
        self.listening = False
        self._build()
        self.show(self.page.get())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", self._escape)
        self.bind("<Key>", self._key)
        self.geometry("680x470")
        self.minsize(600, 420)
        self.focus_set()

    def _build(self):
        nav = ttk.Frame(self, padding=(10, 12, 0, 12), width=150)
        nav.pack(side="left", fill="y")
        for name in PAGES:
            ttk.Radiobutton(nav, text=name, value=name, variable=self.page, style="Toolbutton",
                            command=lambda n=name: self.show(n)).pack(fill="x", pady=1)
        ttk.Separator(self, orient="vertical").pack(side="left", fill="y", padx=(10, 0))
        right = ttk.Frame(self, padding=(16, 12, 16, 12))
        right.pack(side="left", fill="both", expand=True)
        self.heading = ttk.Label(right, style="Head.TLabel")
        self.heading.pack(anchor="w", pady=(0, 10))
        self.body = ttk.Frame(right)
        self.body.pack(fill="both", expand=True)
        bottom = ttk.Frame(right)
        bottom.pack(fill="x", pady=(10, 0))
        self.status = ttk.Label(bottom, style="Muted.TLabel", wraplength=WRAP, justify="left")
        self.status.pack(side="left", fill="x", expand=True)
        ttk.Button(bottom, text="Close", command=self.close).pack(side="right")

    def show(self, name):
        """Switch to page name (building it the first time)."""
        self.page.set(name)
        for page in self.pages.values():
            page.pack_forget()
        if name not in self.pages:
            page = ttk.Frame(self.body)
            getattr(self, "_page_" + name.lower().replace(" ", "_"))(page)
            self.pages[name] = page
        self.pages[name].pack(fill="both", expand=True)
        self.heading.config(text=name)
        self.status.config(text="")
        if name == "Updates":
            self._refresh_updates()
        self.lift()

    def close(self):
        if self.listening:
            self.app.apply_screenshot_key(self.app.screenshot_key)
        self.destroy()
        self.app.settings_window = None

    def _key(self, event):
        if self.listening:
            return self._hotkey_pressed(event)
        panel = getattr(self, "sort_panel", None)
        if panel is not None and self.page.get() == "Sort keys":
            return panel.on_key(event)
        return None

    def _escape(self, event):
        """Esc cancels waiting for a key, else closes the window."""
        panel = getattr(self, "sort_panel", None)
        if self.listening or (panel is not None and panel.waiting):
            return self._key(event)
        self.close()
        return "break"

    # ------------------------------------------------------------- pages
    def _check(self, parent, text, var, tip=None, command=None):
        box = ttk.Checkbutton(parent, text=text, variable=var, command=command)
        box.pack(anchor="w", pady=3)
        if tip:
            Tooltip(box, tip)
        return box

    def _note(self, parent, text):
        ttk.Label(parent, text=text, style="Muted.TLabel", wraplength=WRAP,
                  justify="left").pack(anchor="w", pady=(0, 8))

    def _folder_row(self, parent, label, var, title):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(8, 3))
        ttk.Label(row, text=label, width=14).pack(side="left")
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)

        def browse():
            folder = filedialog.askdirectory(parent=self, title=title,
                                             initialdir=var.get() or None)
            if folder:
                var.set(os.path.normpath(folder))

        ttk.Button(row, text="Browse...", command=browse).pack(side="left", padx=(6, 0))

    def _choice_row(self, parent, label, var, values, width=10, tip=None):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, width=14).pack(side="left")
        box = ttk.Combobox(row, textvariable=var, values=values, state="readonly", width=width)
        box.pack(side="left")
        if tip:
            Tooltip(box, tip)
        return box

    def _page_general(self, p):
        app = self.app
        self._check(p, "Dark mode", app.dark_mode, command=app._toggle_dark)
        self._check(p, "One window", app.one_window,
                    "Videos you open from Explorer play in this window, instead of opening "
                    "another GifMaker")
        ttk.Separator(p).pack(fill="x", pady=8)
        self._check(p, "Start playing videos as soon as they open (autoplay)", app.autoplay)
        self._check(p, "Start videos over when they reach the end (loop)", app.loop_video)
        self._check(p, "Show the file in its folder after saving", app.show_folder)

    def _page_slideshow(self, p):
        from .app import SLIDE_SECONDS  # (here: app imports this module)

        app = self.app
        self._note(p, "Start a slideshow from Tools in the library, or with F5 in the editor. "
                      "Esc stops it.")
        row = ttk.Frame(p)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text="Next file every", width=14).pack(side="left")
        for seconds in SLIDE_SECONDS:
            text = f"{seconds} s" if seconds < 60 else f"{seconds // 60} min"
            ttk.Radiobutton(row, text=text, value=seconds, variable=app.slide_seconds,
                            style="Toolbutton").pack(side="left", padx=(0, 2))
        ttk.Separator(p).pack(fill="x", pady=8)
        self._check(p, "Play videos to the end before moving on", app.slide_videos)
        self._check(p, "Shuffle", app.slide_shuffle)
        self._check(p, "Start over at the end", app.slide_loop)

    def _page_sort_keys(self, p):
        self.sort_panel = sort_keys.SortKeysPanel(p, self.app, self.app._sort_keys_changed)
        self.sort_panel.pack(fill="both", expand=True)

    def _page_recording(self, p):
        self.rec_fps = _setting_var(tk.StringVar, "record_fps", 30)
        self.rec_sound = _setting_var(tk.BooleanVar, "record_sound", True)
        self.rec_mouse = _setting_var(tk.BooleanVar, "record_mouse", True)
        self.rec_hide = _setting_var(tk.BooleanVar, "record_hide", True)
        self.rec_folder = _setting_var(tk.StringVar, "record_folder",
                                       screen_recorder.default_folder())
        self._choice_row(p, "Frame rate", self.rec_fps,
                         [str(f) for f in screen_recorder.FPS_CHOICES], width=6,
                         tip="Frames per second. 30 is smooth; 60 for games and fast motion "
                             "(bigger files).")
        sound = self._check(p, "Record the computer's sound", self.rec_sound,
                            "What plays through your speakers or headphones (not the "
                            "microphone)")
        no_sound = screen_recorder.sound_available()
        if no_sound:
            sound.config(state="disabled")
            Tooltip(sound, no_sound)
        self._check(p, "Show the mouse pointer", self.rec_mouse)
        self._check(p, "Hide GifMaker while recording", self.rec_hide)
        self._folder_row(p, "Save in", self.rec_folder, "Save recordings and screenshots in")
        ttk.Separator(p).pack(fill="x", pady=10)
        row = ttk.Frame(p)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text="Screenshot key", width=14).pack(side="left")
        self.key_btn = ttk.Button(row, width=22, command=self._listen_for_key)
        self.key_btn.pack(side="left")
        ttk.Button(row, text="Clear", command=lambda: self._set_hotkey(None)).pack(
            side="left", padx=(6, 0))
        Tooltip(self.key_btn, "Click, then press the keys you want (e.g. Ctrl+Shift+S or F9). "
                              "It takes a screenshot of the screen the mouse is on, saved in "
                              "the folder above, from anywhere, even when GifMaker is "
                              "minimized.")
        self._show_hotkey()

    def _page_downloads(self, p):
        quality = _setting_var(tk.StringVar, "download_quality", "1080p")
        if quality.get() not in downloader.QUALITIES:
            quality.set("1080p")
        login = _setting_var(tk.StringVar, "download_login", NO_LOGIN)
        if login.get() not in downloader.BROWSERS:
            login.set(NO_LOGIN)
        folder = _setting_var(tk.StringVar, "download_folder", downloader.default_folder())
        self.dl_vars = (quality, login, folder)
        self._note(p, "Used by Download link (in the library's Tools, or Ctrl+L). The quality "
                      "can also be changed for each download.")
        self._choice_row(p, "Quality", quality, list(downloader.QUALITIES),
                         tip="The highest quality to get (lower downloads faster).")
        self._choice_row(p, "Logged in", login, [NO_LOGIN, *downloader.BROWSERS], width=18,
                         tip="For videos you can only watch logged in: pick the browser "
                             "you're logged in to the site with. Firefox works best; Chrome "
                             "and Edge lock their logins away from other programs.")
        self._folder_row(p, "Save in", folder, "Save downloads in")

    def _page_updates(self, p):
        ttk.Label(p, text=f"This is GifMaker {__version__}.").pack(anchor="w", pady=(0, 6))
        self.update_text = ttk.Label(p, wraplength=WRAP, justify="left")
        self.update_text.pack(anchor="w", pady=(0, 10))
        row = ttk.Frame(p)
        row.pack(anchor="w")
        self.install_btn = ttk.Button(row, text="Install update", command=self._install)
        self.check_btn = ttk.Button(row, text="Check now", command=self._check_now)
        self.check_btn.pack(side="left")

    # ----------------------------------------------------------- updates
    def _refresh_updates(self):
        found = self.app.new_release
        if not updater.can_update():
            text = "Updates are installed from the released app (not when running from source)."
        elif found:
            how = "click Install update (GifMaker restarts)" if updater.is_installed() \
                else "Install update opens its download page"
            text = f"Version {found['version']} is out: {how}."
        else:
            text = "You have the latest version."
        self.update_text.config(text=text)
        if found:
            self.install_btn.pack(side="left", padx=(0, 6), before=self.check_btn)
        else:
            self.install_btn.pack_forget()
        self.check_btn.config(state="normal" if updater.can_update() else "disabled")

    def _install(self):
        if self.app.new_release:
            self.app.install_update()

    def _check_now(self):
        self.check_btn.config(state="disabled")
        self.update_text.config(text="Checking...")

        def work():
            try:
                found = updater.check()
                error = None
            except (OSError, ValueError, KeyError) as e:
                found, error = None, e
            ui_thread.call(lambda: self._checked(found, error))

        threading.Thread(target=work, daemon=True).start()

    def _checked(self, found, error):
        if not self.winfo_exists():
            return
        if found:
            self.app._update_found(found)
        self._refresh_updates()
        if error:
            self.update_text.config(text=f"Couldn't check: {error}")

    # ---------------------------------------------------- screenshot key
    def _show_hotkey(self):
        self.key_btn.config(text="Press keys..." if self.listening
                            else hotkey.describe(self.app.screenshot_key))

    def _listen_for_key(self):
        if not hotkey.supported():
            self.status.config(text="A screenshot key that works anywhere needs Windows.")
            return
        self.listening = True
        self.app.screenshot_hotkey.clear()  # so pressing the current one is just a key press
        self._show_hotkey()
        self.status.config(text="Press the keys you want (Esc cancels).")
        self.focus_force()

    def _hotkey_pressed(self, event):
        if event.keysym in ("Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
                            "Win_L", "Win_R", "Meta_L", "Meta_R"):
            return "break"
        self.listening = False
        if event.keysym == "Escape":
            self.status.config(text="")
            self.app.apply_screenshot_key(self.app.screenshot_key)
        else:
            key = hotkey.from_event(event)
            if key is None:
                self.status.config(text="Use a function key, Print Screen, or a combination "
                                        "such as Ctrl+Shift+S.")
                self.app.apply_screenshot_key(self.app.screenshot_key)
            else:
                self._set_hotkey(key)
        self._show_hotkey()
        return "break"

    def _set_hotkey(self, key):
        error = self.app.apply_screenshot_key(key)
        self.status.config(text=error or ("Screenshot key removed." if key is None else
                                          f"Press {hotkey.describe(key)} anywhere to take a "
                                          "screenshot."))
        self._show_hotkey()
