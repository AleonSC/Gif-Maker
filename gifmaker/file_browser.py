"""The side panel that shows the videos and pictures in the current folder as thumbnails.

Only the thumbnails in view (and a couple of rows either side) are drawn and
made, so a folder of thousands of files opens at once and doesn't keep ffmpeg
busy in the background. Thumbnails are a frame a few seconds in, kept as PNG in
memory and on disk (%LOCALAPPDATA%\\GifMaker\\thumbs) so they're instant next
time, and shown at the size picked with the slider: the grid fits as many per
row as the panel's width allows.
"""

import collections
import hashlib
import os
import subprocess
import threading
import tkinter as tk
from tkinter import font, ttk

from . import ffmpeg_tools as ff
from . import theme
from . import ui_thread
from .tooltip import Tooltip

BASE_W, BASE_H = 320, 180  # thumbnails are made at this size and scaled down
MIN_SIZE, MAX_SIZE = 80, 320  # thumbnail widths the slider allows
GAP = 8  # pixels between thumbnails
GRAB_AT = 5.0  # seconds into the video for the thumbnail (or the start if shorter)
MEMORY_CACHE = 1500  # thumbnails kept in memory (as PNG, ~40 KB each)
DISK_CACHE = 5000  # thumbnails kept on disk
SHOWN_CACHE = 300  # thumbnail pictures kept ready to draw (Windows limits graphics objects)
MARGIN_ROWS = 2  # rows drawn (and made) above and below the ones in view
WORKERS = 2  # thumbnails made at once


def _thumb_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "GifMaker", "thumbs")


