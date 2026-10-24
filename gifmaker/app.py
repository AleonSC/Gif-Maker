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
from operator import itemgetter
from tkinter import filedialog, font, messagebox, ttk

from . import ffmpeg_tools as ff
from . import files
from .export_dialog import ExportDialog
from .grid_player import GridPlayer
from .player import Player
from .timeline import Timeline

try:  # Drag and drop from Explorer needs the tkdnd extension; run without it if missing.
    from tkinterdnd2 import COPY, DND_FILES, TkinterDnD
except ImportError:
    TkinterDnD = None

PREVIEW_W, PREVIEW_H = 720, 405
MAX_GIF_SECONDS = 30
MAX_PLAYBACK_FPS = 30
PLAY_SPEEDS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0)
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
        tk.Label(self.tip, text=text, background="#ffffe0", relief="solid",
                 borderwidth=1, padx=6, pady=2).pack()

    def _hide(self, _event):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class GifMakerApp:
    def __init__(self, root, ffmpeg):
        self.root = root
        self.ffmpeg = ffmpeg
        root.title("GifMaker")
        root.minsize(760, 640)
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
        self.view_w, self.view_h = PREVIEW_W, PREVIEW_H  # size of the video area

        self.player = Player(root, ffmpeg, self._on_play_frame, self._on_play_end)
        self.play_clip = False  # looping the start..end range rather than playing on
        self.play_image = None

        self.position = tk.DoubleVar(value=0.0)
        self.start_text = tk.StringVar(value="0:00.000")
        self.end_text = tk.StringVar(value="0:00.000")
        self.fps = tk.IntVar(value=15)
        self.width = tk.IntVar(value=480)
        self.speed = tk.DoubleVar(value=1.0)
        self.colors = tk.IntVar(value=256)
        self.loop = tk.BooleanVar(value=True)
        self.formats = ff.available_formats(ffmpeg)
        self.anim_format = tk.StringVar(value="GIF")
        self.direction = tk.StringVar(value="Forward")
        self.show_folder = tk.BooleanVar(value=False)
        self.sound = tk.BooleanVar(value=True)
        self.play_speed = tk.StringVar(value="1\u00D7")
        self.volume = tk.DoubleVar(value=100)
        self.status = tk.StringVar(value="Open a video to get started.")

        self._build_ui()
        self._sections_changed()
        self._enable_drop()
        self._poll_frame_queue()

    def open_grid(self):
        """Open the grid player (starting with the current video), or bring it to the front."""
        if self.grid_window is not None:
            self.grid_window.lift()
            return
        self.stop_playback()
        self.grid_window = GridPlayer(self, VIDEO_TYPES)
        if self.video_path:
            self.grid_window.after(100, lambda: self.grid_window and self.grid_window.add(self.video_path))

    def close(self):
        # Stop the sound before Tk and Python shut down, or PortAudio can crash on exit.
        if self.grid_window is not None:
            self.grid_window.close()
        self.player.close()
        self.root.destroy()

    @property
    def playing(self):
        return self.player.playing

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        main = self.main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        top = ttk.Frame(main)
        top.pack(fill="x")
        ttk.Button(top, text="Open video...", command=self.open_video).pack(side="left")
        ttk.Button(top, text="Clear", command=self.clear_video).pack(side="left", padx=(6, 0))
        delete = ttk.Button(top, text="Delete file...", command=self.delete_file)
        delete.pack(side="left", padx=(6, 0))
        Tooltip(delete, "Delete this video and open the next one in the folder  (Delete key)")
        grid = ttk.Button(top, text="Grid player", command=self.open_grid)
        grid.pack(side="right")
        Tooltip(grid, "Play several videos at once, each in a panel you can move, resize and loop")
        self.file_label = ttk.Label(top, text="No video loaded", foreground="#666")
        self.file_label.pack(side="left", padx=10)

        view = ttk.Frame(main)
        view.pack(pady=8)
        nav_font = font.nametofont("TkDefaultFont").copy()
        nav_font.configure(size=20, weight="bold")
        ttk.Style(self.root).configure("Nav.TButton", font=nav_font, padding=(2, 0))
        self.back_btn = ttk.Button(view, text="\u2039", width=2, style="Nav.TButton",
                                   command=lambda: self.open_neighbour(-1))
        self.back_btn.pack(side="left", fill="y", padx=(0, 4))
        Tooltip(self.back_btn, lambda: self._neighbour_tip(-1))
        self.canvas = tk.Canvas(view, width=PREVIEW_W, height=PREVIEW_H,
                                bg="#111", highlightthickness=0)
        self.canvas.pack(side="left")
        self.canvas.bind("<Double-Button-1>", self.toggle_fullscreen)
        self.next_btn = ttk.Button(view, text="\u203A", width=2, style="Nav.TButton",
                                   command=lambda: self.open_neighbour(1))
        self.next_btn.pack(side="left", fill="y", padx=(4, 0))
        Tooltip(self.next_btn, lambda: self._neighbour_tip(1))
        self.canvas_text = self.canvas.create_text(
            PREVIEW_W // 2, PREVIEW_H // 2, fill="#888",
            text="Open a video, or drag and drop one here.")
        self.canvas_img = self.canvas.create_image(PREVIEW_W // 2, PREVIEW_H // 2)

        scrub = self.scrub_bar = ttk.Frame(main)
        scrub.pack(fill="x")
        self.timeline = Timeline(scrub, on_seek=self._on_drag, on_seek_end=self._settle,
                                 on_marker=self._on_marker, on_section=self._select_section)
        self.timeline.pack(side="left", fill="x", expand=True)
        self.position.trace_add("write", lambda *_: self.timeline.set_position(self.position.get()))
        self.pos_label = ttk.Label(scrub, text="0:00.000", width=12, anchor="e")
        self.pos_label.pack(side="left", padx=(8, 0))
        speed = ttk.Combobox(scrub, textvariable=self.play_speed, width=6, state="readonly",
                             values=[f"{v:g}\u00D7" for v in PLAY_SPEEDS])
        speed.pack(side="left", padx=(10, 0))
        speed.bind("<<ComboboxSelected>>", lambda e: self._restart_playback())
        Tooltip(speed, "Playback speed (sound keeps its pitch)")
        ttk.Label(scrub, text="Vol").pack(side="left", padx=(10, 2))
        vol = ttk.Scale(scrub, from_=0, to=100, variable=self.volume, length=90,
                        command=lambda v: setattr(self.player, "volume", float(v) / 100))
        vol.pack(side="left")
        Tooltip(vol, "Volume")

        step = self.play_bar = ttk.Frame(main)
        step.pack(pady=4)
        self.play_btn = ttk.Button(step, text="\u25B6 Play", width=9, command=self.toggle_play)
        self.play_btn.pack(side="left", padx=2)
        self.clip_btn = ttk.Button(step, text="\u21BB Play clip", width=11,
                                   command=self.toggle_play_clip)
        self.clip_btn.pack(side="left", padx=2)
        self.sound_check = ttk.Checkbutton(step, text="Sound", variable=self.sound,
                                           command=self._sound_toggled)
        self.sound_check.pack(side="left", padx=(4, 10))
        # Quick marking while watching: loop start, loop end, and keep as a section.
        for text, width, command, tip in (
                ("[", 3, self.set_start, "Set the loop start (green marker) to the playhead  ( [ key )"),
                ("]", 3, self.set_end, "Set the loop end (red marker) to the playhead  ( ] key )"),
                ("+ Section", 9, self._section_button,
                 "Keep the loop as a video section (finishes editing if you're changing one)")):
            button = ttk.Button(step, text=text, width=width, command=command)
            button.pack(side="left", padx=1)
            Tooltip(button, tip)
        ttk.Frame(step, width=10).pack(side="left")
        for label, delta in (("<< 1s", -1), ("< frame", "-f"), ("frame >", "+f"), ("1s >>", 1)):
            ttk.Button(step, text=label, width=7,
                       command=lambda d=delta: self.step(d)).pack(side="left", padx=1)
        ttk.Button(step, text="Save snapshot...",
                   command=self.save_snapshot).pack(side="left", padx=(12, 2))
        fs = ttk.Button(step, text="\u26F6", width=3, command=self.toggle_fullscreen)
        fs.pack(side="left", padx=(6, 0))
        Tooltip(fs, "Fullscreen (F11 or double-click the video; Esc to leave)")

        rng = ttk.LabelFrame(main, text="Clip range  (drag the green and red markers, "
                                        "or type seconds or m:ss.sss)", padding=6)
        rng.pack(fill="x", pady=6)
        ttk.Label(rng, text="Start").grid(row=0, column=0, **pad)
        ttk.Entry(rng, textvariable=self.start_text, width=12).grid(row=0, column=1, **pad)
        ttk.Button(rng, text="Set to current", command=self.set_start).grid(row=0, column=2, **pad)
        ttk.Button(rng, text="Go", width=4,
                   command=lambda: self.goto(self.start_text)).grid(row=0, column=3, **pad)
        ttk.Label(rng, text="End").grid(row=0, column=4, **pad)
        ttk.Entry(rng, textvariable=self.end_text, width=12).grid(row=0, column=5, **pad)
        ttk.Button(rng, text="Set to current", command=self.set_end).grid(row=0, column=6, **pad)
        ttk.Button(rng, text="Go", width=4,
                   command=lambda: self.goto(self.end_text)).grid(row=0, column=7, **pad)
        self.length_label = ttk.Label(rng, text="GIF length: 0.00s")
        self.length_label.grid(row=1, column=0, columnspan=9, sticky="w", **pad)
        sec = ttk.Frame(rng)
        sec.grid(row=2, column=0, columnspan=9, sticky="w", padx=8, pady=(2, 4))
        self.section_btn = ttk.Button(sec, text="Add section", width=13,
                                      command=self._section_button)
        self.section_btn.pack(side="left")
        self.remove_section_btn = ttk.Button(sec, text="Remove section",
                                             command=self.remove_section, state="disabled")
        self.remove_section_btn.pack(side="left", padx=4)
        ttk.Button(sec, text="Clear sections", command=self.clear_sections).pack(side="left")
        self.section_label = ttk.Label(sec, foreground="#555", wraplength=470, justify="left")
        self.section_label.pack(side="left", padx=(10, 0))
        for var in (self.start_text, self.end_text, self.speed):
            var.trace_add("write", lambda *_: self._range_changed())

        opts = ttk.LabelFrame(main, text="Animation options", padding=6)
        opts.pack(fill="x", pady=6)
        ttk.Label(opts, text="FPS").grid(row=0, column=0, **pad)
        ttk.Spinbox(opts, from_=1, to=50, textvariable=self.fps, width=6).grid(row=0, column=1, **pad)
        ttk.Label(opts, text="Width (px, 0 = original)").grid(row=0, column=2, **pad)
        ttk.Combobox(opts, textvariable=self.width, width=7,
                     values=(0, 240, 320, 480, 640, 800, 1080)).grid(row=0, column=3, **pad)
        ttk.Label(opts, text="Speed").grid(row=0, column=4, **pad)
        ttk.Combobox(opts, textvariable=self.speed, width=5,
                     values=(0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0)).grid(row=0, column=5, **pad)
        ttk.Label(opts, text="Colors").grid(row=0, column=6, **pad)
        self.colors_box = ttk.Combobox(opts, textvariable=self.colors, width=5,
                                       values=(256, 128, 64, 32, 16))
        self.colors_box.grid(row=0, column=7, **pad)
        ttk.Checkbutton(opts, text="Loop forever", variable=self.loop).grid(row=0, column=8, **pad)
        Tooltip(self.colors_box, "Colours in the palette (GIF only)")

        bottom = ttk.Frame(main)
        bottom.pack(fill="x", pady=(8, 0))
        self.make_btn = ttk.Button(bottom, text="Make GIF...", command=self.make_gif)
        self.make_btn.pack(side="left")
        self.video_btn = ttk.Button(bottom, text="Save video...",
                                    command=self.save_clip_video)
        self.video_btn.pack(side="left", padx=(6, 0))
        self.cancel_btn = ttk.Button(bottom, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        ttk.Checkbutton(bottom, text="Show folder after saving",
                        variable=self.show_folder).pack(side="right")
        self.progress = ttk.Progressbar(bottom, maximum=1.0)
        self.progress.pack(side="left", fill="x", expand=True, padx=6)
        status = ttk.Label(main, textvariable=self.status, foreground="#444")
        status.pack(fill="x", pady=(6, 0))
        # Hidden in fullscreen, which keeps just the video, timeline and play bar.
        self._windowed_only = [top, rng, opts, bottom, status]
        self._rows = [(w, w.pack_info()) for w in main.pack_slaves()]

        self.root.bind("<space>", lambda e: self._key_set(e, self.toggle_play))
        self.root.bind("<Left>", lambda e: self._key_step(e, "-f"))
        self.root.bind("<Right>", lambda e: self._key_step(e, "+f"))
        self.root.bind("<bracketleft>", lambda e: self._key_set(e, self.set_start))
        self.root.bind("<bracketright>", lambda e: self._key_set(e, self.set_end))
        self.root.bind("<Prior>", lambda e: self._key_set(e, lambda: self.open_neighbour(-1)))
        self.root.bind("<Next>", lambda e: self._key_set(e, lambda: self.open_neighbour(1)))
        self.root.bind("<Delete>", lambda e: self._key_set(e, self.delete_file))
        self.root.bind("<F11>", self.toggle_fullscreen)
        self.root.bind("<Escape>", lambda e: self.fullscreen and self.toggle_fullscreen())
        self._update_nav()

    # ------------------------------------------------------ drag & drop
    def _enable_drop(self):
        if not hasattr(self.root, "drop_target_register"):
            return

        def register(widget):
            widget.drop_target_register(DND_FILES)
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
        files = [f for f in self.root.tk.splitlist(event.data) if os.path.isfile(f)]
        if not files:
            return event.action
        if self.busy:
            self.status.set("Wait for the GIF to finish before opening another video.")
        else:
            # Open after the drop completes so Explorer isn't left waiting on dialogs.
            self.root.after(10, lambda: self.open_video(files[0]))
        return COPY

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
        sound = "" if info.has_audio else ", no sound"
        self.file_label.config(text=f"{os.path.basename(path)}   ({ff.format_time(duration)}, "
                                    f"{w}x{h}, {self.video_fps:g} fps{sound})", foreground="#000")
        self.timeline.set_duration(duration)
        self.video_token += 1
        self.shown_t = None
        self._build_thumbs()
        self.start_text.set(ff.format_time(0))
        self.end_text.set(ff.format_time(min(duration, 3.0)))
        if w and w < self.width.get():
            self.width.set(0)
        self.status.set("Scrub to the part you want, set Start and End, then click Make GIF.")
        self._update_nav()
        self._seek(0.0)

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

    # -------------------------------------------------------- fullscreen
    def toggle_fullscreen(self, _event=None):
        self.fullscreen = not self.fullscreen
        if self.fullscreen:
            for widget in self._windowed_only:
                widget.pack_forget()
            self.root.attributes("-fullscreen", True)
            self.root.update_idletasks()
            # The video gets the screen, less the timeline and play bar kept below it.
            w = self.root.winfo_screenwidth() - 2 * self.back_btn.winfo_reqwidth() - 40
            h = (self.root.winfo_screenheight() - self.scrub_bar.winfo_reqheight()
                 - self.play_bar.winfo_reqheight() - 50)
            self._resize_view(w, h)
        else:
            self.root.attributes("-fullscreen", False)
            for widget, _ in self._rows:  # re-pack every row in its original order
                widget.pack_forget()
            for widget, packing in self._rows:
                widget.pack(packing)
            self._resize_view(PREVIEW_W, PREVIEW_H)

    def _resize_view(self, w, h):
        self.view_w, self.view_h = w, h
        self.canvas.config(width=w, height=h)
        self.canvas.coords(self.canvas_img, w // 2, h // 2)
        self.canvas.coords(self.canvas_text, w // 2, h // 2)
        if not self.video_path:
            return
        if self.playing:  # carry on playing at the new size
            self._restart_playback()
        else:
            self.shown_t = None
            self._settle()

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
        """Size to decode frames at: fit the view (filling it when fullscreen)."""
        return ff.fit_size(self.video_width, self.video_height, self.view_w, self.view_h,
                           upscale=self.fullscreen)

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
        self.file_label.config(text="No video loaded", foreground="#666")
        self.sound_check.config(state="normal")
        self.position.set(0.0)
        self.pos_label.config(text=ff.format_time(0))
        self.start_text.set(ff.format_time(0))
        self.end_text.set(ff.format_time(0))
        self.timeline.set_duration(0)
        self.progress.config(value=0)
        self._update_nav()
        self.status.set("Open a video to get started.")

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
        self.pos_label.config(text=ff.format_time(t))
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
        self.pos_label.config(text=ff.format_time(t))
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
        self.player.volume = self.volume.get() / 100
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
        self.pos_label.config(text=ff.format_time(t))

    def _on_play_end(self):
        self._playback_stopped()
        if self.play_clip:
            self.toggle_play_clip()  # go round the loop again
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
        self.section_btn.config(text="Done editing" if self.section_sel is not None else "Add section")
        self.remove_section_btn.config(state="normal" if self.section_sel is not None else "disabled")
        if not n:
            text = ("To keep several parts of the video: set the markers, click Add section, "
                    "repeat. Save video then cuts out everything else.")
        elif self.section_sel is not None:
            text = (f"Editing section {self.section_sel + 1} of {n}: the markers now move it. "
                    "Click Done editing when it's right.")
        else:
            total = sum(e - s for s, e in ff.merge_sections(self.sections))
            text = (f"{n} section{'s' if n > 1 else ''}, {total:.2f} s in total: Save video keeps "
                    "only these. Click one in the strip under the timeline to change it.")
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
            speed = float(self.speed.get())
            if speed <= 0:
                raise ValueError
        except (ValueError, tk.TclError):
            self.length_label.config(text="Animation length: ?", foreground="")
            return
        direction = self.direction.get()
        length = ff.animation_seconds(start, end, speed, direction)
        text = f"Animation length: {length:.2f}s  (max {MAX_GIF_SECONDS}s)"
        notes = []
        if speed != 1.0:
            notes.append(f"{speed:g}x speed")
        if direction == "Boomerang":
            notes.append("there and back")
        if notes:
            text = f"Clip {end - start:.2f}s, {', '.join(notes)} \u2192 " + text
        over = length > MAX_GIF_SECONDS
        if over:
            text += "  \u2013 too long, shorten the clip or raise the speed"
        self.length_label.config(text=text, foreground="#c00" if over else "")

    # -------------------------------------------------------------- make
    def make_gif(self):
        """Make an animation (GIF, WebP or PNG) of the markers or of each section."""
        if self.busy:
            return
        if not self.video_path:
            messagebox.showinfo("GifMaker", "Open a video first.")
            return
        try:
            fps = int(self.fps.get())
            width = int(float(self.width.get()))
            speed = float(self.speed.get())
            colors = int(self.colors.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("GifMaker", "Check the animation options: they must be numbers.")
            return
        if not (1 <= fps <= 50 and width >= 0 and 0.1 <= speed <= 10 and 2 <= colors <= 256):
            messagebox.showerror(
                "GifMaker",
                "Options out of range:\nFPS 1-50, Width 0 or more, Speed 0.1-10, Colors 2-256.")
            return
        # Format and direction are picked in the popup; start it on the last ones used.
        fmt, direction = self.anim_format.get(), self.direction.get()
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
        # Every animation has to fit the length limit (checked for the chosen direction
        # when saving; here, leave out what can't fit even played once forwards).
        fits = [c for c in choices
                if all(ff.animation_seconds(*p, speed, "Forward") <= MAX_GIF_SECONDS for p in c[1])]
        if fits and len(fits) < len(choices):
            self.status.set(f"Some of what you marked is over {MAX_GIF_SECONDS} s, so it isn't "
                            "offered: " + ", ".join(c[0] for c in choices if c not in fits) + ".")
        choices = fits
        if not choices:
            messagebox.showerror(
                "Too long",
                f"Animations can be at most {MAX_GIF_SECONDS} seconds long (a boomerang counts "
                "twice). Shorten the clip or sections, or raise the speed.")
            return
        self.stop_playback()
        settings = dict(fps=fps, width=width, speed=speed, colors=colors, loop=self.loop.get())

        def render(transform, start, end, dst, fmt, direction):
            return ff.build_animation_command(
                self.ffmpeg, self.video_path, dst, start, end, fps, width, speed, fmt,
                direction, colors, settings["loop"], pre_vf=ff.transform_filters(**transform))

        ExportDialog(self, "gif", self.transform,
                     lambda transform, choice, rendered, options: self._save_animation(
                         render, settings, choices[choice], transform, rendered, options),
                     choices=choices, fps=fps, width=width, speed=speed, fmt=fmt,
                     direction=direction, render=render, formats=self.formats,
                     max_seconds=MAX_GIF_SECONDS)

    def _loop_range_quiet(self):
        """The marker range if it's usable, else None (without complaining)."""
        try:
            start, end = self._range()
        except ValueError:
            return None
        end = min(end, self.duration)
        return (start, end) if end > start else None

    def _save_animation(self, render, settings, choice, transform, rendered, options):
        self.transform = transform
        _, sections, separate = choice
        fmt, direction = options["fmt"], options["direction"]
        self.anim_format.set(fmt)  # remembered for next time
        self.direction.set(direction)
        self._update_length()
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
        jobs = [(render(transform, s, e, path, fmt, direction),
                 ff.animation_seconds(s, e, settings["speed"], direction), path)
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
        self.make_btn.config(state="disabled" if busy else "normal")
        self.video_btn.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")
        self.progress.config(value=0)

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
        self.progress.config(value=1.0)
        size_mb = sum(os.path.getsize(p) for p in saved) / (1024 * 1024)
        if len(saved) == 1:
            self.status.set(f"Saved {saved[0]}  ({size_mb:.2f} MB)")
        else:
            self.status.set(f"Saved {len(saved)} files in {os.path.dirname(saved[0])}  "
                            f"({size_mb:.2f} MB in all)")
        if self.show_folder.get():
            show_in_folder(saved[0])


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
    try:
        ttk.Style(root).theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    try:
        ffmpeg = ff.find_ffmpeg()
    except RuntimeError as e:
        root.withdraw()
        messagebox.showerror("GifMaker", str(e))
        return
    app = GifMakerApp(root, ffmpeg)
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        root.after(100, lambda: app.open_video(sys.argv[1]))
    root.mainloop()
    app.player.close()


if __name__ == "__main__":
    main()
