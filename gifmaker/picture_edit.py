"""Rotating, flipping and cropping a picture right in the viewer.

An edit is {"rotate": degrees clockwise, "flip": mirrored, "crop": (x, y, w, h)} with the
crop as fractions of the rotated (and mirrored) picture, the same as
ffmpeg_tools.transform_filters() and the Save copy window use.
"""

from .export_dialog import ASPECTS, FULL, HANDLE, MIN_CROP_PX
from .loupe import Loupe, snap

NO_EDIT = {"rotate": 0, "flip": False, "crop": FULL}


def is_edited(edit):
    return edit["rotate"] % 360 != 0 or edit["flip"] or tuple(edit["crop"]) != FULL


def rotated(edit, degrees):
    """edit with the picture as shown turned a further 90 degrees (+90: clockwise)."""
    # Rotation comes before the mirror, so when mirrored, turn the other way.
    rotate = (edit["rotate"] + (-degrees if edit["flip"] else degrees)) % 360
    x, y, w, h = edit["crop"]
    crop = (1 - y - h, x, h, w) if degrees > 0 else (y, 1 - x - w, h, w)  # the box turns too
    return {"rotate": rotate, "flip": edit["flip"], "crop": crop}


def flipped(edit):
    x, y, w, h = edit["crop"]
    return {"rotate": edit["rotate"], "flip": not edit["flip"], "crop": (1 - x - w, y, w, h)}


def edited_size(width, height, edit, crop=True):
    """Pixel size of the picture after the edit (after just the turn if not crop)."""
    w, h = (height, width) if edit["rotate"] % 180 == 90 else (width, height)
    if crop and tuple(edit["crop"]) != FULL:
        _, _, cw, ch = edit["crop"]
        w, h = max(2, int(w * cw / 2) * 2), max(2, int(h * ch / 2) * 2)
    return w, h


def describe(width, height, edit):
    parts = []
    turn = edit["rotate"] % 360
    if turn:
        parts.append({90: "turned right", 180: "turned over", 270: "turned left"}[turn])
    if edit["flip"]:
        parts.append("mirrored")
    if tuple(edit["crop"]) != FULL:
        w, h = edited_size(width, height, edit)
        parts.append(f"cropped to {w} × {h}")
    return ", ".join(parts)


