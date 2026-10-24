"""A window for playing several videos at once, each in a panel you can move and resize."""

import math
import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import ffmpeg_tools as ff
from . import theme
from .player import Player
from .timeline import Timeline

try:
    from tkinterdnd2 import COPY, DND_FILES
except ImportError:
    DND_FILES = None

MIN_W, MIN_H = 220, 170  # smallest panel
SNAP = 20  # grid size when "Snap to grid" is on
RESIZE_SETTLE_MS = 150  # re-decode at the new size once resizing pauses this long
MAX_FPS = 30
DARK, MID, TEXT = "#202020", "#3a3a3a", "#e8e8e8"
RESIZE_CURSOR = "size_nw_se" if sys.platform == "win32" else "bottom_right_corner"


class GridPlayer(tk.Toplevel):
    """Plays any number of videos side by side. Each panel can be dragged and
    resized freely, and loops its own section (set with its green/red markers)."""

    def __init__(self, app, video_types):
        super().__init__(app.root)
        self.app = app
        self.video_types = video_types
        self.tiles = []
        self.snap = tk.BooleanVar(value=False)
        self.title("Grid player")
        self.geometry("1200x800")
        self.minsize(520, 400)
        theme.style_window(self)

        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="Add videos...", command=self.add_videos).pack(side="left")
        ttk.Button(bar, text="Arrange", command=self.arrange).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="▶ Play all", command=lambda: self._all("play")).pack(side="left", padx=(16, 0))
        ttk.Button(bar, text="❚❚ Pause all", command=lambda: self._all("pause")).pack(side="left", padx=4)
        ttk.Button(bar, text="Remove all", command=self.remove_all).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(bar, text="Snap to grid", variable=self.snap).pack(side="left", padx=(16, 0))
        ttk.Label(bar, style="Muted.TLabel", text="Drag to move \u00B7 corner to resize \u00B7 click "
                  "to play/pause \u00B7 green/red markers set the loop").pack(side="left", padx=(16, 0))

        self.area = tk.Frame(self, bg=DARK)
        self.area.pack(fill="both", expand=True)
        self.hint = tk.Label(self.area, bg=DARK, fg="#888",
                             text="Add videos, or drag them in from Explorer.")
        self.hint.place(relx=0.5, rely=0.5, anchor="center")
        self._enable_drop(self.area)
        self.protocol("WM_DELETE_WINDOW", self.close)

    # ------------------------------------------------------------ videos
    def add_videos(self):
        paths = filedialog.askopenfilenames(
            parent=self, title="Add videos", filetypes=self.video_types,
            initialdir=os.path.dirname(self.app.video_path) if self.app.video_path else None)
        for path in paths:
            self.add(path)

    def add(self, path, x=None, y=None):
        try:
            info = ff.probe(self.app.ffmpeg, path)
        except Exception as e:
            messagebox.showerror("Could not open video", f"{os.path.basename(path)}:\n{e}", parent=self)
            return
        self.update_idletasks()
        area_w, area_h = max(self.area.winfo_width(), 600), max(self.area.winfo_height(), 400)
        # Start a third of the window wide (less for tall videos), keeping the video's shape.
        pic_w, pic_h = ff.fit_size(info.width or 16, info.height or 9,
                                   area_w // 3, int(area_h * 0.6) - Tile.CHROME, upscale=True)
        w, h = max(MIN_W, pic_w), max(MIN_H, pic_h + Tile.CHROME)
        if x is None:  # cascade new panels so they don't sit exactly on top of each other
            k = len(self.tiles) % 8
            x, y = 20 + 30 * k, 20 + 30 * k
        x, y = min(max(0, x), area_w - 60), min(max(0, y), area_h - 40)
        tile = Tile(self, path, info, self._snapped(x), self._snapped(y), w, h)
        self.tiles.append(tile)
        self._enable_drop(tile)
        self.hint.place_forget()

    def remove(self, tile):
        tile.close()
        self.tiles.remove(tile)
        if not self.tiles:
            self.hint.place(relx=0.5, rely=0.5, anchor="center")

    def remove_all(self):
        for tile in list(self.tiles):
            self.remove(tile)

    def arrange(self):
        """Tile the videos in a neat grid that fills the window."""
        n = len(self.tiles)
        if not n:
            return
        area_w, area_h = self.area.winfo_width(), self.area.winfo_height()
        cols = math.ceil(math.sqrt(n * area_w / area_h * 9 / 16)) if area_h else 1
        cols = max(1, min(n, cols))
        rows = math.ceil(n / cols)
        gap = 6
        cell_w, cell_h = (area_w - gap) // cols, (area_h - gap) // rows
        for i, tile in enumerate(self.tiles):
            r, c = divmod(i, cols)
            tile.set_geometry(gap + c * cell_w, gap + r * cell_h,
                              max(MIN_W, cell_w - gap), max(MIN_H, cell_h - gap))

    def _all(self, action):
        for tile in self.tiles:
            tile.play() if action == "play" else tile.pause()

    def _snapped(self, value):
        return round(value / SNAP) * SNAP if self.snap.get() else value

    # ------------------------------------------------------- drag & drop
    def _enable_drop(self, widget):
        if DND_FILES is None or not hasattr(widget, "drop_target_register"):
            return
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
        except tk.TclError:
            return
        for child in widget.winfo_children():
            self._enable_drop(child)

    def _on_drop(self, event):
        x = event.x_root - self.area.winfo_rootx()
        y = event.y_root - self.area.winfo_rooty()
        paths = [p for p in self.tk.splitlist(event.data) if os.path.isfile(p)]
        for i, path in enumerate(paths):
            self.after(10, lambda p=path, i=i: self.add(p, x + 30 * i, y + 30 * i))
        return COPY

    def close(self):
        for tile in self.tiles:
            tile.close()
        self.tiles = []
        self.app.grid_window = None
        self.destroy()


class Tile(tk.Frame):
    """One video in the grid: a title bar, the picture, and a loop timeline."""

    CHROME = 26 + 42  # height of the title bar and timeline around the picture

    def __init__(self, grid, path, info, x, y, w, h):
        super().__init__(grid.area, bg=MID, highlightthickness=1, highlightbackground="#555")
        self.owner = grid
        self.ffmpeg = grid.app.ffmpeg
        self.path, self.info = path, info
        self.loop_start, self.loop_end = 0.0, info.duration
        self.position = 0.0
        self.sound = False
        self.resume_after_seek = None
        self.drag = None
        self.resize_job = None
        self.still_busy, self.still_want, self.still_token = False, None, 0
        self.closed = False

        header = tk.Frame(self, bg=MID)
        header.pack(fill="x")
        button = dict(bg=MID, fg=TEXT, activebackground="#555", activeforeground="white",
                      relief="flat", bd=0, padx=6, pady=2, highlightthickness=0)
        tk.Button(header, text="✕", command=lambda: grid.remove(self), **button).pack(side="right")
        self.sound_btn = tk.Button(header, text="Sound: off", command=self.toggle_sound, **button)
        if info.has_audio:
            self.sound_btn.pack(side="right")
        self.play_btn = tk.Button(header, text="▶", width=2, command=self.toggle, **button)
        self.play_btn.pack(side="left")
        self.name = tk.Label(header, text=os.path.basename(path), bg=MID, fg=TEXT, anchor="w")
        self.name.pack(side="left", fill="x", expand=True)
        for widget in (header, self.name):
            widget.bind("<Button-1>", self._move_start)
            widget.bind("<B1-Motion>", self._move)
            widget.bind("<ButtonRelease-1>", self._move_end)

        # The timeline is packed before the picture so a small panel never squeezes it
        # out, and the picture asks for no space of its own: it takes what's left.
        self.timeline = Timeline(self, on_seek=self._seek, on_seek_end=self._seek_end,
                                 on_marker=self._marker)
        self.timeline.pack(side="bottom", fill="x", padx=(0, 14))  # room for the resize corner
        self.timeline.set_duration(info.duration)
        self.timeline.set_range(self.loop_start, self.loop_end)
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0, cursor="fleur",
                                width=10, height=10)
        self.canvas.pack(fill="both", expand=True)
        self.photo = tk.PhotoImage(master=self)
        self.image = self.canvas.create_image(0, 0, image=self.photo)
        self.canvas.bind("<Configure>", lambda e: self.canvas.coords(self.image, e.width // 2, e.height // 2))
        self.canvas.bind("<Button-1>", self._move_start)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._click_or_move_end)

        grip = tk.Label(self, text="◢", bg=MID, fg="#aaa", cursor=RESIZE_CURSOR)
        grip.place(relx=1.0, rely=1.0, anchor="se")
        grip.bind("<Button-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize)
        grip.bind("<ButtonRelease-1>", self._resize_end)

        self.player = Player(self, self.ffmpeg, self._on_frame, self._on_end)
        self.place(x=x, y=y, width=w, height=h)
        self.lift()
        self.after(50, lambda: self._request_still(0.0, exact=True))

    # ---------------------------------------------------------- playback
    @property
    def playing(self):
        return self.player.playing

    def _frame_size(self):
        self.canvas.update_idletasks()
        cw, ch = max(2, self.canvas.winfo_width()), max(2, self.canvas.winfo_height())
        return ff.fit_size(self.info.width, self.info.height, cw, ch, upscale=True)

    def play(self, start=None):
        if self.closed:
            return
        if start is None:  # carry on from the playhead if it's inside the loop
            start = self.position
            if not self.loop_start <= start < self.loop_end - 0.05:
                start = self.loop_start
        self.player.volume, self.player.boost = self.owner.app.gain_settings()
        self.player.start(self.path, start, self._frame_size(), min(self.info.fps or 30, MAX_FPS),
                          end=self.loop_end, sound=self.info.has_audio and self.sound)
        self.play_btn.config(text="❚❚")

    def pause(self):
        self.player.stop()
        self.play_btn.config(text="▶")

    def toggle(self):
        self.pause() if self.playing else self.play()

    def toggle_sound(self):
        self.sound = not self.sound
        self.sound_btn.config(text="Sound: on" if self.sound else "Sound: off")
        if self.playing:  # pick up the change straight away
            self.pause()
            self.play(self.position)

    def _on_frame(self, t, data):
        try:
            self.photo.configure(data=data, format="ppm")
        except tk.TclError:
            pass
        self.position = t
        self.timeline.set_position(t)

    def _on_end(self):
        self.play(self.loop_start)  # round the loop again

    # ------------------------------------------------------ still frames
    def _request_still(self, t, exact):
        """Show the frame at t; only one grab at a time, the newest request wins."""
        self.still_want = (t, exact)
        if not self.still_busy:
            self._grab_next()

    def _grab_next(self):
        if self.closed or not self.still_want:
            return
        (t, exact), self.still_want = self.still_want, None
        self.still_busy = True
        w, h = self._frame_size()
        path, ffmpeg = self.path, self.ffmpeg

        def work():
            data = ff.grab_frame(ffmpeg, path, t, w, h, fast=not exact, pre_vf=f"scale={w}:{h}")
            try:
                self.after(0, lambda: self._still_done(data))
            except (RuntimeError, tk.TclError):
                pass

        threading.Thread(target=work, daemon=True).start()

    def _still_done(self, data):
        self.still_busy = False
        if self.closed:
            return
        if data and not self.playing:
            self._on_frame(self.position, data)
        self._grab_next()

    # ---------------------------------------------------------- timeline
    def _seek(self, t):
        if self.resume_after_seek is None:
            self.resume_after_seek = self.playing
        self.pause()
        self.position = t
        self.timeline.set_position(t)
        self._request_still(t, exact=False)

    def _seek_end(self):
        resume, self.resume_after_seek = self.resume_after_seek, None
        if resume:
            self.play(self.position)
        else:
            self._request_still(self.position, exact=True)

    def _marker(self, which, t, final):
        gap = 1.0 / (self.info.fps or 30)  # keep the loop at least a frame long
        if which == "start":
            self.loop_start = t = max(0.0, min(t, self.loop_end - gap))
        else:
            self.loop_end = t = min(self.info.duration, max(t, self.loop_start + gap))
        self.timeline.set_range(self.loop_start, self.loop_end)
        if self.playing:
            self.player.end = self.loop_end  # the loop changes straight away
            return
        self.position = t
        self.timeline.set_position(t)
        self._request_still(t, exact=final)

    # ------------------------------------------------- moving & resizing
    def set_geometry(self, x, y, w, h):
        self.place_configure(x=x, y=y, width=w, height=h)
        self._resized()

    def _move_start(self, event):
        self.lift()
        self.drag = (event.x_root, event.y_root, self.winfo_x(), self.winfo_y(), False)

    def _move(self, event):
        if not self.drag:
            return
        x0, y0, wx, wy, moved = self.drag
        dx, dy = event.x_root - x0, event.y_root - y0
        if not moved and abs(dx) < 4 and abs(dy) < 4:
            return  # still a click, not a drag
        self.drag = (x0, y0, wx, wy, True)
        area = self.owner.area
        # Keep the title bar on screen so the panel can always be grabbed again.
        x = min(max(-self.winfo_width() + 60, wx + dx), area.winfo_width() - 60)
        y = min(max(0, wy + dy), area.winfo_height() - 30)
        snap = self.owner._snapped
        self.place_configure(x=snap(x), y=snap(y))

    def _move_end(self, _event):
        self.drag = None

    def _click_or_move_end(self, _event):
        moved = self.drag and self.drag[4]
        self.drag = None
        if not moved:
            self.toggle()

    def _resize_start(self, event):
        self.lift()
        self.drag = (event.x_root, event.y_root, self.winfo_width(), self.winfo_height(), True)

    def _resize(self, event):
        if not self.drag:
            return
        x0, y0, w0, h0, _ = self.drag
        snap = self.owner._snapped
        w = max(MIN_W, snap(w0 + event.x_root - x0))
        h = max(MIN_H, snap(h0 + event.y_root - y0))
        self.place_configure(width=w, height=h)
        if self.resize_job:
            self.after_cancel(self.resize_job)
        self.resize_job = self.after(RESIZE_SETTLE_MS, self._resized)

    def _resize_end(self, _event):
        self.drag = None
        self._resized()

    def _resized(self):
        """Decode at the new size so the picture stays sharp."""
        if self.resize_job:
            self.after_cancel(self.resize_job)
            self.resize_job = None
        if self.closed:
            return
        if self.playing:
            self.pause()
            self.play(self.position)
        else:
            self._request_still(self.position, exact=True)

    def close(self):
        self.closed = True
        self.player.close()
        self.destroy()