def _key(path):
    """Changes when the file does, so an edited video gets a new thumbnail."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    text = f"{os.path.normcase(os.path.abspath(path))}|{st.st_size}|{st.st_mtime_ns}"
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _trim_disk_cache():
    """Keep the newest DISK_CACHE thumbnails on disk."""
    try:
        entries = [e for e in os.scandir(_thumb_dir()) if e.name.endswith(".png")]
    except OSError:
        return
    if len(entries) > DISK_CACHE:
        entries.sort(key=lambda e: e.stat().st_mtime)
        for e in entries[:len(entries) - DISK_CACHE]:
            try:
                os.remove(e.path)
            except OSError:
                pass


def _ratio(scale):
    """Small whole numbers (zoom, subsample) whose ratio is closest to scale."""
    return min(((a, b) for b in range(1, 13) for a in range(1, 2 * b + 1)),
               key=lambda ab: (abs(ab[0] / ab[1] - scale), ab[1]))


class FileBrowser(ttk.Frame):
    """on_open(path) is called when a thumbnail is clicked; on_close() by ✕."""

    def __init__(self, master, ffmpeg, on_open, on_close):
        super().__init__(master, padding=(6, 0, 0, 0))
        self.ffmpeg, self.on_open, self.on_close = ffmpeg, on_open, on_close
        self.size = tk.IntVar(value=int(theme.load_setting("thumb_size", 160)))
        # A plain copy for the worker threads: asking a Tk variable from another
        # thread waits for the window's thread, which may be waiting for the lock.
        self.size_now = self.size.get()
        self.paths = []
        self.index = {}  # path -> its place in paths
        self.current = None
        self.pngs = collections.OrderedDict()  # key -> PNG bytes at BASE size (newest last)
        self.images = collections.OrderedDict()  # path -> PhotoImage at the current size
        self.smooth_done = set()  # paths whose image is the smooth one (not a quick scale)
        self.drawn = set()  # indexes of the cells on the canvas
        self.grid = (1, 0, 0, 0, 0)  # columns, left margin, w, h, row height
        self.token = 0  # bumped when the folder or size changes: stale work is dropped
        self.want = []  # paths to make thumbnails for, most wanted first
        self.lock = threading.Condition()
        self.closed = False
        self.relayout_job = None
        self.view_job = None
        self.view_at = None  # the view's position when the cells were last drawn
        self.name_font = font.nametofont("TkDefaultFont").copy()
        self.name_font.configure(size=max(7, self.name_font.cget("size") - 1))
        self.names = {}  # (name, width) -> the name shortened to fit
        self._build()
        theme.on_change(self._recolor)
        for _ in range(WORKERS):
            threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=_trim_disk_cache, daemon=True).start()

    def _build(self):
        head = ttk.Frame(self)
        head.pack(fill="x", pady=(0, 4))
        self.title = ttk.Label(head, style="Head.TLabel", width=1)
        self.title.pack(side="left", fill="x", expand=True)
        close = ttk.Button(head, text="\u2715", width=3, command=self.on_close)
        close.pack(side="right")
        Tooltip(close, "Hide the files  (F3)")
        zoom = ttk.Scale(head, from_=MIN_SIZE, to=MAX_SIZE, variable=self.size, length=90,
                         command=lambda v: self._size_changed())
        zoom.pack(side="right", padx=(6, 6))
        Tooltip(zoom, "Thumbnail size (or Ctrl + mouse wheel over them)")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, width=BASE_W, highlightthickness=0, takefocus=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        # Whenever the view moves, draw what's come into it.
        self.canvas.configure(yscrollcommand=lambda a, b: (scroll.set(a, b), self._view_soon(a, b)))
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.canvas.bind("<Configure>", lambda e: self._relayout_soon())
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Leave>", lambda e: self.canvas.delete("hover"))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(seq, self._wheel)
        self.hover_name = None
        self._recolor()

    # ------------------------------------------------------------ content
    def show(self, paths, current):
        """Show these files (in order), marking current. Cheap if nothing changed."""
        changed = paths != self.paths
        self.paths = list(paths)
        self.current = current
        folder = os.path.dirname(current or (paths[0] if paths else "")) if (current or paths) else ""
        self.title.config(text=os.path.basename(folder) or folder or "No folder")
        if changed:
            # Thumbnails already made are kept (a deleted or added file shouldn't
            # make the rest blank while they're redone); only missing ones are made.
            self.index = {p: i for i, p in enumerate(self.paths)}
            for p in [p for p in self.images if p not in self.index]:
                del self.images[p]
            self._layout()
        else:
            self._mark_current()
        self._scroll_to_current()

    def refresh(self, path):
        """Make path's thumbnail again (the file has changed)."""
        if path in self.index:
            self.images.pop(path, None)
            self.smooth_done.discard(path)
            self._update_view()

    # --------------------------------------------------- making thumbnails
    def _worker(self):
        while True:
            with self.lock:
                while not self.want and not self.closed:
                    self.lock.wait()
                if self.closed:
                    return
                path = self.want.pop(0)
                token, size = self.token, self.size_now
            try:
                self._make_thumb(token, path, size)
            except Exception:
                pass  # a broken file just gets no thumbnail

    def _make_thumb(self, token, path, size):
        key = _key(path)
        png = self.pngs.get(key) if key else None
        disk = os.path.join(_thumb_dir(), f"{key}.png") if key else None
        if png is None and disk and os.path.exists(disk):
            try:
                with open(disk, "rb") as f:
                    png = f.read()
            except OSError:
                png = None
        if png is None:
            png = None if ff.is_image(path) else ff.grab_frame(
                self.ffmpeg, path, GRAB_AT, BASE_W, BASE_H, fast=True, codec="png")
            png = png or ff.grab_frame(self.ffmpeg, path, 0, BASE_W, BASE_H, fast=True, codec="png")
            if png and disk:
                try:
                    os.makedirs(_thumb_dir(), exist_ok=True)
                    with open(disk, "wb") as f:
                        f.write(png)
                except OSError:
                    pass
        if not png or token != self.token:
            return
        smooth = self._smooth(png, size) if size != BASE_W else None
        ui_thread.call(lambda: self._thumb_ready(token, path, key, png, smooth))

    def _smooth(self, png, size):
        """The thumbnail smoothly scaled to the current size, as PPM (or None)."""
        scale = size / BASE_W
        try:
            proc = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "png_pipe", "-i", "-",
                 "-vf", f"scale='max(2,trunc(iw*{scale:.4f}))':-1:flags=area",
                 "-f", "image2pipe", "-vcodec", "ppm", "-"],
                input=png, capture_output=True, timeout=20, creationflags=ff._NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 and proc.stdout else None

    def _thumb_ready(self, token, path, key, png, smooth):
        if token != self.token or self.closed or not self.winfo_exists():
            return
        if key:
            self.pngs[key] = png
            self.pngs.move_to_end(key)
            while len(self.pngs) > MEMORY_CACHE:
                self.pngs.popitem(last=False)
        try:
            image = (tk.PhotoImage(data=smooth, format="ppm") if smooth
                     else self._scaled(png))
        except tk.TclError:
            return
        self._keep_image(path, image)
        if smooth or self.size.get() == BASE_W:
            self.smooth_done.add(path)
        if self.index.get(path) in self.drawn:
            self._draw_thumb(path)

    def _keep_image(self, path, image):
        self.images[path] = image
        self.images.move_to_end(path)
        # Drop the least recently used ones that aren't on the canvas.
        drawn = {self.paths[i] for i in self.drawn if i < len(self.paths)}
        for old in list(self.images):
            if len(self.images) <= SHOWN_CACHE:
                break
            if old not in drawn:
                del self.images[old]
                self.smooth_done.discard(old)

    def _scaled(self, png):
        """A quick PhotoImage of the thumbnail at the current size (picks pixels, so
        it's a little rough until the smooth one arrives)."""
        base = tk.PhotoImage(data=png, format="png")
        zoom, sub = _ratio(self.size.get() / BASE_W)
        if (zoom, sub) == (1, 1):
            return base
        img = tk.PhotoImage()
        self.tk.call(img, "copy", base, "-zoom", zoom, zoom, "-subsample", sub, sub)
        return img

    # ------------------------------------------------------------- layout
    def _metrics(self):
        w = self.size.get()
        h = round(w * BASE_H / BASE_W)
        label = self.name_font.metrics("linespace") + 4
        return w, h, label

    def _cell_xy(self, i):
        cols, left, w, _, row_h = self.grid
        row, col = divmod(i, cols)
        return left + col * (w + GAP), GAP + row * row_h

    def _layout(self):
        """Work out the grid for the panel's width; only the cells in view get drawn."""
        c = self.canvas
        c.delete("all")
        self.drawn = set()
        w, h, label = self._metrics()
        width = max(c.winfo_width(), w + 2 * GAP)
        cols = max(1, (width - GAP) // (w + GAP))
        left = (width - cols * (w + GAP) + GAP) // 2
        self.grid = (cols, left, w, h, h + label + GAP)
        rows = (len(self.paths) + cols - 1) // cols
        c.configure(scrollregion=(0, 0, width, GAP + rows * self.grid[4]))
        if not self.paths:
            c.create_text(width // 2, 40, text="No videos or pictures here",
                          fill=theme.colors["muted"])
        self._update_view()

    def _visible(self):
        """Indexes of the cells in view, plus MARGIN_ROWS rows either side."""
        if not self.paths:
            return range(0)
        cols, _, _, _, row_h = self.grid
        top = self.canvas.canvasy(0)
        bottom = top + max(1, self.canvas.winfo_height())
        first = max(0, int((top - GAP) // row_h) - MARGIN_ROWS)
        last = int(bottom // row_h) + MARGIN_ROWS
        return range(first * cols, min(len(self.paths), (last + 1) * cols))

    def _view_soon(self, first, last):
        # Only when the view really moved: drawing cells also reports here, and
        # redrawing for that would go round for ever.
        if (first, last) != self.view_at and not self.view_job:
            self.view_job = self.after(15, self._update_view)

    def _update_view(self):
        """Draw the cells that came into view, drop the ones that left, and ask for
        thumbnails for what's shown (nearest the middle of the view first)."""
        self.view_job = None
        self.view_at = tuple(str(f) for f in self.canvas.yview())
        visible = self._visible()
        keep = set(visible)
        for i in self.drawn - keep:
            self.canvas.delete(f"i{i}")
        for i in keep - self.drawn:
            self._draw_cell(i)
        self.drawn = keep
        self._mark_current()
        middle = (visible.start + visible.stop) / 2
        want = [self.paths[i] for i in sorted(visible, key=lambda i: abs(i - middle))
                if self.paths[i] not in self.smooth_done]
        with self.lock:
            self.want = want
            self.lock.notify_all()

    def _draw_cell(self, i):
        c, colors = self.canvas, theme.colors
        path = self.paths[i]
        x, y = self._cell_xy(i)
        _, _, w, h, _ = self.grid
        tag = f"i{i}"
        c.create_rectangle(x, y, x + w, y + h, fill=colors["lane"], width=0, tags=(tag, "box"))
        c.create_text(x + w // 2, y + h + 3, text=self._fit_name(os.path.basename(path), w),
                      anchor="n", font=self.name_font, fill=colors["fg"], tags=(tag, "name"))
        if path not in self.images:  # a quick version from memory, if it's there
            png = self.pngs.get(_key(path))
            if png:
                try:
                    self._keep_image(path, self._scaled(png))
                except tk.TclError:
                    pass
        if path in self.images:
            self._draw_thumb(path)

    def _draw_thumb(self, path):
        i = self.index.get(path)
        if i is None or i not in self.drawn and not self.canvas.find_withtag(f"i{i}"):
            return
        x, y = self._cell_xy(i)
        _, _, w, h, _ = self.grid
        self.canvas.delete(f"i{i}&&img")
        self.canvas.create_image(x + w // 2, y + h // 2, image=self.images[path],
                                 tags=(f"i{i}", "img"))
        self.images.move_to_end(path)

    def _fit_name(self, name, width):
        key = (name, width)
        if key not in self.names:
            if self.name_font.measure(name) <= width:
                fitted = name
            else:
                stem, ext = os.path.splitext(name)
                while stem and self.name_font.measure(stem + "\u2026" + ext) > width:
                    stem = stem[:-1]
                fitted = stem + "\u2026" + ext
            if len(self.names) > 5000:
                self.names.clear()
            self.names[key] = fitted
        return self.names[key]

    def _mark_current(self):
        c = self.canvas
        c.delete("marked")
        i = self.index.get(self.current)
        if i is not None:
            x, y = self._cell_xy(i)
            w, h, label = self._metrics()
            c.create_rectangle(x - 3, y - 3, x + w + 3, y + h + label, outline=theme.colors["select"],
                               width=3, tags="marked")

    def _scroll_to_current(self):
        i = self.index.get(self.current)
        region = self.canvas.cget("scrollregion").split()
        if i is None or len(region) < 4 or float(region[3]) <= 0:
            return
        total = float(region[3])
        top, bottom = (f * total for f in self.canvas.yview())
        _, h, label = self._metrics()
        y = self._cell_xy(i)[1]
        if y < top or y + h + label > bottom:
            self.canvas.yview_moveto(max(0.0, (y - GAP) / total))

    def _relayout_soon(self):
        if self.relayout_job:
            self.after_cancel(self.relayout_job)
        self.relayout_job = self.after(60, self._relayout)

    def _relayout(self):
        self.relayout_job = None
        self._layout()

    def _size_changed(self):
        """New thumbnail size: quick versions straight away, smooth ones as they come."""
        size = int(round(self.size.get() / 10) * 10)
        if size != self.size.get():
            self.size.set(size)
        if getattr(self, "_drawn_size", None) == size:
            return
        self._drawn_size = size
        self.size_now = size
        theme.save_setting("thumb_size", size)
        self.token += 1  # thumbnails being made at the old size aren't wanted
        self.images.clear()
        self.smooth_done.clear()
        self._layout()
        self._scroll_to_current()

    # -------------------------------------------------------------- mouse
    def _cell_at(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        cols, left, w, h, row_h = self.grid
        col, row = int((x - left) // (w + GAP)), int((y - GAP) // row_h)
        if not (0 <= col < cols and row >= 0):
            return None
        i = row * cols + col
        cx, cy = self._cell_xy(i)
        if i < len(self.paths) and cx <= x <= cx + w and cy <= y <= cy + row_h - GAP:
            return self.paths[i], cx, cy
        return None

    def _click(self, event):
        hit = self._cell_at(event)
        if hit and hit[0] != self.current:
            self.on_open(hit[0])

    def _hover(self, event):
        hit = self._cell_at(event)
        name = os.path.basename(hit[0]) if hit else None
        if name == self.hover_name:
            return
        self.hover_name = name
        self.canvas.delete("hover")
        self.canvas.config(cursor="hand2" if hit else "")
        if hit:
            _, x, y = hit
            w, h, label = self._metrics()
            self.canvas.create_rectangle(x - 2, y - 2, x + w + 2, y + h + label - 1,
                                         outline=theme.colors["muted"], width=1, tags="hover")

    def _wheel(self, event):
        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            step = -1
        else:
            step = 1
        if event.state & 0x4:  # Ctrl: bigger or smaller thumbnails
            self.size.set(min(MAX_SIZE, max(MIN_SIZE, self.size.get() - step * 20)))
            self._size_changed()
        else:
            self.canvas.yview_scroll(step * 2, "units")
        return "break"

    def _recolor(self):
        self.canvas.config(bg=theme.colors["field"], yscrollincrement=20)
        if self.paths:
            self._layout()

    def close(self):
        self.token += 1
        with self.lock:
            self.closed = True
            self.want = []
            self.lock.notify_all()
