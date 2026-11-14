"""A timeline editor for joining several videos and pictures into one video.

Clips sit one after another on a track at the bottom: drag one to move it, click it to
trim it with the green and red markers above, and save the lot as one MP4.
"""

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import ffmpeg_tools as ff
from . import theme
from . import ui_thread
from .player import Player
from .timeline import Timeline
from .tooltip import Tooltip

try:
    from tkinterdnd2 import COPY, DND_FILES
except ImportError:
    DND_FILES = None

MAX_PREVIEW_FPS = 30
PICTURE_SECONDS = 3.0  # how long a picture shows when it's added
TRACK_H = 86
BLOCK_TOP, BLOCK_H = 8, 62
MIN_BLOCK_W = 70
THUMB_W, THUMB_H = 96, 54
COLORS = ("#3b6ea8", "#8a4fa3", "#3d8c5c", "#a8673b", "#a83b55", "#3b8fa8")
SIZES = [("Like the first clip", None),
         ("1920 × 1080 (landscape)", (1920, 1080)),
         ("1280 × 720 (landscape)", (1280, 720)),
         ("1080 × 1920 (portrait)", (1080, 1920)),
         ("720 × 1280 (portrait)", (720, 1280)),
         ("1080 × 1080 (square)", (1080, 1080))]
FPS_CHOICES = ("24", "25", "30", "60")


class Clip:
    """One piece of the edit: a part of a video, or a picture shown for a while."""

    def __init__(self, path, info):
        self.path, self.info = path, info
        self.image = ff.is_image(path)
        self.start = 0.0
        self.end = PICTURE_SECONDS if self.image else info.duration
        self.sound = info.has_audio and not self.image
        self.thumb = None  # PhotoImage for the track

    @property
    def length(self):
        return self.end - self.start

    @property
    def name(self):
        return os.path.basename(self.path)


