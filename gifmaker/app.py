"""GifMaker: pick a section of a video and turn it into a GIF."""

import bisect
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from operator import itemgetter
from tkinter import filedialog, font, messagebox, ttk

from . import ffmpeg_tools as ff
from . import downloader
from . import files
from . import single_instance
from . import theme
from . import updater
from .export_dialog import ANIMATION_DEFAULTS, ExportDialog
from .file_browser import FileBrowser
from .grid_player import GridPlayer
from .link_dialog import LinkDialog
from .rename_tool import RenameTool
from .player import Player
from .timeline import Timeline
from .tooltip import Tooltip
from .version import __version__

try:  # Drag and drop from Explorer needs the tkdnd extension; run without it if missing.
    from tkinterdnd2 import COPY, DND_FILES, DND_TEXT, TkinterDnD
except ImportError:
    TkinterDnD = None

PREVIEW_W, PREVIEW_H = 720, 405  # starting size of the video area
MIN_VIEW_H = 180  # the window can be shrunk until the video area is this tall
VIEW_RESIZE_MS = 150  # re-render at the new size once resizing pauses this long
MAX_GIF_SECONDS = 30
MAX_PLAYBACK_FPS = 30
PLAY_SPEEDS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0)
MAX_BOOST = 400  # percent
MAX_THUMBNAILS = 300  # cached keyframes for instant scrubbing (~65 MB at most)
SCRUB_SETTLE_MS = 120  # fetch the exact frame once the slider rests this long
VIDEO_TYPES = [
    ("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.wmv *.flv *.m4v *.mpg *.mpeg *.ts *.gif"),
    ("All files", "*.*"),
]
NO_TRANSFORM = {"rotate": 0, "flip": False, "crop": (0.0, 0.0, 1.0, 1.0)}
VIDEO_EXTENSIONS = {pattern[1:] for pattern in VIDEO_TYPES[0][1].split()}
# Keyboard shortcuts are ignored while one of these has focus.
_NO_SHORTCUT_WIDGETS = (ttk.Entry, tk.Entry, ttk.Combobox, ttk.Spinbox, ttk.Button, ttk.Checkbutton)


