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
    "section": "#f2b75a",
    "section_selected": "#e07b00",
}
LANE_TOP = 47  # the strip under the track that shows the saved sections


class Timeline(tk.Canvas):
    """Click or drag anywhere to seek; drag the green/red markers to set the loop.

    Saved sections are drawn in a strip underneath; clicking one selects it.

    Callbacks:
      on_seek(t)            while seeking (also called on the initial click)
      on_seek_end()         when the mouse is released after seeking
      on_marker(which, t, final)  which is "start" or "end"; final on release
      on_section(index)     a section was clicked (None: an empty part of the strip)
    """

    def __init__(self, master, on_seek, on_seek_end, on_marker, on_section=None, **kw):
        # Without on_section there's no sections strip, which makes it more compact.
        height = LANE_TOP + 14 if on_section else LANE_TOP - 2
        super().__init__(master, height=height, highlightthickness=0, **kw)
        self.on_seek = on_seek
        self.on_seek_end = on_seek_end
        self.on_marker = on_marker
        self.on_section = on_section
        self.sections = []
        self.selected = None
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

    def set_sections(self, sections, selected):
        self.sections, self.selected = list(sections), selected
        self.redraw()

    # ---------------------------------------------------------- geometry
    def _x(self, t):
        w = self.winfo_width() - 2 * MARGIN
        return MARGIN + (t / self.duration * w if self.duration else 0)

    def _t(self, x):
        w = max(1, self.winfo_width() - 2 * MARGIN)
        return min(self.duration, max(0.0, (x - MARGIN) / w * self.duration))

    def _section_at(self, x):
        t = self._t(x)
        hits = [i for i, (s, e) in enumerate(self.sections) if s <= t <= e]
        # Overlapping sections: prefer the shortest, so small ones stay clickable.
        return min(hits, key=lambda i: self.sections[i][1] - self.sections[i][0]) if hits else None

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
        if self.on_section:
            self.create_rectangle(MARGIN, LANE_TOP, w - MARGIN, LANE_TOP + 12, fill="#e6e6e6", width=0)
        if self.duration:
            order = [i for i in range(len(self.sections)) if i != self.selected]
            if self.selected is not None:
                order.append(self.selected)  # drawn last, on top
            for i in order:
                s, e = self.sections[i]
                sel = i == self.selected
                self.create_rectangle(self._x(s), LANE_TOP, max(self._x(e), self._x(s) + 2),
                                      LANE_TOP + 12, width=2 if sel else 1, outline="#7a4a00",
                                      fill=COLORS["section_selected" if sel else "section"])
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
            if self.on_section and event.y >= LANE_TOP - 2:
                cursor = "hand2" if self._section_at(event.x) is not None else ""
            else:
                cursor = "sb_h_double_arrow" if self._marker_at(event.x) else ""
            self.config(cursor=cursor)

    def _press(self, event):
        if not self.duration:
            return
        if self.on_section and event.y >= LANE_TOP - 2:
            self.on_section(self._section_at(event.x))
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
