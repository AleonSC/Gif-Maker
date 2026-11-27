"""A magnifier that follows the mouse while picking an area, and soft snapping to edges.

Loupe shows the pixels around the pointer, enlarged, in a small window beside it,
with a crosshair on the exact spot. snap() pulls a position onto an edge when it's
within a few pixels of it, so a crop or recording area lands exactly on the edge of
the picture or screen without fiddling (move further to get away from it).
"""

import tkinter as tk

SNAP = 10  # pixels: how close to an edge counts as "on" it
SIZE = 160  # the magnifier's width and height, in screen pixels
ZOOM = 4  # how much it enlarges
GAP = 28  # from the pointer to the magnifier


def snap(value, edges, distance=SNAP):
    """value, or the nearest of edges if it's within distance of it."""
    best = min(edges, key=lambda e: abs(value - e), default=None)
    return best if best is not None and abs(value - best) <= distance else value


class Loupe:
    """Call show...() as the mouse moves and hide() when done."""

    def __init__(self, master, size=SIZE, zoom=ZOOM):
        self.master, self.size, self.zoom = master, size, zoom
        self.win = None

    def _make(self):
        win = self.win = tk.Toplevel(self.master)
        win.withdraw()
        win.overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        s = self.size
        self.canvas = tk.Canvas(win, width=s, height=s, bg="#202020", highlightthickness=1,
                                highlightbackground="white", bd=0)
        self.canvas.pack()
        self.photo = tk.PhotoImage(master=win, width=s, height=s)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        c, gap, arm = s // 2, ZOOM, s // 2
        for color, width in (("black", 3), ("white", 1)):  # visible on light and dark
            for x0, y0, x1, y1 in ((c - arm, c, c - gap, c), (c + gap, c, c + arm, c),
                                   (c, c - arm, c, c - gap), (c, c + gap, c, c + arm)):
                self.canvas.create_line(x0, y0, x1, y1, fill=color, width=width)
        self.text_bg = self.canvas.create_rectangle(0, 0, 0, 0, fill="black", outline="")
        self.text = self.canvas.create_text(4, s - 3, anchor="sw", fill="white",
                                            font=("Segoe UI", 8))

    def show(self, image, sx, sy, screen_x, screen_y, label="", bounds=None):
        """Magnify image (a PhotoImage or its name) around its pixel (sx, sy), next to
        the pointer at (screen_x, screen_y). bounds (x, y, w, h) is the screen area to
        keep the magnifier inside (the main screen if not given)."""
        if self.win is None:
            self._make()
        name = str(image)
        s, z = self.size, self.zoom
        half = s // (2 * z)
        tk_ = self.photo.tk
        try:
            iw, ih = int(tk_.call("image", "width", name)), int(tk_.call("image", "height", name))
        except tk.TclError:
            self.hide()
            return
        sx, sy = int(sx), int(sy)
        left, top = sx - half, sy - half
        x0, y0 = max(0, left), max(0, top)
        x1, y1 = min(iw, sx + half + 1), min(ih, sy + half + 1)
        self.photo.blank()
        if x1 > x0 and y1 > y0:
            try:
                tk_.call(self.photo, "copy", name, "-from", x0, y0, x1, y1,
                         "-to", (x0 - left) * z, (y0 - top) * z, "-zoom", z, z)
            except tk.TclError:
                pass
        self.canvas.itemconfig(self.text, text=label)
        box = self.canvas.bbox(self.text) if label else None
        self.canvas.coords(self.text_bg, *((box[0] - 3, box[1] - 1, box[2] + 3, box[3] + 1)
                                           if box else (0, 0, 0, 0)))
        bx, by, bw, bh = bounds or (0, 0, self.master.winfo_screenwidth(),
                                    self.master.winfo_screenheight())
        wx = screen_x + GAP if screen_x + GAP + s <= bx + bw else screen_x - GAP - s
        wy = screen_y + GAP if screen_y + GAP + s <= by + bh else screen_y - GAP - s
        self.win.geometry(f"+{int(wx)}+{int(wy)}")
        if self.win.state() == "withdrawn":
            self.win.deiconify()
        self.win.lift()

    def show_canvas(self, canvas, image_item, x, y, label=""):
        """Magnify what canvas's image_item shows at the canvas point (x, y)."""
        image = canvas.itemcget(image_item, "image")
        box = canvas.bbox(image_item)
        if not image or not box:
            self.hide()
            return
        self.show(image, x - box[0], y - box[1], canvas.winfo_rootx() + x,
                  canvas.winfo_rooty() + y, label)

    def hide(self):
        if self.win is not None:
            self.win.withdraw()

    def destroy(self):
        if self.win is not None:
            self.win.destroy()
            self.win = None
