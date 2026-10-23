"""A seek bar with a playhead and draggable start/end loop markers."""

import tkinter as tk

MARGIN = 12  # pixels left/right of the track, so the markers can be grabbed at the ends
GRAB = 8  # how close (pixels) the mouse must be to grab a marker
COLORS = {
    "track": "#c8c8c8",
    "range": "#9cc3f5",
    "start": "#2e9e44",
    "end": "#d03a3a",
    "playhead": "#222222",
}


class Timeline(tk.Canvas):
    """Click or drag anywhere to seek; drag the green/red markers to set the loop.

    Callbacks:
      on_seek(t)            while seeking (also called on the initial click)
      on_seek_end()         when the mouse is released after seeking
      on_marker(which, t, final)  which is "start" or "end"; final on release
    """

    def __init__(self, master, on_seek, on_seek_end, on_marker, **kw):
        super().__init__(master, height=44, highlightthickness=0, **kw)
        self.on_seek = on_seek
        self.on_seek_end = on_seek_end
        self.on_marker = on_marker
        self.duration = 0.0
        self.position = 0.0
        self.start = 0.0
        self.end = 0.0
        self.dragging = None  # None, "seek", "start" or "end"
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Motion>", self._hover)

    # ------------------------------------------------------------ public
    def set_duration(self, duration):
        self.duration = max(0.0, duration)
        self.redraw()

    def set_position(self, t):
        self.position = t
        self._draw_playhead()

    def set_range(self, start, end):
        self.start, self.end = start, end
        self.redraw()

    # ---------------------------------------------------------- geometry
    def _x(self, t):
        w = self.winfo_width() - 2 * MARGIN
        return MARGIN + (t / self.duration * w if self.duration else 0)

    def _t(self, x):
        w = max(1, self.winfo_width() - 2 * MARGIN)
        return min(self.duration, max(0.0, (x - MARGIN) / w * self.duration))

    def _marker_at(self, x):
        if not self.duration:
            return None
        ds, de = abs(x - self._x(self.start)), abs(x - self._x(self.end))
        if min(ds, de) > GRAB:
            return None
        if ds == de:  # markers on top of each other: pick by side
            return "start" if x < self._x(self.start) else "end"
        return "start" if ds < de else "end"

    # ----------------------------------------------------------- drawing
    def redraw(self):
        self.delete("all")
        w = self.winfo_width()
        self.create_rectangle(MARGIN, 18, w - MARGIN, 26, fill=COLORS["track"], width=0)
        if self.duration:
            xs, xe = self._x(self.start), self._x(self.end)
            self.create_rectangle(xs, 14, xe, 30, fill=COLORS["range"], width=0)
            for x, color, sign in ((xs, COLORS["start"], 1), (xe, COLORS["end"], -1)):
                self.create_line(x, 6, x, 38, fill=color, width=2)
                # a flag pointing into the loop, easy to grab
                self.create_polygon(x, 30, x, 42, x + 9 * sign, 36, fill=color, outline=color)
                self.create_polygon(x, 2, x, 14, x + 9 * sign, 8, fill=color, outline=color)
        self._draw_playhead()

    def _draw_playhead(self):
        self.delete("playhead")
        if not self.duration:
            return
        x = self._x(self.position)
        self.create_line(x, 10, x, 34, fill=COLORS["playhead"], width=2, tags="playhead")
        self.create_oval(x - 5, 17, x + 5, 27, fill="white", outline=COLORS["playhead"],
                         width=2, tags="playhead")

    # ------------------------------------------------------------- mouse
    def _hover(self, event):
        if self.dragging is None:
            self.config(cursor="sb_h_double_arrow" if self._marker_at(event.x) else "")

    def _press(self, event):
        if not self.duration:
            return
        self.dragging = self._marker_at(event.x) or "seek"
        self._motion(event)

    def _motion(self, event):
        if not self.dragging:
            return
        t = self._t(event.x)
        if self.dragging == "seek":
            self.on_seek(t)
        else:
            self.on_marker(self.dragging, t, False)

    def _release(self, event):
        which, self.dragging = self.dragging, None
        if which == "seek":
            self.on_seek_end()
        elif which:
            self.on_marker(which, self._t(event.x), True)