class CropBox:
    """A crop box drawn over the picture on a canvas, which the mouse moves and resizes.

    size is the picture's pixel size (turned, not cropped); set_box() tells it where the
    picture is on the canvas. crop is (x, y, w, h) fractions of the picture.
    """

    def __init__(self, canvas, crop, size, image_item=None):
        self.canvas = canvas
        self.image_item = image_item  # the picture, for the magnifier
        self.loupe = Loupe(canvas) if image_item is not None else None
        self.crop = list(crop)
        self.size = size
        self.box = None  # (x, y, w, h) of the picture on the canvas
        self.aspect = "Free"
        self.drag = None

    # -------------------------------------------------------- geometry
    def set_box(self, box):
        self.box = box
        self.draw()

    def _px(self):
        bx, by, bw, bh = self.box
        x, y, w, h = self.crop
        return bx + x * bw, by + y * bh, bx + (x + w) * bw, by + (y + h) * bh

    def ratio(self):
        value = ASPECTS.get(self.aspect)
        if value == "original":
            return self.size[0] / self.size[1]
        return value

    def set_aspect(self, name):
        """Use this shape: the biggest box of it that fits, centred on the current one."""
        self.aspect = name
        ratio = self.ratio()
        if ratio is None:
            return
        rw, rh = self.size
        x, y, w, h = self.crop
        cx, cy = x + w / 2, y + h / 2
        if rw / rh > ratio:
            w, h = ratio * rh / rw, 1.0
        else:
            w, h = 1.0, rw / ratio / rh
        self.crop = [min(max(0.0, cx - w / 2), 1 - w), min(max(0.0, cy - h / 2), 1 - h), w, h]
        self.draw()

    # ----------------------------------------------------------- drawing
    def draw(self):
        c = self.canvas
        c.delete("cropbox")
        if not self.box:
            return
        bx, by, bw, bh = self.box
        x0, y0, x1, y1 = self._px()
        shade = dict(fill="black", stipple="gray50", outline="", tags="cropbox")
        for rect in ((bx, by, bx + bw, y0), (bx, y1, bx + bw, by + bh),
                     (bx, y0, x0, y1), (x1, y0, bx + bw, y1)):
            if rect[2] - rect[0] >= 1 and rect[3] - rect[1] >= 1:
                c.create_rectangle(*rect, **shade)
        for k in (1, 2):  # thirds, to help line things up
            gx, gy = x0 + (x1 - x0) * k / 3, y0 + (y1 - y0) * k / 3
            c.create_line(gx, y0, gx, y1, fill="#cccccc", dash=(2, 4), tags="cropbox")
            c.create_line(x0, gy, x1, gy, fill="#cccccc", dash=(2, 4), tags="cropbox")
        c.create_rectangle(x0, y0, x1, y1, outline="white", width=2, tags="cropbox")
        for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            c.create_rectangle(cx - 5, cy - 5, cx + 5, cy + 5, fill="white", outline="#333",
                               tags="cropbox")
        rw, rh = self.size
        w, h = max(2, int(rw * self.crop[2] / 2) * 2), max(2, int(rh * self.crop[3] / 2) * 2)
        label_y = y0 - 6 if y0 - by > 18 else y0 + 18
        c.create_text(x0 + 4, label_y, anchor="sw", fill="white", text=f"{w} × {h}",
                      tags="cropbox")

    def clear(self):
        self.canvas.delete("cropbox")
        if self.loupe:
            self.loupe.destroy()

    # ------------------------------------------------------------- mouse
    def _hit(self, px, py):
        x0, y0, x1, y1 = self._px()
        for cx, cy, ax, ay in ((x0, y0, x1, y1), (x1, y0, x0, y1), (x0, y1, x1, y0),
                               (x1, y1, x0, y0)):
            if abs(px - cx) <= HANDLE and abs(py - cy) <= HANDLE:
                return "corner", (ax, ay)  # anchor: the opposite corner
        if x0 < px < x1 and y0 < py < y1 and tuple(self.crop) != FULL:
            return "move", (px - x0, py - y0)
        return "new", (px, py)

    def _clamp(self, event):
        """The pointer kept on the picture, and pulled onto its edges when near them."""
        bx, by, bw, bh = self.box
        x, y = min(max(event.x, bx), bx + bw), min(max(event.y, by), by + bh)
        return snap(x, (bx, bx + bw)), snap(y, (by, by + bh))

    def cursor(self, event):
        if not self.box or self.drag:
            return None
        mode = self._hit(event.x, event.y)[0]
        return {"corner": "sizing", "move": "fleur"}.get(mode, "crosshair")

    def press(self, event):
        if self.box:
            self.drag = self._hit(*self._clamp(event))

    def motion(self, event):
        if not self.drag or not self.box:
            return
        mode, data = self.drag
        bx, by, bw, bh = self.box
        px, py = self._clamp(event)
        if mode == "move":
            x0, y0, x1, y1 = self._px()
            w, h = x1 - x0, y1 - y0
            px, py = min(max(event.x, bx), bx + bw), min(max(event.y, by), by + bh)
            nx = min(max(px - data[0], bx), bx + bw - w)
            ny = min(max(py - data[1], by), by + bh - h)
            nx = snap(nx, (bx, bx + bw - w))  # either side of the box onto the edge
            ny = snap(ny, (by, by + bh - h))
            self.crop[0], self.crop[1] = (nx - bx) / bw, (ny - by) / bh
        else:
            self._resize(data, px, py)
        self.draw()
        if self.loupe:
            if mode == "move":  # the corner nearest the pointer
                x0, y0, x1, y1 = self._px()
                px = x0 if abs(px - x0) < abs(px - x1) else x1
                py = y0 if abs(py - y0) < abs(py - y1) else y1
            self.loupe.show_canvas(self.canvas, self.image_item, px, py)

    def release(self, _event):
        self.drag = None
        if self.loupe:
            self.loupe.hide()

    def _resize(self, anchor, px, py):
        """Make the crop the box between anchor and the pointer (keeping the shape)."""
        bx, by, bw, bh = self.box
        ax, ay = anchor
        rw, rh = self.size
        sx, sy = rw / bw, rh / bh  # picture pixels per canvas pixel
        dx, dy = px - ax, py - ay
        ratio = self.ratio()
        if ratio is not None:
            room_x = (bx + bw - ax) if dx >= 0 else (ax - bx)
            room_y = (by + bh - ay) if dy >= 0 else (ay - by)
            w = max(abs(dx), abs(dy) * ratio * sy / sx)
            w = min(w, room_x, room_y * ratio * sy / sx)
            h = w * sx / sy / ratio
            dx, dy = (w if dx >= 0 else -w), (h if dy >= 0 else -h)
        if abs(dx) < MIN_CROP_PX / sx or abs(dy) < MIN_CROP_PX / sy:
            return
        x0, x1 = sorted((ax, ax + dx))
        y0, y1 = sorted((ay, ay + dy))
        self.crop = [(x0 - bx) / bw, (y0 - by) / bh, (x1 - x0) / bw, (y1 - y0) / bh]