class GifMakerApp:
    def __init__(self, root, ffmpeg):
        self.root = root
        self.ffmpeg = ffmpeg
        root.title("GifMaker" if __version__ == "dev" else f"GifMaker {__version__}")
        try:
            self.icon = tk.PhotoImage(file=asset("gifmaker.png"))
            root.iconphoto(True, self.icon)
        except tk.TclError:
            pass
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.video_path = None
        self.duration = 0.0
        self.video_fps = 0.0
        self.video_width = 0
        self.video_height = 0
        self.has_audio = False
        self.transform = dict(NO_TRANSFORM)  # rotate/flip/crop last used for saving
        self.sections = []  # (start, end) parts to keep when saving a video
        self.section_sel = None  # index of the section the markers are editing
        self._loading_section = False
        self.video_token = 0  # bumped per opened video so stale work is ignored
        self.preview_image = None
        self.shown_t = None  # video time of the frame currently in the preview
        self.thumbs = []  # (seconds, rgb24 bytes), filled in by a background thread
        self.thumbs_done = False
        self.thumb_size = (0, 0)
        self.frame_busy = False
        self.frame_want = None
        self.frame_queue = queue.Queue()
        self.settle_job = None
        self.busy = False
        self.cancel_requested = False
        self.fullscreen = False
        self.grid_window = None
        self.rename_window = None
        self.view_w, self.view_h = PREVIEW_W, PREVIEW_H  # size of the video area
        self.resize_job = None

        self.player = Player(root, ffmpeg, self._on_play_frame, self._on_play_end)
        self.play_clip = False  # looping the start..end range rather than playing on
        self.play_image = None

        self.position = tk.DoubleVar(value=0.0)
        self.start_text = tk.StringVar(value="0:00.000")
        self.end_text = tk.StringVar(value="0:00.000")
        self.formats = ff.available_formats(ffmpeg)
        # Format, direction, fps, size... of animations: set in the Make GIF window.
        saved = theme.load_setting("animation", {})
        self.anim_options = dict(ANIMATION_DEFAULTS)
        if isinstance(saved, dict):
            self.anim_options.update((k, v) for k, v in saved.items() if k in ANIMATION_DEFAULTS)
        self.show_folder = self._setting_var("show_folder", False)
        self.autoplay = self._setting_var("autoplay", False)
        self.one_window = self._setting_var("one_window", True)
        self.loop_video = self._setting_var("loop_video", False)
        self.dark_mode = tk.BooleanVar(value=theme.dark)
        self.sound = tk.BooleanVar(value=True)
        self.play_speed = tk.StringVar(value="1\u00D7")
        self.volume = tk.DoubleVar(value=100)
        self.boost = tk.DoubleVar(value=100)  # percent; not saved, so it never starts loud
        self.status = tk.StringVar()  # results and problems; empty otherwise

        self._build_ui()
        self._sections_changed()
        self._enable_drop()
        self._poll_frame_queue()
        if theme.load_setting("files_shown", False):
            root.after(50, lambda: self.toggle_files(True))
        try:  # take videos opened from Explorer while this window is open
            self.listener = single_instance.Listener(
                lambda path: self.root.after(0, lambda: self._handed_over(path)))
        except OSError:
            self.listener = None
        self.new_release = None  # a newer release, once found
        if updater.can_update():
            threading.Thread(target=self._check_for_update, daemon=True).start()

    def _setting_var(self, name, default):
        """A BooleanVar that's remembered between runs."""
        var = tk.BooleanVar(value=bool(theme.load_setting(name, default)))
        var.trace_add("write", lambda *_: theme.save_setting(name, var.get()))
        return var

    def _toggle_dark(self):
        theme.apply(self.root, self.dark_mode.get())
        theme.save_dark_setting(self.dark_mode.get())

    def open_grid(self):
        """Open the grid player (starting with the current video), or bring it to the front."""
        if self.grid_window is not None:
            self.grid_window.lift()
            return
        self.stop_playback()
        self.grid_window = GridPlayer(self, VIDEO_TYPES)
        if self.video_path:
            self.grid_window.after(100, lambda: self.grid_window and self.grid_window.add(self.video_path))

    def open_rename_tool(self):
        """Open the folder renaming tool (on the current video's folder), or bring it forward."""
        if self.rename_window is not None:
            self.rename_window.lift()
            return
        folder = os.path.dirname(os.path.abspath(self.video_path)) if self.video_path else None
        self.rename_window = RenameTool(self, VIDEO_EXTENSIONS, folder)

    def release_folder(self, folder):
        """Let go of any video from folder that's open (Windows can't rename open files).

        Returns a function to call afterwards with {old path (normcased): new path};
        it reopens the main window's video under its new name, with its sections.
        """
        key = os.path.normcase(os.path.abspath(folder))
        return self._release(
            lambda path: os.path.normcase(os.path.dirname(os.path.abspath(path))) == key)

    def _release(self, matches):
        """release_folder() for the open videos whose paths matches(path) picks."""
        if self.grid_window is not None:
            for tile in list(self.grid_window.tiles):
                if tile.path and matches(tile.path):
                    self.grid_window.remove(tile)
        if not self.video_path or not matches(self.video_path):
            return lambda mapping: None
        path, position = self.video_path, self.position.get()
        sections, start, end = list(self.sections), self.start_text.get(), self.end_text.get()
        self.sections = []  # kept above; don't ask about losing them
        self.clear_video()

        def reopen(mapping):
            new = mapping.get(os.path.normcase(os.path.abspath(path)), path)
            if not os.path.exists(new):
                return
            self.open_video(new)
            if self.video_path == new:  # put the work back as it was
                self.sections = sections
                self._sections_changed()
                self.start_text.set(start)
                self.end_text.set(end)
                self._seek(position)

        return reopen

    # ------------------------------------------------------------ rename
    def start_rename(self):
        """Edit the video's name in place of its label; Enter renames the file."""
        if not self.video_path or self.rename_entry is not None:
            return
        stem = os.path.splitext(os.path.basename(self.video_path))[0]
        entry = self.rename_entry = ttk.Entry(self.file_label.master)
        entry.insert(0, stem)
        entry.select_range(0, "end")
        entry.place(in_=self.file_label, x=0, y=0, relwidth=1, relheight=1)
        entry.focus_set()
        entry.bind("<Return>", lambda e: self._finish_rename(True) or "break")
        entry.bind("<Escape>", lambda e: self._finish_rename(False) or "break")
        entry.bind("<FocusOut>", lambda e: self._finish_rename(True))

    def _finish_rename(self, keep):
        entry, self.rename_entry = self.rename_entry, None
        if entry is None:
            return
        name = entry.get().strip().rstrip(".")
        entry.destroy()
        self.root.focus_set()
        if not keep or not self.video_path:
            return
        folder = os.path.dirname(os.path.abspath(self.video_path))
        old = os.path.basename(self.video_path)
        stem, ext = os.path.splitext(old)
        if not name or name == stem:
            return
        if any(c in files.INVALID_NAME_CHARS or ord(c) < 32 for c in name):
            messagebox.showerror("Can't rename", "A file name can't contain any of these: "
                                 + " ".join(files.INVALID_NAME_CHARS))
            return
        if self.busy:
            self.status.set("Wait for the save to finish (or cancel it) before renaming.")
            return
        new = name + ext
        if os.path.exists(os.path.join(folder, new)) and \
                os.path.normcase(new) != os.path.normcase(old):
            messagebox.showerror("Can't rename", f"\u201c{new}\u201d already exists in this folder.")
            return
        key = os.path.normcase(os.path.abspath(self.video_path))
        reopen = self._release(lambda path: os.path.normcase(os.path.abspath(path)) == key)
        self._rename_when_free(folder, old, new, reopen, tries=15)

    def _rename_when_free(self, folder, old, new, reopen, tries):
        # The players take a moment to let go of the file, and Windows won't rename
        # a file that's still open, so retry for a couple of seconds.
        try:
            files.rename_all(folder, [(old, new)])
        except OSError as e:
            if tries > 0 and not isinstance(e, FileExistsError):
                self.root.after(150, lambda: self._rename_when_free(folder, old, new, reopen,
                                                                    tries - 1))
                return
            reopen({})  # open it again under its old name
            messagebox.showerror("Couldn't rename", str(e))
            return
        reopen({os.path.normcase(os.path.join(folder, old)): os.path.join(folder, new)})
        if self.rename_window is not None:
            self.rename_window.reload()
        self.status.set(f"Renamed to {new}")

    # ----------------------------------------------------------- updates
    def _check_for_update(self):
        try:
            found = updater.check()
        except (OSError, ValueError, KeyError):
            return  # offline, or GitHub is busy: try again next time
        if found:
            self.root.after(0, lambda: self._update_found(found))

    def _update_found(self, found):
        self.new_release = found
        self.update_btn.config(text=f"Update to {found['version']}")
        self.update_btn.pack(side="right", padx=(10, 0))
        Tooltip(self.update_btn, "A newer GifMaker is out: click to install it "
                                 "(GifMaker restarts)" if updater.is_installed() else
                                 "A newer GifMaker is out: click to open its download page")

    def install_update(self):
        """Download the new installer and run it; it replaces and restarts the app."""
        found = self.new_release
        if not updater.is_installed() or not found["setup_url"]:
            webbrowser.open(found["page"])  # a portable copy: download it by hand
            return
        if self.busy:
            self.status.set("Wait for the save to finish (or cancel it) before updating.")
            return
        lost = f"\n\nYour {len(self.sections)} section(s) will be lost." if self.sections else ""
        if not messagebox.askyesno("Update GifMaker",
                                   f"Download and install GifMaker {found['version']}? It closes "
                                   f"and opens again when it's done.{lost}"):
            return
        self.update_btn.config(state="disabled")

        def progress(frac):
            self.root.after(0, lambda: self.status.set(f"Downloading the update... {frac:.0%}"))

        def work():
            try:
                path = updater.download(found, progress)
            except OSError as e:
                self.root.after(0, lambda e=e: self._update_failed(e))
                return
            self.root.after(0, lambda: self._run_installer(path))

        self.status.set("Downloading the update...")
        threading.Thread(target=work, daemon=True).start()

    def _update_failed(self, error):
        self.update_btn.config(state="normal")
        self.status.set(f"Couldn't download the update: {error}")

    def _run_installer(self, path):
        try:
            updater.install(path)
        except OSError as e:
            self._update_failed(e)
            return
        self.close()

    def _handed_over(self, path):
        """Another copy of GifMaker passed us a video: come to the front and open it."""
        if self.root.state() == "iconic":
            self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)  # Windows won't always let a window come forward
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()
        if self.busy:
            self.status.set("Wait for the save to finish (or cancel it), then open "
                            f"{os.path.basename(path)} again.")
            return
        self.open_video(path)

    def close(self):
        self._remember_files_width()
        self.browser.close()
        if self.listener:
            self.listener.close()
        # Stop the sound before Tk and Python shut down, or PortAudio can crash on exit.
        if self.grid_window is not None:
            self.grid_window.close()
        self.player.close()
        self.root.destroy()

    @property
    def playing(self):
        return self.player.playing

    # ---------------------------------------------------------------- UI
    def _styles(self):
        """Fonts for this window's own styles (redone when the theme changes)."""
        style = ttk.Style(self.root)
        base = font.nametofont("TkDefaultFont")
        nav = base.copy()
        nav.configure(size=20, weight="bold")
        bold = base.copy()
        bold.configure(weight="bold")
        self._fonts = (nav, bold)  # Tk forgets fonts that Python stops referring to
        style.configure("Nav.TButton", font=nav, padding=(2, 0))
        style.configure("Head.TLabel", font=bold)

    def _build_ui(self):
        self._styles()
        theme.on_change(self._styles)
        main = self.main = ttk.Frame(self.root, padding=(10, 8, 10, 6))
        main.pack(fill="both", expand=True)

        # Toolbar: the file on the left, the other tools on the right.
        top = ttk.Frame(main)
        for text, command, tip in (
                ("Open...", self.open_video, "Open a video  (Ctrl+O, or drag one onto the window)"),
                ("Open link...", self.open_link,
                 "Download a video from YouTube or another site and open it  (Ctrl+L, or paste "
                 "or drag a link onto the window)"),
                ("Close", self.clear_video, "Close the video and start again"),
                ("Delete...", self.delete_file,
                 "Delete this video and open the next one in the folder  (Delete key)")):
            button = ttk.Button(top, text=text, command=command)
            button.pack(side="left", padx=(0, 4))
            Tooltip(button, tip)
        ttk.Checkbutton(top, text="Dark", variable=self.dark_mode,
                        command=self._toggle_dark).pack(side="right")
        ttk.Separator(top, orient="vertical").pack(side="right", fill="y", padx=10, pady=3)
        grid = ttk.Button(top, text="Grid player", command=self.open_grid)
        grid.pack(side="right")
        Tooltip(grid, "Play several videos at once, each in a panel you can move, resize and loop")
        rename = ttk.Button(top, text="Rename files", command=self.open_rename_tool)
        rename.pack(side="right", padx=(0, 4))
        Tooltip(rename, "Rename everything in a folder with a numbered pattern, "
                        "in name, date, size or colour order")
        one = ttk.Checkbutton(top, text="One window", variable=self.one_window)
        one.pack(side="right", padx=(0, 12))
        Tooltip(one, "Videos you open from Explorer play in this window, "
                     "instead of opening another GifMaker")
        self.files_btn = ttk.Button(top, text="Files", width=6, command=self.toggle_files)
        self.files_btn.pack(side="right", padx=(0, 12))
        Tooltip(self.files_btn, "Show the videos in this folder as thumbnails beside the "
                                "player  (F3)")
        autoplay = ttk.Checkbutton(top, text="Autoplay", variable=self.autoplay)
        autoplay.pack(side="right", padx=(0, 10))
        Tooltip(autoplay, "Start playing each video as soon as it opens, "
                          "e.g. when going through a folder with ‹ ›")
        # width=1: a long name is cut off rather than widening the window.
        self.file_label = ttk.Label(top, text="No video loaded", style="Muted.TLabel", width=1)
        self.file_label.pack(side="left", fill="x", expand=True, padx=(8, 8))
        Tooltip(self.file_label, lambda: f"{self.video_path}\nDouble-click to rename it"
                if self.video_path else "No video loaded")
        self.file_label.bind("<Double-Button-1>", lambda e: self.start_rename())
        self.rename_entry = None

        # The video, with back/next buttons for the other videos in its folder. It
        # takes whatever room the window has, and the picture is scaled to fit.
        # The video and (when shown) the folder's files beside it, with a draggable
        # divider between them.
        self.split = ttk.PanedWindow(main, orient="horizontal")
        view = ttk.Frame(self.split)
        self.split.add(view, weight=1)
        self.browser = FileBrowser(self.split, self.ffmpeg, self._browser_open,
                                   lambda: self.toggle_files(False))
        self.files_shown = False
        self.back_btn = ttk.Button(view, text="‹", width=2, style="Nav.TButton",
                                   command=lambda: self.open_neighbour(-1))
        self.back_btn.pack(side="left", fill="y", padx=(0, 4))
        Tooltip(self.back_btn, lambda: self._neighbour_tip(-1))
        self.next_btn = ttk.Button(view, text="›", width=2, style="Nav.TButton",
                                   command=lambda: self.open_neighbour(1))
        self.next_btn.pack(side="right", fill="y", padx=(4, 0))
        Tooltip(self.next_btn, lambda: self._neighbour_tip(1))
        self.canvas = tk.Canvas(view, width=PREVIEW_W, height=PREVIEW_H,
                                bg="#111", highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Double-Button-1>", self.toggle_fullscreen)
        self.canvas.bind("<Configure>", self._on_view_resize)
        self.canvas_text = self.canvas.create_text(
            PREVIEW_W // 2, PREVIEW_H // 2, fill="#888",
            text="Open a video, or drag and drop one here.")
        self.canvas_img = self.canvas.create_image(PREVIEW_W // 2, PREVIEW_H // 2)

        # Timeline across the full width, with the time and fullscreen on the right.
        scrub = self.scrub_bar = ttk.Frame(main)
        self.timeline = Timeline(scrub, on_seek=self._on_drag, on_seek_end=self._settle,
                                 on_marker=self._on_marker, on_section=self._select_section)
        self.timeline.pack(side="left", fill="x", expand=True)
        self.position.trace_add("write", lambda *_: self.timeline.set_position(self.position.get()))
        self.pos_label = ttk.Label(scrub, width=19, anchor="e")
        self.pos_label.pack(side="left", padx=(6, 0), anchor="n", pady=(4, 0))
        fs = ttk.Button(scrub, text="⛶", width=3, command=self.toggle_fullscreen)
        fs.pack(side="left", padx=(6, 0), anchor="n")
        Tooltip(fs, "Fullscreen  (F11 or double-click the video; Esc to leave)")
        self._show_time(0)

        # Play bar: playing and stepping, marking, sound, then saving on the right.
        bar = self.play_bar = ttk.Frame(main)
        self.play_btn = ttk.Button(bar, text="▶ Play", width=9, command=self.toggle_play)
        self.play_btn.pack(side="left")
        Tooltip(self.play_btn, "Play or pause  (Space)")
        self.clip_btn = ttk.Button(bar, text="↻ Play clip", width=11,
                                   command=self.toggle_play_clip)
        self.clip_btn.pack(side="left", padx=(2, 0))
        Tooltip(self.clip_btn, "Play the clip between the markers over and over")
        loop = ttk.Checkbutton(bar, text="Loop", variable=self.loop_video)
        loop.pack(side="left", padx=(6, 0))
        Tooltip(loop, "Play starts the video again from the beginning when it reaches the end")
        ttk.Frame(bar, width=8).pack(side="left")
        for label, delta, tip in (("-1s", -1, "Back 1 second"),
                                  ("-1f", "-f", "Back 1 frame  (←)"),
                                  ("+1f", "+f", "Forward 1 frame  (→)"),
                                  ("+1s", 1, "Forward 1 second")):
            button = ttk.Button(bar, text=label, width=3, command=lambda d=delta: self.step(d))
            button.pack(side="left", padx=(0, 1))
            Tooltip(button, tip)
        ttk.Frame(bar, width=8).pack(side="left")
        # Quick marking while watching: clip start, clip end, and keep as a section.
        for text, command, tip in (
                ("[", self.set_start, "Start the clip here (green marker)  ( [ key )"),
                ("]", self.set_end, "End the clip here (red marker)  ( ] key )")):
            button = ttk.Button(bar, text=text, width=2, command=command)
            button.pack(side="left", padx=(0, 1))
            Tooltip(button, tip)
        self.section_btn = ttk.Button(bar, text="+ Section", width=9, command=self._section_button)
        self.section_btn.pack(side="left")
        Tooltip(self.section_btn, "Keep the clip as a section of the video you save "
                                  "(or finish changing the section you're editing)")
        speed = ttk.Combobox(bar, textvariable=self.play_speed, width=5, state="readonly",
                             values=[f"{v:g}×" for v in PLAY_SPEEDS])
        speed.pack(side="left", padx=(10, 0))
        speed.bind("<<ComboboxSelected>>", lambda e: self._restart_playback())
        Tooltip(speed, "Playback speed (sound keeps its pitch)")
        self.sound_check = ttk.Checkbutton(bar, text="Sound", variable=self.sound,
                                           command=self._sound_toggled)
        self.sound_check.pack(side="left", padx=(6, 2))
        vol = ttk.Scale(bar, from_=0, to=100, variable=self.volume, length=70,
                        command=lambda v: self._volume_changed())
        vol.pack(side="left")
        Tooltip(vol, "Volume")
        boost = ttk.Scale(bar, from_=100, to=MAX_BOOST, variable=self.boost, length=50,
                          command=lambda v: self._volume_changed())
        boost.pack(side="left", padx=(6, 2))
        Tooltip(boost, f"Boost: make quiet videos louder, up to {MAX_BOOST}%. "
                       "Loud parts may distort when boosted a lot.")
        self.boost_label = ttk.Label(bar, text="100%", width=5)
        self.boost_label.pack(side="left")
        Tooltip(self.boost_label, "Volume boost")
        self.make_btn = ttk.Button(bar, text="Make GIF...", command=self.make_gif)
        self.make_btn.pack(side="right")
        Tooltip(self.make_btn, "Make a GIF, WebP or animated PNG of the clip (or of each "
                               "section), with a live preview of the options")
        self.video_btn = ttk.Button(bar, text="Save video...", command=self.save_clip_video)
        self.video_btn.pack(side="right", padx=4)
        Tooltip(self.video_btn, "Save the sections (or just the clip) as a new video")
        self.snap_btn = ttk.Button(bar, text="Snapshot...", command=self.save_snapshot)
        self.snap_btn.pack(side="right", padx=(10, 0))
        Tooltip(self.snap_btn, "Save the frame you're looking at as a picture")

        # The clip and the sections.
        edit = ttk.Frame(main)
        edit.columnconfigure(1, weight=1)
        rows = []
        for r, heading in enumerate(("Clip", "Sections")):
            ttk.Label(edit, text=heading, style="Head.TLabel").grid(
                row=r, column=0, sticky="w", padx=(0, 12), pady=3)
            row = ttk.Frame(edit)
            row.grid(row=r, column=1, sticky="ew", pady=3)
            rows.append(row)
        clip, sec = rows

        for text, var, tip in (
                ("Start", self.start_text, "green marker; or press [ at the playhead"),
                ("End", self.end_text, "red marker; or press ] at the playhead")):
            ttk.Label(clip, text=text).pack(side="left", padx=(0, 4))
            entry = ttk.Entry(clip, textvariable=var, width=10)
            entry.pack(side="left", padx=(0, 12))
            entry.bind("<Return>", lambda e, v=var: self.goto(v))
            Tooltip(entry, f"Seconds or m:ss.sss (Enter jumps there). Or drag the {tip}.")
        self.length_label = ttk.Label(clip)
        self.length_label.pack(side="left")
        for var in (self.start_text, self.end_text):
            var.trace_add("write", lambda *_: self._range_changed())

        self.remove_section_btn = ttk.Button(sec, text="Remove", command=self.remove_section,
                                             state="disabled")
        self.remove_section_btn.pack(side="left")
        Tooltip(self.remove_section_btn, "Remove the section you're editing")
        self.clear_sections_btn = ttk.Button(sec, text="Clear all", command=self.clear_sections)
        self.clear_sections_btn.pack(side="left", padx=(4, 10))
        self.section_label = ttk.Label(sec, style="Muted.TLabel", width=1)
        self.section_label.pack(side="left", fill="x", expand=True)

        # Status line; the progress bar and Cancel only show while saving.
        statusbar = ttk.Frame(main)
        ttk.Checkbutton(statusbar, text="Show in folder after saving",
                        variable=self.show_folder).pack(side="right", padx=(10, 0))
        self.cancel_btn = ttk.Button(statusbar, text="Cancel", command=self.cancel)
        self.progress = ttk.Progressbar(statusbar, maximum=1.0, length=160)
        self.update_btn = ttk.Button(statusbar, command=self.install_update)
        self.status_label = ttk.Label(statusbar, textvariable=self.status, style="Muted.TLabel",
                                      width=1)
        self.status_label.pack(side="left", fill="x", expand=True)

        # The video is packed last so that it's what shrinks when the window does.
        top.pack(side="top", fill="x")
        statusbar.pack(side="bottom", fill="x", pady=(8, 0))
        edit.pack(side="bottom", fill="x", pady=(8, 0))
        bar.pack(side="bottom", fill="x", pady=(2, 0))
        scrub.pack(side="bottom", fill="x")
        self.split.pack(side="top", fill="both", expand=True, pady=(8, 4))
        # Hidden in fullscreen, which keeps just the video, timeline and play bar.
        self._windowed_only = [top, edit, statusbar]
        self._rows = [(w, w.pack_info()) for w in main.pack_slaves()]

        self.root.bind("<space>", lambda e: self._key_set(e, self.toggle_play))
        self.root.bind("<Left>", lambda e: self._key_step(e, "-f"))
        self.root.bind("<Right>", lambda e: self._key_step(e, "+f"))
        self.root.bind("<bracketleft>", lambda e: self._key_set(e, self.set_start))
        self.root.bind("<bracketright>", lambda e: self._key_set(e, self.set_end))
        self.root.bind("<Prior>", lambda e: self._key_set(e, lambda: self.open_neighbour(-1)))
        self.root.bind("<Next>", lambda e: self._key_set(e, lambda: self.open_neighbour(1)))
        self.root.bind("<Delete>", lambda e: self._key_set(e, self.delete_file))
        self.root.bind("<Control-o>", lambda e: self.open_video())
        self.root.bind("<Control-l>", lambda e: self.open_link())
        self.root.bind("<Control-v>", self._paste_link)
        self.root.bind("<F2>", lambda e: self.start_rename())
        self.root.bind("<F11>", self.toggle_fullscreen)
        self.root.bind("<F3>", lambda e: self.toggle_files())
        self.root.bind("<Escape>", lambda e: self.fullscreen and self.toggle_fullscreen())
        self._update_nav()
        # Let the window be shrunk until the video is small, but no narrower than
        # the play bar (and keep its size from now on, whatever the labels say).
        self.root.update_idletasks()
        req_h = main.winfo_reqheight() - PREVIEW_H + MIN_VIEW_H
        self.root.minsize(bar.winfo_reqwidth() + 20, req_h)
        self.root.geometry(f"{self.root.winfo_reqwidth()}x{self.root.winfo_reqheight()}")

    def _show_time(self, t):
        self.pos_label.config(text=f"{ff.format_time(t)} / {ff.format_time(self.duration)}")

    # ------------------------------------------------------ drag & drop
    def _enable_drop(self):
        if not hasattr(self.root, "drop_target_register"):
            return

        def register(widget):
            widget.drop_target_register(DND_FILES, DND_TEXT)
            widget.dnd_bind("<<DropEnter>>", self._on_drop_enter)
            widget.dnd_bind("<<DropLeave>>", self._on_drop_leave)
            widget.dnd_bind("<<Drop>>", self._on_drop)
            for child in widget.winfo_children():
                register(child)

        try:
            register(self.root)
        except tk.TclError:
            pass

    def _on_drop_enter(self, event):
        self.canvas.config(highlightthickness=3, highlightbackground="#3a7bd5")
        return event.action

    def _on_drop_leave(self, event):
        self.canvas.config(highlightthickness=0)
        return event.action

    def _on_drop(self, event):
        self.canvas.config(highlightthickness=0)
        text = (event.data or "").strip()
        if downloader.looks_like_link(text):  # a link dragged from a browser
            self.root.after(10, lambda: self.open_link(text.split()[0]))
            return COPY
        files = [f for f in self.root.tk.splitlist(event.data) if os.path.isfile(f)]
        if not files:
            return event.action
        if self.busy:
            self.status.set("Wait for the GIF to finish before opening another video.")
        else:
            # Open after the drop completes so Explorer isn't left waiting on dialogs.
            self.root.after(10, lambda: self.open_video(files[0]))
        return COPY

    # ------------------------------------------------------------- links
    def open_link(self, url=""):
        """Download a video from a site (with yt-dlp) and open it."""
        if self.busy:
            self.status.set("Wait for the save to finish (or cancel it) first.")
            return
        existing = [w for w in self.root.winfo_children() if isinstance(w, LinkDialog)]
        if existing:
            existing[0].lift()
            if url and not existing[0].busy:
                existing[0].url.set(url)
            return
        if self.fullscreen:
            self.toggle_fullscreen()
        LinkDialog(self, url, on_done=self._downloaded)

    def _downloaded(self, path):
        self.open_video(path)
        if self.video_path == path:
            self.status.set(f"Downloaded to {path}")

    def _paste_link(self, event):
        """Ctrl+V with a link copied (and not typing in a box): download it."""
        if isinstance(event.widget, (ttk.Entry, tk.Entry, ttk.Combobox, ttk.Spinbox)):
            return
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            return
        if downloader.looks_like_link(text):
            self.open_link(text)

    # ----------------------------------------------------------- loading
    def open_video(self, path=None):
        if self.busy:
            return
        self.stop_playback()
        path = path or filedialog.askopenfilename(title="Open video", filetypes=VIDEO_TYPES)
        if not path:
            return
        if self.sections and not messagebox.askyesno(
                "Open another video",
                f"Opening another video clears your {len(self.sections)} "
                f"section{'s' if len(self.sections) > 1 else ''}. Open it anyway?"):
            return
        try:
            info = ff.probe(self.ffmpeg, path)
        except Exception as e:
            messagebox.showerror("Could not open video", str(e))
            return
        self.video_path = path
        self.duration = duration = info.duration
        self.video_fps = info.fps or 30.0
        self.video_width, self.video_height = w, h = info.width, info.height
        self.has_audio = info.has_audio
        self.sound_check.config(state="normal" if info.has_audio else "disabled")
        self.transform = dict(NO_TRANSFORM)
        self.clear_sections()
        details = [ff.format_time(duration), f"{w}\u00D7{h}", f"{self.video_fps:g} fps"]
        if not info.has_audio:
            details.append("no sound")
        self.file_label.config(text=f"{os.path.basename(path)}    " + "  \u00B7  ".join(details),
                               style="TLabel")
        self.timeline.set_duration(duration)
        self.video_token += 1
        self.shown_t = None
        self._build_thumbs()
        self.start_text.set(ff.format_time(0))
        self.end_text.set(ff.format_time(min(duration, 3.0)))
        self.status.set("")
        self._update_nav()
        self._seek(0.0)
        if self.autoplay.get():
            self._start_playback(0.0, None, clip=False)

    # ------------------------------------------------- folder navigation
    def _folder_videos(self):
        """Videos in the current video's folder, in Explorer-like (natural) order."""
        folder = os.path.dirname(os.path.abspath(self.video_path))
        try:
            names = os.listdir(folder)
        except OSError:
            return []

        def natural(name):  # "clip 2" before "clip 10"
            return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name.lower())]

        names = [n for n in names if os.path.splitext(n)[1].lower() in VIDEO_EXTENSIONS
                 and os.path.isfile(os.path.join(folder, n))]
        return [os.path.join(folder, n) for n in sorted(names, key=natural)]

    def _neighbour(self, step):
        """The video `step` places away in the folder (-1 back, 1 next), or None."""
        if not self.video_path:
            return None
        files = self._folder_videos()
        here = os.path.normcase(os.path.abspath(self.video_path))
        keys = [os.path.normcase(f) for f in files]
        if here not in keys:
            return None
        i = keys.index(here) + step
        return files[i] if 0 <= i < len(files) else None

    def _neighbour_tip(self, step):
        path = self._neighbour(step)
        if path:
            return f"{'Next' if step > 0 else 'Previous'}: {os.path.basename(path)}  (Page {'Down' if step > 0 else 'Up'})"
        return "No more videos in this folder" if self.video_path else "Open a video first"

    def open_neighbour(self, step):
        if self.busy:
            self.status.set("Wait for the save to finish (or cancel it) first.")
            return
        path = self._neighbour(step)
        if path:
            self.open_video(path)
        elif self.video_path:
            self.status.set(f"This is the {'last' if step > 0 else 'first'} video in the folder.")

    def _update_nav(self):
        for button, step in ((self.back_btn, -1), (self.next_btn, 1)):
            button.config(state="normal" if self._neighbour(step) else "disabled")
        if self.files_shown:
            self.browser.show(self._folder_videos() if self.video_path else [], self.video_path)

    # ------------------------------------------------------- files panel
    def toggle_files(self, show=None):
        """Show or hide the thumbnails of the folder's videos beside the player."""
        show = not self.files_shown if show is None else show
        if show == self.files_shown:
            return
        if show:
            width = int(theme.load_setting("files_width", 370))
            self.split.add(self.browser, weight=0)
            self.files_shown = True
            self.root.update_idletasks()
            total = self.split.winfo_width()
            self.split.sashpos(0, max(200, total - width))
            self._update_nav()
        else:
            self._remember_files_width()
            self.split.forget(self.browser)
            self.files_shown = False
        self.files_btn.config(text="Files \u2713" if show else "Files")
        if not self.fullscreen:  # fullscreen hides it for the moment, not for good
            theme.save_setting("files_shown", show)

    def _remember_files_width(self):
        if self.files_shown and self.browser.winfo_width() > 50:
            theme.save_setting("files_width", self.browser.winfo_width())

    def _browser_open(self, path):
        if self.busy:
            self.status.set("Wait for the save to finish (or cancel it) first.")
            return
        self.open_video(path)

    # -------------------------------------------------------- fullscreen
    def toggle_fullscreen(self, _event=None):
        """Just the video, timeline and play bar, filling the screen (and back)."""
        self.fullscreen = not self.fullscreen
        if self.fullscreen:
            self._files_before_fullscreen = self.files_shown
            self.toggle_files(False)
            for widget in self._windowed_only:
                widget.pack_forget()
            self.root.attributes("-fullscreen", True)
        else:
            self.root.attributes("-fullscreen", False)
            for widget, _ in self._rows:  # re-pack every row in its original order
                widget.pack_forget()
            for widget, packing in self._rows:
                widget.pack(packing)
            if getattr(self, "_files_before_fullscreen", False):
                self.root.after(50, lambda: self.toggle_files(True))

    def _on_view_resize(self, event):
        """The video area changed size: centre the picture now, re-render it shortly."""
        if (event.width, event.height) == (self.view_w, self.view_h):
            return
        self.view_w, self.view_h = event.width, event.height
        self.canvas.coords(self.canvas_img, event.width // 2, event.height // 2)
        self.canvas.coords(self.canvas_text, event.width // 2, event.height // 2)
        if self.resize_job:
            self.root.after_cancel(self.resize_job)
        self.resize_job = self.root.after(VIEW_RESIZE_MS, self._view_resized)

    def _view_resized(self):
        self.resize_job = None
        if not self.video_path:
            return
        if self.playing:  # carry on playing at the new size
            self._restart_playback()
        else:
            self.shown_t = None
            self._settle()

    def gain_settings(self):
        """(volume 0..1, boost 1..4) for every player in the app."""
        return self.volume.get() / 100, self.boost.get() / 100

    def _volume_changed(self):
        """Apply the Vol and Boost sliders straight away to everything playing."""
        volume, boost = self.gain_settings()
        self.boost_label.config(text=f"{boost * 100:.0f}%")
        players = [self.player]
        if self.grid_window is not None:
            players += [tile.player for tile in self.grid_window.tiles]
        players += [w.player for w in self.root.winfo_children() if isinstance(w, ExportDialog)]
        for player in players:
            player.volume, player.boost = volume, boost

    def _play_speed(self):
        try:
            return float(self.play_speed.get().rstrip("\u00D7"))
        except ValueError:
            return 1.0

    def _restart_playback(self):
        """Pick up a new size or speed while playing, carrying on from the same place."""
        if self.playing:
            pos, end, clip = self.position.get(), self.player.end, self.play_clip
            self.player.stop()
            self._start_playback(pos, end, clip)

    def _frame_size(self):
        """Size to decode frames at: as big as fits in the video area."""
        return ff.fit_size(self.video_width, self.video_height, self.view_w, self.view_h,
                           upscale=True)

    def clear_video(self):
        """Close the current video and put the window back as it was at startup."""
        if self.busy:
            self.status.set("Wait for the save to finish (or cancel it) before clearing.")
            return
        self.stop_playback()
        self.video_token += 1  # stops the thumbnail pass and drops pending frames
        if self.settle_job:
            self.root.after_cancel(self.settle_job)
            self.settle_job = None
        self.video_path = None
        self.duration = 0.0
        self.video_fps = 0.0
        self.video_width = self.video_height = 0
        self.has_audio = False
        self.transform = dict(NO_TRANSFORM)
        self.clear_sections()
        self.thumbs = []
        self.thumbs_done = False
        self.frame_want = None
        self.shown_t = None
        self.preview_image = None
        self.canvas.itemconfig(self.canvas_img, image="")
        self.canvas.itemconfig(self.canvas_text, text="Open a video, or drag and drop one here.")
        self.file_label.config(text="No video loaded", style="Muted.TLabel")
        self.sound_check.config(state="normal")
        self.position.set(0.0)
        self._show_time(0)
        self.start_text.set(ff.format_time(0))
        self.end_text.set(ff.format_time(0))
        self.timeline.set_duration(0)
        self._update_nav()
        self.status.set("")

    def delete_file(self):
        """Delete the open video (to the Recycle Bin on Windows) after asking."""
        if not self.video_path:
            return
        if self.busy:
            self.status.set("Wait for the save to finish (or cancel it) before deleting.")
            return
        path = self.video_path
        message = f"Delete \u201c{os.path.basename(path)}\u201d?\n\n{files.where_it_goes()}"
        if self.sections:
            message += f"\n\nYour {len(self.sections)} section(s) will be lost too."
        if not messagebox.askyesno("Delete file", message, icon="warning"):
            return
        after = self._neighbour(1) or self._neighbour(-1)
        self.sections = []  # already confirmed, so don't ask again when opening the next one
        self.clear_video()  # stops playback and everything else reading the file
        self._delete_when_free(path, after, tries=15)

    def _delete_when_free(self, path, after, tries):
        # ffmpeg processes reading the file take a moment to exit, and Windows won't
        # delete a file that's still open, so retry for a couple of seconds.
        try:
            files.send_to_trash(path)
        except OSError as e:
            if tries > 0 and os.path.exists(path):
                self.root.after(150, lambda: self._delete_when_free(path, after, tries - 1))
                return
            messagebox.showerror("Couldn't delete", f"{os.path.basename(path)} wasn't deleted:\n{e}")
            return
        if after and os.path.exists(after):
            self.open_video(after)
        recycled = " (it's in the Recycle Bin)" if sys.platform == "win32" else ""
        self.status.set(f"Deleted {os.path.basename(path)}{recycled}.")

    # ----------------------------------------------------------- preview
    # While dragging, the preview shows cached keyframe thumbnails instantly (or a
    # quick keyframe grab where the cache hasn't reached yet), then the exact frame
    # once the slider rests. Only one ffmpeg frame grab runs at a time; the newest
    # request wins, and whichever image is closest to the slider is shown.
    def _build_thumbs(self):
        token = self.video_token
        thumbs = self.thumbs = []
        self.thumbs_done = False
        pw, ph = ff.fit_size(self.video_width, self.video_height, PREVIEW_W, PREVIEW_H)
        w, h = self.thumb_size = (max(2, pw // 2), max(2, ph // 2))
        gap = self.duration / MAX_THUMBNAILS
        path = self.video_path

        def work():
            for item in ff.iter_keyframes(self.ffmpeg, path, w, h, gap,
                                          lambda: token != self.video_token):
                thumbs.append(item)
            if token == self.video_token:
                self.thumbs_done = True

        threading.Thread(target=work, daemon=True).start()

    def _on_drag(self, t):
        if not self.video_path:
            return
        self.stop_playback()
        self.position.set(t)
        self._show_time(t)
        if not self._show_thumb(t):
            self._request_frame(t, exact=False)
        if self.settle_job:
            self.root.after_cancel(self.settle_job)
        self.settle_job = self.root.after(SCRUB_SETTLE_MS, self._settle)

    def _settle(self, _event=None):
        if self.settle_job:
            self.root.after_cancel(self.settle_job)
            self.settle_job = None
        if self.video_path and not self.playing:
            self._request_frame(self.position.get(), exact=True)

    def _seek(self, t):
        self.stop_playback()
        t = min(self.duration, max(0.0, t))
        self.position.set(t)
        self._show_time(t)
        self._settle()

    def _show_thumb(self, t):
        """Show the cached keyframe for time t. Returns False if it isn't cached yet."""
        thumbs = self.thumbs
        i = bisect.bisect_right(thumbs, t, key=itemgetter(0)) - 1
        if i < 0 or (not self.thumbs_done and thumbs[-1][0] < t):
            return False
        thumb_t, data = thumbs[i]
        if self.shown_t is None or abs(thumb_t - t) < abs(self.shown_t - t):
            w, h = self.thumb_size
            img = tk.PhotoImage(data=b"P6 %d %d 255\n" % (w, h) + data, format="ppm")
            fw, fh = self._frame_size()
            self._display(img.zoom(max(1, min(fw // w, fh // h))), thumb_t)
        return True

    def _display(self, img, t):
        self.preview_image = img
        self.shown_t = t
        self.canvas.itemconfig(self.canvas_img, image=img)
        self.canvas.itemconfig(self.canvas_text, text="")

    def _request_frame(self, t, exact):
        self.frame_want = (self.video_token, self.video_path, t, exact)
        if not self.frame_busy:
            self._launch_frame()

    def _launch_frame(self):
        token, path, t, exact = self.frame_want
        self.frame_want = None
        fw, fh = self._frame_size()
        self.frame_busy = True

        def work():
            data = ff.grab_frame(self.ffmpeg, path, t, fw, fh, fast=not exact,
                                 pre_vf=f"scale={fw}:{fh}")
            self.frame_queue.put((token, t, data))

        threading.Thread(target=work, daemon=True).start()

    def _poll_frame_queue(self):
        try:
            while True:
                token, t, data = self.frame_queue.get_nowait()
                self.frame_busy = False
                if not data or token != self.video_token or self.playing:
                    continue
                pos = self.position.get()
                if self.shown_t is None or abs(t - pos) <= abs(self.shown_t - pos):
                    try:
                        self._display(tk.PhotoImage(data=data, format="ppm"), t)
                    except tk.TclError:
                        pass
        except queue.Empty:
            pass
        if self.frame_want and not self.frame_busy:
            self._launch_frame()
        self.root.after(15, self._poll_frame_queue)

    # ---------------------------------------------------------- playback
    def toggle_play(self):
        if self.playing and not self.play_clip:
            self.stop_playback()
            return
        if not self.video_path:
            return
        start = self.position.get()
        if start >= self.duration - 0.05:
            start = 0.0
        self._start_playback(start, None, clip=False)

    def toggle_play_clip(self):
        if self.playing and self.play_clip:
            self.stop_playback()
            return
        if not self.video_path:
            return
        rng = self._valid_range()
        if rng:
            self._start_playback(*rng, clip=True)

    def _start_playback(self, start, end, clip):
        self.play_clip = clip
        size = self._frame_size()
        fps = min(self.video_fps, MAX_PLAYBACK_FPS)
        self.player.muted = not self.sound.get()
        self.player.volume, self.player.boost = self.gain_settings()
        self.player.start(self.video_path, start, size, fps, end=end,
                          speed=self._play_speed(), sound=self.has_audio)
        if self.has_audio and self.player.no_sound_device:
            self.status.set("No sound device found, so playing without sound.")
        if self.play_image is None:
            self.play_image = tk.PhotoImage()
        self.canvas.itemconfig(self.canvas_img, image=self.play_image)
        self.canvas.itemconfig(self.canvas_text, text="")
        if clip:
            self.play_btn.config(text="\u25B6 Play")
            self.clip_btn.config(text="\u25A0 Stop clip")
        else:
            self.play_btn.config(text="\u275A\u275A Pause")
            self.clip_btn.config(text="\u21BB Play clip")

    def stop_playback(self):
        if self.playing:
            self.player.stop()
            self._playback_stopped()

    def _playback_stopped(self):
        self.play_btn.config(text="\u25B6 Play")
        self.clip_btn.config(text="\u21BB Play clip")
        self.shown_t = None  # the preview is showing a playback frame

    def _sound_toggled(self):
        self.player.muted = not self.sound.get()

    def _on_play_frame(self, t, data):
        try:
            self.play_image.configure(data=data, format="ppm")
        except tk.TclError:
            pass
        self.position.set(min(t, self.duration))
        self._show_time(t)

    def _on_play_end(self):
        self._playback_stopped()
        if self.play_clip:
            self.toggle_play_clip()  # go round the loop again
        elif self.loop_video.get() and self.video_path:
            self._start_playback(0.0, None, clip=False)  # the whole video, from the top
        else:
            self._settle()

    # ---------------------------------------------------------- snapshot
    def save_snapshot(self):
        if not self.video_path:
            messagebox.showinfo("GifMaker", "Open a video first.")
            return
        self.stop_playback()
        if self.fullscreen:  # so the preview window isn't hidden behind the video
            self.toggle_fullscreen()
        t = self.position.get()
        ExportDialog(self, "snapshot", self.transform,
                     lambda transform, *_: self._save_snapshot(t, transform),
                     time=t)

    def _save_snapshot(self, t, transform):
        self.transform = transform
        base = os.path.splitext(os.path.basename(self.video_path))[0]
        dst = filedialog.asksaveasfilename(
            title="Save snapshot as", defaultextension=".png",
            initialfile=f"{base}_{t:.2f}s.png",
            initialdir=os.path.dirname(self.video_path),
            filetypes=[("PNG image", "*.png"), ("JPEG image", "*.jpg *.jpeg")])
        if not dst:
            return
        self.status.set(f"Saving snapshot at {ff.format_time(t)}...")
        path, vf = self.video_path, ff.transform_filters(**transform)

        def work():
            err = ff.save_frame(self.ffmpeg, path, t, dst, vf)
            self.root.after(0, lambda: self._snapshot_done(err, dst))

        threading.Thread(target=work, daemon=True).start()

    def _snapshot_done(self, err, dst):
        if err:
            self.status.set("Snapshot failed.")
            messagebox.showerror("Snapshot failed", err[-1500:])
        else:
            self.status.set(f"Saved snapshot {dst}")
            if self.show_folder.get():
                show_in_folder(dst)

    def step(self, delta):
        if not self.video_path:
            return
        if delta == "-f":
            delta = -1.0 / self.video_fps
        elif delta == "+f":
            delta = 1.0 / self.video_fps
        self._seek(self.position.get() + delta)

    def goto(self, var):
        try:
            t = ff.parse_time(var.get())
        except ValueError:
            return
        self._seek(t)

    def _key_step(self, event, delta):
        if not isinstance(event.widget, _NO_SHORTCUT_WIDGETS):
            self.step(delta)

    def _key_set(self, event, fn):
        if not isinstance(event.widget, _NO_SHORTCUT_WIDGETS):
            fn()

    # ------------------------------------------------------------- range
    def set_start(self):
        t = self.position.get()
        try:
            _, end = self._range()
        except ValueError:
            end = self.duration
        if end <= t:  # past the end marker: move that out of the way
            self.end_text.set(ff.format_time(self.duration))
        self.start_text.set(ff.format_time(t))

    def set_end(self):
        t = self.position.get()
        try:
            start, _ = self._range()
        except ValueError:
            start = 0.0
        if start >= t:  # before the start marker: move that out of the way
            self.start_text.set(ff.format_time(0))
        self.end_text.set(ff.format_time(t))

    def _range(self):
        start = ff.parse_time(self.start_text.get())
        end = ff.parse_time(self.end_text.get())
        return start, end

    def _valid_range(self):
        """Return (start, end), or show what's wrong and return None."""
        try:
            start, end = self._range()
        except ValueError:
            messagebox.showerror("GifMaker", "Start and End must be times like 12.5 or 1:02.25")
            return None
        end = min(end, self.duration)
        if end <= start:
            messagebox.showerror("GifMaker", "End must be after Start.")
            return None
        return start, end

    def _range_changed(self):
        try:
            start, end = self._range()
        except ValueError:
            pass
        else:
            self.timeline.set_range(start, min(end, self.duration))
            if self.playing and self.play_clip and end > start:
                self.player.end = end  # the loop picks up a new end straight away
            if self.section_sel is not None and not self._loading_section and end > start:
                self.sections[self.section_sel] = (start, min(end, self.duration))
                self._sections_changed()
        self._update_length()

    # ---------------------------------------------------------- sections
    def _section_button(self):
        if self.section_sel is not None:  # "Done editing"
            self._select_section(None)
            return
        rng = self._valid_range()
        if rng:
            self.sections.append(rng)
            self._sections_changed()

    def remove_section(self):
        if self.section_sel is not None:
            del self.sections[self.section_sel]
            self.section_sel = None
            self._sections_changed()

    def clear_sections(self):
        self.sections, self.section_sel = [], None
        self._sections_changed()

    def _select_section(self, index):
        """Edit a section with the markers (index None: stop editing)."""
        self.section_sel = index
        if index is not None:
            start, end = self.sections[index]
            self._loading_section = True
            self.start_text.set(ff.format_time(start))
            self.end_text.set(ff.format_time(end))
            self._loading_section = False
            if not (self.playing and self.play_clip):
                self._seek(start)
        self._sections_changed()

    def _sections_changed(self):
        self.timeline.set_sections(self.sections, self.section_sel)
        n = len(self.sections)
        editing = self.section_sel is not None
        self.section_btn.config(text="\u2713 Done" if editing else "+ Section")
        self.remove_section_btn.config(state="normal" if editing else "disabled")
        self.clear_sections_btn.config(state="normal" if n else "disabled")
        if not n:
            text = ("None. To keep several parts in one video, mark each and click + Section; "
                    "Save video cuts out the rest.")
        elif editing:
            text = (f"Editing {self.section_sel + 1} of {n}: the markers move it. "
                    "Click \u2713 Done when it's right.")
        else:
            total = sum(e - s for s, e in ff.merge_sections(self.sections))
            text = (f"{n} section{'s' if n > 1 else ''}, {total:.2f} s in all. "
                    "Click one under the timeline to change it.")
        self.section_label.config(text=text)

    def _on_marker(self, which, t, final):
        """A loop marker on the timeline was dragged to t."""
        if not self.video_path:
            return
        try:
            start, end = self._range()
        except ValueError:
            start, end = 0.0, self.duration
        gap = 1.0 / self.video_fps  # keep the loop at least one frame long
        if which == "start":
            t = max(0.0, min(t, end - gap))
            self.start_text.set(ff.format_time(t))
        else:
            t = min(self.duration, max(t, start + gap))
            self.end_text.set(ff.format_time(t))
        if self.playing and self.play_clip:
            return  # adjusting a playing loop: keep playing
        if final:
            self._seek(t)
        else:
            self._on_drag(t)  # show the frame under the marker

    def _update_length(self):
        try:
            start, end = self._range()
        except ValueError:
            self.length_label.config(text="Length: ?", style="TLabel")
            return
        length = max(0.0, min(end, self.duration or end) - start)
        text = f"Length {length:.2f} s"
        over = length > MAX_GIF_SECONDS
        if over:
            text += f"  \u2013 over {MAX_GIF_SECONDS} s, so a GIF of it needs a higher speed"
        self.length_label.config(text=text, style="Error.TLabel" if over else "TLabel")

    # -------------------------------------------------------------- make
    def make_gif(self):
        """Make an animation (GIF, WebP or PNG) of the markers or of each section."""
        if self.busy:
            return
        if not self.video_path:
            messagebox.showinfo("GifMaker", "Open a video first.")
            return
        choices = []
        rng = self._loop_range_quiet()
        if rng:
            choices.append(("The loop markers", [rng], False))
        if self.sections:
            sections = sorted(self.sections)
            choices.append((f"Each section as its own file ({len(sections)})", sections, True))
        if not choices:
            self._valid_range()  # explains what's wrong with the markers
            return
        self.stop_playback()
        if self.fullscreen:  # so the window isn't hidden behind the video
            self.toggle_fullscreen()
        path = self.video_path

        def render(transform, start, end, dst, o):
            return ff.build_animation_command(
                self.ffmpeg, path, dst, start, end, o["fps"], o["width"], o["speed"], o["fmt"],
                o["direction"], o["colors"], o["loop"], pre_vf=ff.transform_filters(**transform))

        # The options start as they were last time; the length limit is checked there.
        ExportDialog(self, "gif", self.transform,
                     lambda transform, choice, rendered, options: self._save_animation(
                         render, choices[choice], transform, rendered, options),
                     choices=choices, options=self.anim_options, render=render,
                     formats=self.formats, max_seconds=MAX_GIF_SECONDS)

    def _loop_range_quiet(self):
        """The marker range if it's usable, else None (without complaining)."""
        try:
            start, end = self._range()
        except ValueError:
            return None
        end = min(end, self.duration)
        return (start, end) if end > start else None

    def _save_animation(self, render, choice, transform, rendered, options):
        self.transform = transform
        _, sections, separate = choice
        fmt, direction = options["fmt"], options["direction"]
        self.anim_options = options  # remembered for next time
        theme.save_setting("animation", options)
        ext, kind = ff.ANIMATION_FORMATS[fmt]
        base = os.path.splitext(os.path.basename(self.video_path))[0]
        if separate:
            default = f"{base}_section{ext}"
        else:
            default = f"{base}_{sections[0][0]:.1f}-{sections[0][1]:.1f}{ext}"
        dst = filedialog.asksaveasfilename(
            title=f"Save {kind} as" if not separate else f"Save {kind}s as (numbered)",
            defaultextension=ext, initialfile=default,
            initialdir=os.path.dirname(self.video_path), filetypes=[(kind, "*" + ext)])
        if not dst or self._is_source(dst):
            return
        what = "GIF" if fmt == "GIF" else fmt
        if rendered:  # the preview already made exactly this: just copy it
            shutil.copyfile(rendered, dst)
            self._done(None, [dst], what)
            return
        targets = self._numbered(dst, len(sections)) if separate else [dst]
        if targets is None:
            return
        jobs = [(render(transform, s, e, path, options),
                 ff.animation_seconds(s, e, options["speed"], direction), path)
                for (s, e), path in zip(sections, targets)]
        self._run(jobs, what)

    def _numbered(self, dst, count):
        """'clip.gif' -> ['clip 1.gif', 'clip 2.gif', ...], asking before overwriting."""
        stem, ext = os.path.splitext(dst)
        paths = [f"{stem} {i}{ext}" for i in range(1, count + 1)]
        existing = [p for p in paths if os.path.exists(p)]
        if existing and not messagebox.askyesno(
                "Replace files?",
                f"{len(existing)} of these files already exist, e.g. "
                f"{os.path.basename(existing[0])}. Replace them?"):
            return None
        if any(self._is_source(p) for p in paths):
            return None
        return paths

    # ------------------------------------------------------------- video
    def save_clip_video(self):
        """Save the sections (or the marker range if there are none) as a new video."""
        if self.busy:
            return
        if not self.video_path:
            messagebox.showinfo("GifMaker", "Open a video first.")
            return
        if self.sections:
            n = len(self.sections)
            choices = [("Joined into one video", ff.merge_sections(self.sections), False),
                       (f"Each section as its own video ({n})", sorted(self.sections), True)]
        else:
            rng = self._valid_range()
            if not rng:
                return
            choices = [("The loop markers", [rng], False)]
        self.stop_playback()
        if self.fullscreen:
            self.toggle_fullscreen()
        ExportDialog(self, "video", self.transform,
                     lambda transform, choice, *_: self._save_video(choices[choice], transform),
                     choices=choices, fps=min(self.video_fps, MAX_PLAYBACK_FPS),
                     sound=self.has_audio)

    def _save_video(self, choice, transform):
        self.transform = transform
        _, sections, separate = choice
        base = os.path.splitext(os.path.basename(self.video_path))[0]
        if separate:
            name = f"{base}_section.mp4"
        elif len(sections) == 1:
            name = f"{base}_{sections[0][0]:.1f}-{sections[0][1]:.1f}.mp4"
        else:
            name = f"{base}_edit.mp4"
        dst = filedialog.asksaveasfilename(
            title="Save videos as (numbered)" if separate else "Save video as",
            defaultextension=".mp4", initialfile=name,
            initialdir=os.path.dirname(self.video_path),
            filetypes=[("MP4 video", "*.mp4")])
        if not dst or self._is_source(dst):
            return
        vf = ff.transform_filters(**transform)
        if separate:
            targets = self._numbered(dst, len(sections))
            if targets is None:
                return
            jobs = [(ff.build_clip_command(self.ffmpeg, self.video_path, path, [sec],
                                           self.has_audio, pre_vf=vf), sec[1] - sec[0], path)
                    for sec, path in zip(sections, targets)]
        else:
            jobs = [(ff.build_clip_command(self.ffmpeg, self.video_path, dst, sections,
                                           self.has_audio, pre_vf=vf),
                     sum(e - s for s, e in sections), dst)]
        self._run(jobs, "Video")

    # -------------------------------------------------------------- save
    def _is_source(self, dst):
        """Refuse to write over the video being read (ffmpeg would destroy it)."""
        same = os.path.normcase(os.path.abspath(dst)) == os.path.normcase(
            os.path.abspath(self.video_path))
        if same:
            messagebox.showerror("GifMaker", "Choose a different file name: that's the video "
                                             "you're making it from.")
        return same

    def _run(self, jobs, what):
        """Run ffmpeg saves one after another in the background, with progress and Cancel.

        jobs is a list of (command, output seconds, output path).
        """
        self._set_busy(True)
        n = len(jobs)
        self.status.set(f"Saving {what if what != 'Video' else 'video'}...")

        def work():
            done = []
            err = None
            for k, (cmd, seconds, dst) in enumerate(jobs):
                if n > 1:
                    self.root.after(0, lambda k=k: self.status.set(f"Saving {k + 1} of {n}..."))

                def progress(frac, k=k):
                    self.root.after(0, lambda: self.progress.config(value=(k + frac) / n))

                err = ff.run_with_progress(cmd, seconds, progress, lambda: self.cancel_requested)
                if err:
                    break
                done.append(dst)
            self.root.after(0, lambda: self._done(err, done, what))

        threading.Thread(target=work, daemon=True).start()

    def cancel(self):
        self.cancel_requested = True
        self.status.set("Cancelling...")

    def _set_busy(self, busy):
        self.busy = busy
        self.cancel_requested = False
        for button in (self.make_btn, self.video_btn, self.snap_btn):
            button.config(state="disabled" if busy else "normal")
        self.progress.config(value=0)
        if busy:
            self.cancel_btn.pack(side="right")
            self.progress.pack(side="right", padx=6)
        else:
            self.cancel_btn.pack_forget()
            self.progress.pack_forget()

    def _done(self, err, saved, what):
        """Report how a save went; `saved` lists the files that were finished."""
        self._set_busy(False)
        if err == "cancelled":
            self.status.set("Cancelled." + (f" {len(saved)} file(s) were saved first." if saved else ""))
            return
        if err:
            self.status.set("Failed.")
            messagebox.showerror(f"{what} failed", err[-1500:])
            return
        size_mb = sum(os.path.getsize(p) for p in saved) / (1024 * 1024)
        if len(saved) == 1:
            self.status.set(f"Saved {saved[0]}  ({size_mb:.2f} MB)")
        else:
            self.status.set(f"Saved {len(saved)} files in {os.path.dirname(saved[0])}  "
                            f"({size_mb:.2f} MB in all)")
        if self.show_folder.get():
            show_in_folder(saved[0])


def asset(name):
    """Path of a file in the assets folder (bundled into the exe when built)."""
    base = getattr(sys, "_MEIPASS", os.path.join(os.path.dirname(__file__), os.pardir))
    return os.path.join(base, "assets", name)


def show_in_folder(path):
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except OSError:
        pass


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        from .selftest import run

        sys.exit(run(sys.argv[2]))
    video = sys.argv[1] if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]) else None
    if video and theme.load_setting("one_window", True) and single_instance.hand_over(video):
        return  # the GifMaker that's already open has it
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = None
    if TkinterDnD is not None:
        try:
            root = TkinterDnD.Tk()
        except (RuntimeError, tk.TclError):
            root = None
    if root is None:
        root = tk.Tk()
    theme.apply(root, theme.load_dark_setting())
    try:
        ffmpeg = ff.find_ffmpeg()
    except RuntimeError as e:
        root.withdraw()
        messagebox.showerror("GifMaker", str(e))
        return
    app = GifMakerApp(root, ffmpeg)
    if video:
        root.after(100, lambda: app.open_video(video))
    root.mainloop()
    app.player.close()


if __name__ == "__main__":
    main()
