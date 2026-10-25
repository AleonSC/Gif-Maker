"""The side panel that shows the videos in the current folder as thumbnails.

Thumbnails are made in the background (a frame a few seconds in), kept as PNG
in memory and on disk (%LOCALAPPDATA%\\GifMaker\\thumbs) so a folder opens
instantly the next time, and drawn at the size picked with the slider: the
grid fits as many per row as the panel's width allows.
"""

import collections
import concurrent.futures
import hashlib
import os
import subprocess
import tkinter as tk
from tkinter import font, ttk

from . import ffmpeg_tools as ff
from . import theme
from .tooltip import Tooltip

BASE_W, BASE_H = 320, 180  # thumbnails are made at this size and scaled down
MIN_SIZE, MAX_SIZE = 80, 320  # thumbnail widths the slider allows
GAP = 8  # pixels between thumbnails
GRAB_AT = 5.0  # seconds into the video for the thumbnail (or the start if shorter)
MEMORY_CACHE = 1500  # thumbnails kept in memory (as PNG, ~40 KB each)
DISK_CACHE = 5000  # thumbnails kept on disk


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
        self.paths = []
        self.current = None
        self.pngs = collections.OrderedDict()  # key -> PNG bytes (most recent last)
        self.images = {}  # path -> PhotoImage at the current size
        self.cells = []  # (path, x, y) of each thumbnail's top-left
        self.token = 0  # bumped when the folder changes, so stale work is dropped
        self.smooth = 0  # bumped when the size changes: drops stale smooth rescales
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.relayout_job = None
        self.smooth_job = None
        self.name_font = font.nametofont("TkDefaultFont").copy()
        self.name_font.configure(size=max(7, self.name_font.cget("size") - 1))
        self._build()
        theme.on_change(self._recolor)
        self.pool.submit(_trim_disk_cache)

    def _build(self):
        head = ttk.Frame(self)
        head.pack(fill="x", pady=(0, 4))
        self.title = ttk.Label(head, style="Head.TLabel", width=1)
        self.title.pack(side="left", fill="x", expand=True)
        close = ttk.Button(head, text="✕", width=3, command=self.on_close)
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
        self.canvas.configure(yscrollcommand=scroll.set)
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
            self.token += 1
            self.images = {p: img for p, img in self.images.items() if p in set(paths)}
            self._layout()
            self._load_thumbs()
        else:
            self._mark_current()
        self._scroll_to_current()

    def _load_thumbs(self):
        """Make the missing thumbnails, nearest the current video first."""
        token = self.token
        start = self.paths.index(self.current) if self.current in self.paths else 0
        order = sorted(range(len(self.paths)), key=lambda i: abs(i - start))
        for i in order:
            path = self.paths[i]
            if path not in self.images:
                self.pool.submit(self._make_thumb, token, path)

    def _make_thumb(self, token, path):
        if token != self.token:
            return
        key = _key(path)
        png = self.pngs.get(key) if key else None
        disk = os.path.join(_thumb_dir(), f"{key}.png") if key else None
        if png is None and disk and os.path.exists(disk):
            try:
                with open(disk, "rb") as f:
                    png = f.read()
            except OSError:
                png = None
        if png is None and token == self.token:
            png = (ff.grab_frame(self.ffmpeg, path, GRAB_AT, BASE_W, BASE_H, fast=True, codec="png")
                   or ff.grab_frame(self.ffmpeg, path, 0, BASE_W, BASE_H, fast=True, codec="png"))
            if png and disk:
                try:
                    os.makedirs(_thumb_dir(), exist_ok=True)
                    with open(disk, "wb") as f:
                        f.write(png)
                except OSError:
                    pass
        if png:
            try:
                self.after(0, lambda: self._thumb_ready(token, path, key, png))
            except (RuntimeError, tk.TclError):
                pass  # the window has closed

    def _thumb_ready(self, token, path, key, png):
        if token != self.token or not self.winfo_exists():
            return
        self.pngs[key] = png
        self.pngs.move_to_end(key)
        while len(self.pngs) > MEMORY_CACHE:
            self.pngs.popitem(last=False)
        self.images[path] = self._scaled(png)
        self._draw_thumb(path)
        self._smooth_later([path])

    def _smooth_later(self, paths):
        """Replace quick (pixel-picked) thumbnails with smoothly scaled ones."""
        size = self.size.get()
        if size == BASE_W:
            return
        token = self.smooth
        for path in paths:
            png = self.pngs.get(_key(path))
            if png:
                self.pool.submit(self._smooth, token, path, png, size)

    def _smooth(self, token, path, png, size):
        if token != self.smooth:
            return
        scale = size / BASE_W
        try:
            proc = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "png_pipe", "-i", "-",
                 "-vf", f"scale='max(2,trunc(iw*{scale:.4f}))':-1:flags=area",
                 "-f", "image2pipe", "-vcodec", "ppm", "-"],
                input=png, capture_output=True, timeout=20, creationflags=ff._NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return
        if proc.returncode == 0 and proc.stdout and token == self.smooth:
            try:
                self.after(0, lambda: self._smoothed(token, path, proc.stdout))
            except (RuntimeError, tk.TclError):
                pass

    def _smoothed(self, token, path, ppm):
        if token != self.smooth or path not in self.images or not self.winfo_exists():
            return
        try:
            self.images[path] = tk.PhotoImage(data=ppm, format="ppm")
        except tk.TclError:
            return
        self._draw_thumb(path)

    def _scaled(self, png):
        """A quick PhotoImage of the thumbnail at the current size (picks pixels, so
        it's a little rough until _smooth_later() replaces it)."""
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

    def _layout(self):
        """Place every thumbnail in rows that fit the panel's width."""
        c = self.canvas
        c.delete("all")
        w, h, label = self._metrics()
        width = max(c.winfo_width(), w + 2 * GAP)
        cols = max(1, (width - GAP) // (w + GAP))
        left = (width - cols * (w + GAP) + GAP) // 2
        self.cells = []
        for i, path in enumerate(self.paths):
            row, col = divmod(i, cols)
            x, y = left + col * (w + GAP), GAP + row * (h + label + GAP)
            self.cells.append((path, x, y))
            self._draw_cell(path, x, y)
        rows = (len(self.paths) + cols - 1) // cols
        c.configure(scrollregion=(0, 0, width, GAP + rows * (h + label + GAP)))
        if not self.paths:
            c.create_text(width // 2, 40, text="No videos here", fill=theme.colors["muted"])
        self._mark_current()

    def _draw_cell(self, path, x, y):
        c, colors = self.canvas, theme.colors
        w, h, _ = self._metrics()
        tag = self._tag(path)
        c.create_rectangle(x, y, x + w, y + h, fill=colors["lane"], width=0, tags=(tag, "box"))
        name = self._fit_name(os.path.basename(path), w)
        c.create_text(x + w // 2, y + h + 3, text=name, anchor="n", font=self.name_font,
                      fill=colors["fg"], tags=(tag, "name"))
        if path in self.images:
            self._draw_thumb(path)

    def _draw_thumb(self, path):
        cell = next(((x, y) for p, x, y in self.cells if p == path), None)
        if cell is None:
            return
        x, y = cell
        w, h, _ = self._metrics()
        tag = self._tag(path)
        self.canvas.delete(f"{tag}&&img")
        self.canvas.create_image(x + w // 2, y + h // 2, image=self.images[path],
                                 tags=(tag, "img"))

    @staticmethod
    def _tag(path):
        return "c" + hashlib.md5(path.encode("utf-8")).hexdigest()[:12]

    def _fit_name(self, name, width):
        if self.name_font.measure(name) <= width:
            return name
        stem, ext = os.path.splitext(name)
        while stem and self.name_font.measure(stem + "…" + ext) > width:
            stem = stem[:-1]
        return stem + "…" + ext

    def _mark_current(self):
        c = self.canvas
        c.delete("current")
        cell = next(((x, y) for p, x, y in self.cells if p == self.current), None)
        if cell:
            x, y = cell
            w, h, label = self._metrics()
            c.create_rectangle(x - 3, y - 3, x + w + 3, y + h + label, outline=theme.colors["select"],
                               width=3, tags="current")

    def _scroll_to_current(self):
        cell = next(((x, y) for p, x, y in self.cells if p == self.current), None)
        region = self.canvas.cget("scrollregion").split()
        if not cell or len(region) < 4 or float(region[3]) <= 0:
            return
        total = float(region[3])
        top, bottom = (f * total for f in self.canvas.yview())
        _, h, label = self._metrics()
        y = cell[1]
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
        """New thumbnail size: rescale what's loaded and reflow the rows."""
        size = int(round(self.size.get() / 10) * 10)
        if size != self.size.get():
            self.size.set(size)
        if getattr(self, "_drawn_size", None) == size:
            return
        self._drawn_size = size
        theme.save_setting("thumb_size", size)
        self.smooth += 1
        for path in list(self.images):
            key = _key(path)
            if key in self.pngs:
                self.images[path] = self._scaled(self.pngs[key])
        self._layout()
        self._scroll_to_current()
        if self.smooth_job:
            self.after_cancel(self.smooth_job)
        # Once the slider rests, smooth them, nearest the current video first.
        start = self.paths.index(self.current) if self.current in self.paths else 0
        order = sorted(self.images, key=lambda p: abs(self.paths.index(p) - start)
                       if p in self.paths else 0)
        self.smooth_job = self.after(250, lambda: self._smooth_later(order))

    # -------------------------------------------------------------- mouse
    def _cell_at(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        w, h, label = self._metrics()
        for path, cx, cy in self.cells:
            if cx <= x <= cx + w and cy <= y <= cy + h + label:
                return path, cx, cy
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
        if self.paths or self.cells:
            self._layout()

    def close(self):
        self.token += 1
        self.smooth += 1
        self.pool.shutdown(wait=False, cancel_futures=True)