class SequenceEditor(tk.Toplevel):
    def __init__(self, app, open_types):
        super().__init__(app.root)
        self.app, self.ffmpeg, self.open_types = app, app.ffmpeg, open_types
        self.clips = []
        self.sel = None  # index of the clip being trimmed
        self.playing_all = None  # index being played by "Play all", else None
        self.picture_job = None
        self.position = 0.0
        self.resume_after_seek = None
        self.still_busy, self.still_want = False, None
        self.drag = None
        self.busy = False
        self.cancel_requested = False
        self.closed = False
        self.title("Timeline edit")
        self.geometry("1100x760")
        self.minsize(960, 560)
        theme.style_window(self)
        saved = theme.load_setting("seq_size", SIZES[0][0])
        self.size_name = tk.StringVar(value=saved if saved in dict(SIZES) else SIZES[0][0])
        self.fps = tk.StringVar(value=str(theme.load_setting("seq_fps", 30)))
        self.open_after = tk.BooleanVar(value=theme.load_setting("seq_open_after", True))
        self.status = tk.StringVar()
        self._build()
        self.player = Player(self, self.ffmpeg, self._on_frame, self._on_end)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._refresh()

    # --------------------------------------------------------------- UI
    def _build(self):
        main = ttk.Frame(self, padding=(10, 8, 10, 6))
        main.pack(fill="both", expand=True)

        bar = ttk.Frame(main)
        bar.pack(fill="x")
        add = ttk.Button(bar, text="Add...", command=self.add_files)
        add.pack(side="left")
        Tooltip(add, "Add videos or pictures to the end of the timeline (or drag them in "
                     "from Explorer)")
        current = ttk.Button(bar, text="Add open video", command=self.add_current)
        current.pack(side="left", padx=(4, 0))
        Tooltip(current, "Add the video open in the main window, trimmed to its markers "
                         "(or one clip per section, if it has sections)")
        self.remove_btn = ttk.Button(bar, text="Remove", command=self.remove_selected)
        self.remove_btn.pack(side="left", padx=(12, 0))
        Tooltip(self.remove_btn, "Take the chosen clip off the timeline  (Delete key)")
        self.left_btn = ttk.Button(bar, text="◀", width=3, command=lambda: self.move_selected(-1))
        self.left_btn.pack(side="left", padx=(4, 0))
        Tooltip(self.left_btn, "Move the chosen clip earlier (or drag it on the timeline)")
        self.right_btn = ttk.Button(bar, text="▶", width=3, command=lambda: self.move_selected(1))
        self.right_btn.pack(side="left", padx=(2, 0))
        Tooltip(self.right_btn, "Move the chosen clip later")
        self.dup_btn = ttk.Button(bar, text="Duplicate", command=self.duplicate_selected)
        self.dup_btn.pack(side="left", padx=(4, 0))
        Tooltip(self.dup_btn, "Put a copy of the chosen clip right after it (to use "
                              "another part of the same video)")

        self.save_btn = ttk.Button(bar, text="Save video...", command=self.save)
        self.save_btn.pack(side="right")
        Tooltip(self.save_btn, "Join the clips, in order and trimmed, into one MP4 with sound")
        fps = ttk.Combobox(bar, textvariable=self.fps, values=FPS_CHOICES, state="readonly",
                           width=4)
        fps.pack(side="right", padx=(4, 12))
        Tooltip(fps, "Frames per second of the saved video")
        ttk.Label(bar, text="FPS").pack(side="right")
        size = ttk.Combobox(bar, textvariable=self.size_name, values=[n for n, _ in SIZES],
                            state="readonly", width=24)
        size.pack(side="right", padx=(4, 12))
        Tooltip(size, "Picture size of the saved video. Clips of another shape get black "
                      "bars rather than being stretched.")
        ttk.Label(bar, text="Size").pack(side="right")

        # The preview of the chosen clip.
        self.canvas = tk.Canvas(main, bg="#111", highlightthickness=0, width=640, height=360)
        self.canvas.pack(fill="both", expand=True, pady=(8, 4))
        self.photo = tk.PhotoImage(master=self)
        self.canvas_img = self.canvas.create_image(0, 0, image=self.photo)
        self.canvas_text = self.canvas.create_text(
            0, 0, fill="#888", justify="center",
            text="Add videos or pictures, or drag them in from Explorer.\n\n"
                 "They play one after another, in the order on the timeline below.\n"
                 "Click a clip to trim it with the green and red markers; drag it to move it.")
        self.canvas.bind("<Configure>", self._canvas_resized)
        self.canvas.bind("<Button-1>", lambda e: self.toggle_play())

        # Trimming the chosen clip.
        trim = ttk.Frame(main)
        trim.pack(fill="x")
        self.timeline = Timeline(trim, on_seek=self._seek, on_seek_end=self._seek_end,
                                 on_marker=self._marker)
        self.timeline.pack(side="left", fill="x", expand=True)
        self.time_label = ttk.Label(trim, width=24, anchor="e")
        self.time_label.pack(side="left", padx=(6, 0), anchor="n", pady=(4, 0))

        controls = ttk.Frame(main)
        controls.pack(fill="x", pady=(2, 6))
        self.play_btn = ttk.Button(controls, text="▶ Play clip", width=12,
                                   command=self.toggle_play)
        self.play_btn.pack(side="left")
        Tooltip(self.play_btn, "Play the chosen clip from its start marker  (Space)")
        self.all_btn = ttk.Button(controls, text="▶ Play all", width=12,
                                  command=self.toggle_play_all)
        self.all_btn.pack(side="left", padx=(4, 0))
        Tooltip(self.all_btn, "Play the whole timeline, clip after clip")
        b = ttk.Button(controls, text="[", width=3, command=self.set_in)
        b.pack(side="left", padx=(12, 0))
        Tooltip(b, "Start the clip at the playhead  ( [ key )")
        b = ttk.Button(controls, text="]", width=3, command=self.set_out)
        b.pack(side="left", padx=(2, 0))
        Tooltip(b, "End the clip at the playhead  ( ] key )")
        self.sound_var = tk.BooleanVar()
        self.sound_check = ttk.Checkbutton(controls, text="Sound", variable=self.sound_var,
                                           command=self._sound_changed)
        self.sound_check.pack(side="left", padx=(12, 0))
        Tooltip(self.sound_check, "Keep this clip's sound in the saved video (silence if off)")
        self.pic_frame = ttk.Frame(controls)
        ttk.Label(self.pic_frame, text="Show for").pack(side="left")
        self.pic_seconds = tk.StringVar()
        spin = ttk.Spinbox(self.pic_frame, from_=0.5, to=60, increment=0.5, width=5,
                           textvariable=self.pic_seconds, command=self._picture_length)
        spin.pack(side="left", padx=4)
        spin.bind("<Return>", lambda e: self._picture_length())
        spin.bind("<FocusOut>", lambda e: self._picture_length())
        ttk.Label(self.pic_frame, text="seconds").pack(side="left")
        self.clip_label = ttk.Label(controls, style="Muted.TLabel")
        self.clip_label.pack(side="left", padx=(12, 0), fill="x", expand=True)

        # The track: every clip in order, as wide as it is long.
        track_box = ttk.Frame(main)
        track_box.pack(fill="x")
        self.track = tk.Canvas(track_box, height=TRACK_H, highlightthickness=0,
                               bg=theme.colors["lane"])
        self.track.pack(fill="x")
        scroll = ttk.Scrollbar(track_box, orient="horizontal", command=self.track.xview)
        scroll.pack(fill="x")
        self.track.config(xscrollcommand=scroll.set)
        self.track.bind("<Configure>", lambda e: self._draw_track())
        self.track.bind("<Button-1>", self._track_press)
        self.track.bind("<B1-Motion>", self._track_drag)
        self.track.bind("<ButtonRelease-1>", self._track_release)
        theme.on_change(lambda: self.winfo_exists() and (
            self.track.config(bg=theme.colors["lane"]), self._draw_track()))

        foot = ttk.Frame(main)
        foot.pack(fill="x", pady=(6, 0))
        self.total_label = ttk.Label(foot, text="")
        self.total_label.pack(side="left")
        ttk.Label(foot, textvariable=self.status, style="Muted.TLabel").pack(
            side="left", padx=(16, 0), fill="x", expand=True)
        after = ttk.Checkbutton(foot, text="Open in GifMaker after saving",
                                variable=self.open_after)
        after.pack(side="right")
        Tooltip(after, "Open the saved video in the main window, ready to make GIFs from it")
        self.cancel_btn = ttk.Button(foot, text="Cancel", command=self._cancel)
        self.progress = ttk.Progressbar(foot, length=160, maximum=1.0)

        self.bind("<space>", self._key(self.toggle_play))
        self.bind("<Delete>", self._key(self.remove_selected))
        self.bind("<bracketleft>", self._key(self.set_in))
        self.bind("<bracketright>", self._key(self.set_out))
        self.bind("<Escape>", lambda e: self.stop())
        self._enable_drop(self)

    def _key(self, fn):
        def handler(event):
            if isinstance(event.widget, (ttk.Entry, tk.Entry, ttk.Spinbox, ttk.Combobox)):
                return None
            fn()
            return "break"
        return handler

    def _canvas_resized(self, event):
        self.canvas.coords(self.canvas_img, event.width // 2, event.height // 2)
        self.canvas.coords(self.canvas_text, event.width // 2, event.height // 2)
        if self.sel is not None and not self.player.playing:
            self._show_still(self.position, exact=True)

    # ------------------------------------------------------------ clips
    def add_files(self):
        folder = os.path.dirname(self.clips[-1].path) if self.clips else (
            os.path.dirname(self.app.video_path) if self.app.video_path else None)
        paths = filedialog.askopenfilenames(parent=self, title="Add videos or pictures",
                                            filetypes=self.open_types, initialdir=folder)
        for path in paths:
            self.add(path)

    def add_current(self):
        app = self.app
        if not app.video_path:
            self.status.set("Nothing is open in the main window.")
            return
        if app.is_image:
            self.add(app.video_path)
            return
        if app.sections:  # each section becomes a clip
            for rng in sorted(app.sections):
                self.add(app.video_path, rng)
            return
        try:
            rng = app._range()
        except ValueError:
            rng = None
        self.add(app.video_path, rng)

    def add(self, path, rng=None, at=None):
        try:
            info = ff.probe(self.ffmpeg, path)
        except Exception as e:
            messagebox.showerror("Could not open it", f"{os.path.basename(path)}:\n{e}",
                                 parent=self)
            return None
        clip = Clip(path, info)
        if not clip.image and info.duration <= 0:
            messagebox.showerror("Could not open it", f"{clip.name} has no length.", parent=self)
            return None
        if rng and rng[1] > rng[0] and not clip.image:
            clip.start, clip.end = max(0.0, rng[0]), min(info.duration, rng[1])
        at = len(self.clips) if at is None else at
        self.clips.insert(at, clip)
        self._make_thumb(clip)
        self.select(at)
        return clip

    def remove_selected(self):
        if self.sel is None or self.busy:
            return
        self.stop()
        del self.clips[self.sel]
        self.select(min(self.sel, len(self.clips) - 1) if self.clips else None)

    def move_selected(self, step):
        if self.sel is None or self.busy:
            return
        self._move(self.sel, self.sel + step)

    def _move(self, i, j):
        j = max(0, min(len(self.clips) - 1, j))
        if i == j:
            return
        self.stop()
        self.clips.insert(j, self.clips.pop(i))
        self.sel = j
        self._refresh()

    def duplicate_selected(self):
        if self.sel is None or self.busy:
            return
        old = self.clips[self.sel]
        clip = Clip(old.path, old.info)
        clip.start, clip.end, clip.sound, clip.thumb = old.start, old.end, old.sound, old.thumb
        self.clips.insert(self.sel + 1, clip)
        self.select(self.sel + 1)

    def select(self, index):
        self.stop()
        self.sel = index
        clip = self.current
        if clip:
            self.position = clip.start
            self.timeline.set_duration(0.0 if clip.image else clip.info.duration)
            self.timeline.set_range(clip.start, clip.end)
            self.timeline.set_position(clip.start)
            self.sound_var.set(clip.sound)
            self.pic_seconds.set(f"{clip.length:g}")
            self._show_still(clip.start, exact=True)
        else:
            self.timeline.set_duration(0.0)
            self.photo.blank()
        self._refresh()

    @property
    def current(self):
        return self.clips[self.sel] if self.sel is not None and self.sel < len(self.clips) else None

    def _refresh(self):
        """Bring the labels, buttons and track up to date."""
        clip = self.current
        has = clip is not None
        self.canvas.itemconfigure(self.canvas_text, state="hidden" if self.clips else "normal")
        for w in (self.remove_btn, self.dup_btn, self.play_btn):
            w.config(state="normal" if has and not self.busy else "disabled")
        self.left_btn.config(state="normal" if has and self.sel > 0 else "disabled")
        self.right_btn.config(state="normal" if has and self.sel < len(self.clips) - 1
                              else "disabled")
        self.all_btn.config(state="normal" if self.clips else "disabled")
        self.save_btn.config(state="normal" if self.clips and not self.busy else "disabled")
        if has and clip.image:
            self.pic_frame.pack(side="left", padx=(12, 0), before=self.clip_label)
            self.sound_check.pack_forget()
            self.timeline.pack_forget()
        else:
            self.pic_frame.pack_forget()
            if not self.sound_check.winfo_ismapped():
                self.sound_check.pack(side="left", padx=(12, 0), before=self.clip_label)
            if not self.timeline.winfo_ismapped():
                self.timeline.pack(side="left", fill="x", expand=True, before=self.time_label)
            self.sound_check.config(state="normal" if has and clip.info.has_audio
                                    else "disabled")
        if has:
            what = "picture" if clip.image else (
                f"{ff.format_time(clip.start)} – {ff.format_time(clip.end)}")
            self.clip_label.config(text=f"Clip {self.sel + 1} of {len(self.clips)}: "
                                        f"{clip.name}  ({what})")
        else:
            self.clip_label.config(text="")
        total = sum(c.length for c in self.clips)
        self.total_label.config(text=f"{len(self.clips)} clip{'s' * (len(self.clips) != 1)}, "
                                     f"{ff.format_time(total)} in all" if self.clips else "")
        self._show_time()
        self._draw_track()

    def _show_time(self):
        clip = self.current
        if clip and not clip.image:
            self.time_label.config(text=f"{ff.format_time(self.position)} / "
                                        f"{ff.format_time(clip.info.duration)}")
        else:
            self.time_label.config(text="")

    # -------------------------------------------------------- trimming
    def _seek(self, t):
        if self.resume_after_seek is None:
            self.resume_after_seek = self.player.playing
        self.stop()
        self.position = t
        self.timeline.set_position(t)
        self._show_time()
        self._show_still(t, exact=False)

    def _seek_end(self):
        resume, self.resume_after_seek = self.resume_after_seek, None
        if resume:
            self.play_clip(self.position)
        else:
            self._show_still(self.position, exact=True)

    def _marker(self, which, t, final):
        clip = self.current
        if not clip or clip.image:
            return
        gap = 1.0 / (clip.info.fps or 30)
        if which == "start":
            clip.start = t = max(0.0, min(t, clip.end - gap))
        else:
            clip.end = t = min(clip.info.duration, max(t, clip.start + gap))
        self.timeline.set_range(clip.start, clip.end)
        if self.player.playing:
            self.player.end = clip.end
        else:
            self.position = t
            self.timeline.set_position(t)
            self._show_still(t, exact=final)
        if final:
            if which == "start":
                self._make_thumb(clip)
            self._refresh()
        else:
            self._show_time()

    def set_in(self):
        clip = self.current
        if clip and not clip.image:
            self._marker("start", self.position, True)

    def set_out(self):
        clip = self.current
        if clip and not clip.image:
            self._marker("end", self.position, True)

    def _sound_changed(self):
        clip = self.current
        if clip:
            clip.sound = self.sound_var.get()
            if self.player.playing and self.playing_all is None:
                self.play_clip(self.position)

    def _picture_length(self):
        clip = self.current
        if not clip or not clip.image:
            return
        try:
            seconds = float(self.pic_seconds.get().replace(",", "."))
        except ValueError:
            seconds = clip.length
        clip.end = clip.start + min(600.0, max(0.1, seconds))
        self.pic_seconds.set(f"{clip.length:g}")
        self._refresh()

    # ---------------------------------------------------------- preview
    def _frame_size(self, clip):
        self.canvas.update_idletasks()
        cw, ch = max(2, self.canvas.winfo_width()), max(2, self.canvas.winfo_height())
        return ff.fit_size(clip.info.width, clip.info.height, cw, ch, upscale=True)

    def _show_still(self, t, exact, clip=None):
        """Show clip's frame at t (the newest request wins; one grab at a time)."""
        clip = clip or self.current
        if not clip:
            return
        self.still_want = (clip, t, exact)
        if not self.still_busy:
            self._grab_next()

    def _grab_next(self):
        if self.closed or not self.still_want:
            return
        (clip, t, exact), self.still_want = self.still_want, None
        self.still_busy = True
        w, h = self._frame_size(clip)
        ffmpeg = self.ffmpeg

        def work():
            data = ff.grab_frame(ffmpeg, clip.path, 0 if clip.image else t, w, h,
                                 fast=not exact, pre_vf=f"scale={w}:{h}")
            ui_thread.call(lambda: self._still_done(data))

        threading.Thread(target=work, daemon=True).start()

    def _still_done(self, data):
        self.still_busy = False
        if self.closed:
            return
        if data and not self.player.playing:
            try:
                self.photo.configure(data=data, format="ppm")
            except tk.TclError:
                pass
        self._grab_next()

    def _on_frame(self, t, data):
        try:
            self.photo.configure(data=data, format="ppm")
        except tk.TclError:
            pass
        self.position = t
        self.timeline.set_position(t)
        self._show_time()

    def _on_end(self):
        if self.playing_all is not None:
            self._play_index(self.playing_all + 1)
        else:
            self._stopped()
            clip = self.current
            if clip:
                self.position = clip.start
                self.timeline.set_position(clip.start)
                self._show_time()

    def toggle_play(self):
        if self.player.playing or self.playing_all is not None:
            self.stop()
        else:
            self.play_clip()

    def play_clip(self, start=None):
        clip = self.current
        if not clip or clip.image:
            return
        self.stop()
        if start is None or not clip.start <= start < clip.end - 0.05:
            start = clip.start
        self._start_player(clip, start)
        self.play_btn.config(text="❚❚ Pause")

    def toggle_play_all(self):
        if self.playing_all is not None:
            self.stop()
        elif self.clips:
            self.stop()
            self._play_index(0)

    def _play_index(self, i):
        """Play clip i of the whole timeline, then the next."""
        if self.picture_job:
            self.after_cancel(self.picture_job)
            self.picture_job = None
        if i >= len(self.clips):
            self.stop()
            if self.clips:
                self.select(0)
            return
        self.playing_all = i
        self.all_btn.config(text="■ Stop")
        clip = self.clips[i]
        self.sel = i
        self.timeline.set_duration(0.0 if clip.image else clip.info.duration)
        self.timeline.set_range(clip.start, clip.end)
        self._refresh()
        if clip.image:
            self.player.stop()
            self._show_still(0, exact=True, clip=clip)
            self.picture_job = self.after(int(clip.length * 1000),
                                          lambda: self._play_index(i + 1))
        else:
            self._start_player(clip, clip.start)

    def _start_player(self, clip, start):
        self.player.volume, self.player.boost = self.app.gain_settings()
        self.player.start(clip.path, start, self._frame_size(clip),
                          min(clip.info.fps or 30, MAX_PREVIEW_FPS), end=clip.end,
                          sound=clip.sound and clip.info.has_audio)

    def stop(self):
        if self.picture_job:
            self.after_cancel(self.picture_job)
            self.picture_job = None
        self.player.stop()
        self._stopped()

    def _stopped(self):
        self.playing_all = None
        self.play_btn.config(text="▶ Play clip")
        self.all_btn.config(text="▶ Play all")

    # ------------------------------------------------------------ track
    def _layout(self):
        """[(x0, x1)] of each clip's block on the track."""
        width = max(200, self.track.winfo_width() - 12)
        total = sum(c.length for c in self.clips) or 1.0
        per_second = width / total
        spans, x = [], 6
        for clip in self.clips:
            w = max(MIN_BLOCK_W, clip.length * per_second)
            spans.append((x, x + w))
            x += w + 4
        return spans

    def _draw_track(self):
        t = self.track
        t.delete("all")
        spans = self._layout()
        fg = theme.colors["muted"]
        if not self.clips:
            t.create_text(10, TRACK_H // 2, anchor="w", fill=fg,
                          text="The timeline: clips play left to right.")
        for i, (clip, (x0, x1)) in enumerate(zip(self.clips, spans)):
            selected = i == self.sel
            t.create_rectangle(x0, BLOCK_TOP, x1, BLOCK_TOP + BLOCK_H,
                               fill=COLORS[i % len(COLORS)],
                               outline="#ffcc33" if selected else "#222",
                               width=3 if selected else 1)
            if clip.thumb and x1 - x0 >= clip.thumb.width() + 8:
                t.create_image(x0 + 4, BLOCK_TOP + 4, anchor="nw", image=clip.thumb)
                text_x = x0 + 8 + clip.thumb.width()
            else:
                text_x = x0 + 6
            if x1 - text_x > 50:
                label = f"{i + 1}. {clip.name}\n{ff.format_time(clip.length)}"
                if not clip.image and clip.info.has_audio and not clip.sound:
                    label += "  (muted)"
                t.create_text(text_x, BLOCK_TOP + 6, anchor="nw", fill="white",
                              width=x1 - text_x - 4, text=label)
        if self.drag and self.drag.get("moved"):
            x = self._insert_x(self.drag["target"], spans)
            t.create_line(x, 2, x, TRACK_H - 2, fill="#ffcc33", width=3)
        end = spans[-1][1] + 10 if spans else 0
        t.config(scrollregion=(0, 0, max(end, t.winfo_width()), TRACK_H))

    @staticmethod
    def _insert_x(target, spans):
        if not spans:
            return 6
        if target >= len(spans):
            return spans[-1][1] + 2
        return spans[target][0] - 2

    def _clip_at(self, x, spans):
        for i, (x0, x1) in enumerate(spans):
            if x0 <= x <= x1:
                return i
        return None

    def _target_at(self, x, spans):
        """Where a clip dropped at x goes: before the first clip whose middle is right of x."""
        for i, (x0, x1) in enumerate(spans):
            if x < (x0 + x1) / 2:
                return i
        return len(spans)

    def _track_press(self, event):
        x = self.track.canvasx(event.x)
        i = self._clip_at(x, self._layout())
        if i is None:
            return
        self.drag = {"index": i, "x": event.x, "moved": False, "target": i}

    def _track_drag(self, event):
        if not self.drag or self.busy:
            return
        if not self.drag["moved"] and abs(event.x - self.drag["x"]) < 6:
            return
        self.drag["moved"] = True
        self.drag["target"] = self._target_at(self.track.canvasx(event.x), self._layout())
        self.track.config(cursor="sb_h_double_arrow")
        self._draw_track()

    def _track_release(self, _event):
        drag, self.drag = self.drag, None
        self.track.config(cursor="")
        if not drag:
            return
        i = drag["index"]
        if drag["moved"]:
            target = drag["target"]
            self._move(i, target - 1 if target > i else target)
        elif i != self.sel:
            self.select(i)
        else:
            self._draw_track()

    def _make_thumb(self, clip):
        ffmpeg = self.ffmpeg
        t = 0 if clip.image else clip.start

        def work():
            data = ff.grab_frame(ffmpeg, clip.path, t, THUMB_W, THUMB_H, fast=True)

            def done():
                if self.closed or not data:
                    return
                try:
                    clip.thumb = tk.PhotoImage(master=self, data=data, format="ppm")
                except tk.TclError:
                    return
                self._draw_track()

            ui_thread.call(done)

        threading.Thread(target=work, daemon=True).start()

    # ----------------------------------------------------------- saving
    def _output_size(self):
        size = dict(SIZES).get(self.size_name.get())
        if size:
            return size
        first = self.clips[0].info
        w, h = first.width or 1280, first.height or 720
        if max(w, h) > 3840:  # keep huge pictures to a sensible video size
            w, h = ff.fit_size(w, h, 3840, 3840)
        return max(2, w // 2 * 2), max(2, h // 2 * 2)

    def save(self):
        if not self.clips or self.busy:
            return
        self.stop()
        theme.save_setting("seq_size", self.size_name.get())
        theme.save_setting("seq_fps", int(self.fps.get()))
        theme.save_setting("seq_open_after", self.open_after.get())
        first = self.clips[0]
        dst = filedialog.asksaveasfilename(
            parent=self, title="Save video as", defaultextension=".mp4",
            initialfile=f"{os.path.splitext(first.name)[0]}_timeline.mp4",
            initialdir=os.path.dirname(first.path), filetypes=[("MP4 video", "*.mp4")])
        if not dst:
            return
        key = os.path.normcase(os.path.abspath(dst))
        if any(os.path.normcase(os.path.abspath(c.path)) == key for c in self.clips):
            messagebox.showerror("Timeline edit", "Choose a different file name: that's one "
                                                  "of the clips.", parent=self)
            return
        w, h = self._output_size()
        fps = int(self.fps.get())
        clips = [dict(path=c.path, start=c.start, end=c.end, sound=c.sound,
                      has_audio=c.info.has_audio, image=c.image) for c in self.clips]
        cmd = ff.build_sequence_command(self.ffmpeg, clips, dst, w, h, fps)
        seconds = sum(c.length for c in self.clips)
        self._set_busy(True)
        self.status.set(f"Saving {w} × {h} video...")

        def work():
            err = ff.run_with_progress(
                cmd, seconds, lambda f: ui_thread.call(lambda: self.progress.config(value=f)),
                lambda: self.cancel_requested)
            ui_thread.call(lambda: self._saved(err, dst))

        threading.Thread(target=work, daemon=True).start()

    def _cancel(self):
        self.cancel_requested = True
        self.status.set("Cancelling...")

    def _set_busy(self, busy):
        self.busy = busy
        self.cancel_requested = False
        self.progress.config(value=0)
        if busy:
            self.cancel_btn.pack(side="right", padx=(6, 12))
            self.progress.pack(side="right")
        else:
            self.cancel_btn.pack_forget()
            self.progress.pack_forget()
        self._refresh()

    def _saved(self, err, dst):
        if self.closed:
            return
        self._set_busy(False)
        if err == "cancelled":
            self.status.set("Cancelled.")
            return
        if err:
            self.status.set("Failed.")
            messagebox.showerror("Saving failed", err[-1500:], parent=self)
            return
        size_mb = os.path.getsize(dst) / (1024 * 1024)
        self.status.set(f"Saved {os.path.basename(dst)}  ({size_mb:.2f} MB)")
        if self.open_after.get():
            self.app.open_saved(dst)

    # ------------------------------------------------------- drag & drop
    def _enable_drop(self, widget):
        if DND_FILES is None or not hasattr(widget, "drop_target_register"):
            return
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
        except tk.TclError:
            return

    def _on_drop(self, event):
        paths = [p for p in self.tk.splitlist(event.data) if os.path.isfile(p)]
        # Dropped on the track: put them where they were dropped.
        at = None
        tx, ty = self.track.winfo_rootx(), self.track.winfo_rooty()
        if ty <= event.y_root <= ty + self.track.winfo_height():
            at = self._target_at(self.track.canvasx(event.x_root - tx), self._layout())
        for k, path in enumerate(paths):
            self.after(10, lambda p=path, k=k: self.add(p, at=None if at is None else at + k))
        return COPY

    # ------------------------------------------------------------ close
    def uses(self, matches):
        """Drop the clips whose paths matches(path) picks (their files are being renamed)."""
        keep = [c for c in self.clips if not matches(c.path)]
        if len(keep) != len(self.clips):
            self.stop()
            self.clips = keep
            self.select(0 if keep else None)

    def close(self):
        if self.busy:
            if not messagebox.askyesno("Timeline edit", "Stop saving and close?", parent=self):
                return
            self.cancel_requested = True
        self.closed = True
        self.stop()
        self.player.close()
        self.app.sequence_window = None
        self.destroy()
