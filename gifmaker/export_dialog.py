"""Preview popup for snapshots and GIFs, with rotate, flip and crop before saving."""

import threading
import tkinter as tk
from tkinter import ttk

from . import ffmpeg_tools as ff
from .player import Player

VIEW_W, VIEW_H = 720, 480
HANDLE = 10  # grab distance for crop corners, in pixels
MIN_CROP_PX = 16  # smallest crop, in output pixels
ASPECTS = {
    "Free": None,
    "Original": "original",
    "1:1": 1.0,
    "4:3": 4 / 3,
    "3:4": 3 / 4,
    "16:9": 16 / 9,
    "9:16": 9 / 16,
}
FULL = (0.0, 0.0, 1.0, 1.0)


class ExportDialog(tk.Toplevel):
    """Shows what will be saved and lets the user rotate, flip and crop it.

    mode is "snapshot" (the frame at `time`) or "gif" (loops start..end at the
    GIF's fps and speed). transform is a dict with rotate (degrees clockwise),
    flip (mirror) and crop ((x, y, w, h) fractions of the rotated frame).
    on_save(transform) is called with the chosen settings when Save is clicked.
    """

    def __init__(self, app, mode, transform, on_save, time=0.0, start=0.0, end=0.0,
                 fps=15, width=0, speed=1.0):
        super().__init__(app.root)
        self.app = app
        self.mode = mode
        self.on_save = on_save
        self.time, self.start, self.end = time, start, end
        self.fps, self.gif_width, self.speed = fps, width, speed
        self.rotate = transform["rotate"] % 360
        self.flip = transform["flip"]
        self.crop = list(transform["crop"])
        self.aspect = tk.StringVar(value="Free")
        self.drag = None  # (mode, data) while dragging the crop box
        self.photo = tk.PhotoImage(master=self)
        self.box = (0, 0, 2, 2)  # where the image is drawn: x, y, w, h
        self.grab_token = 0

        self.title("Save snapshot" if mode == "snapshot" else "Save GIF")
        self.transient(app.root)
        self.resizable(False, False)
        self._build()
        self.player = Player(self, app.ffmpeg, self._on_frame, self._restart_loop)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        self.bind("<Return>", lambda e: self._save())
        self._reload()
        self.grab_set()
        self.focus_set()

    # ---------------------------------------------------------------- UI
    def _build(self):
        main = ttk.Frame(self, padding=10)
        main.pack(fill="both", expand=True)

        tools = ttk.Frame(main)
        tools.pack(fill="x", pady=(0, 6))
        ttk.Button(tools, text="⟲ Rotate left", command=lambda: self._rotate(-90)).pack(side="left")
        ttk.Button(tools, text="⟳ Rotate right", command=lambda: self._rotate(90)).pack(side="left", padx=4)
        ttk.Button(tools, text="⇆ Flip", command=self._toggle_flip).pack(side="left")
        ttk.Label(tools, text="Crop shape").pack(side="left", padx=(16, 4))
        aspect = ttk.Combobox(tools, textvariable=self.aspect, values=list(ASPECTS),
                              width=9, state="readonly")
        aspect.pack(side="left")
        aspect.bind("<<ComboboxSelected>>", lambda e: self._apply_aspect())
        ttk.Button(tools, text="Reset", command=self._reset).pack(side="left", padx=4)

        self.canvas = tk.Canvas(main, width=VIEW_W, height=VIEW_H, bg="#111",
                                highlightthickness=0, cursor="crosshair")
        self.canvas.pack()
        self.image_item = self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag", None))
        self.canvas.bind("<Motion>", self._hover)

        ttk.Label(main, text="Drag on the picture to crop; drag the box or its corners to adjust.",
                  foreground="#666").pack(anchor="w", pady=(6, 0))
        bottom = ttk.Frame(main)
        bottom.pack(fill="x", pady=(6, 0))
        self.info = ttk.Label(bottom)
        self.info.pack(side="left")
        ttk.Button(bottom, text="Cancel", command=self.close).pack(side="right")
        ttk.Button(bottom, text="Save snapshot..." if self.mode == "snapshot" else "Save GIF...",
                   command=self._save).pack(side="right", padx=6)

    # -------------------------------------------------------- transforms
    def transform(self):
        return {"rotate": self.rotate, "flip": self.flip, "crop": tuple(self.crop)}

    def _rotated_size(self):
        """Pixel size of the frame after rotation (before cropping)."""
        w, h = self.app.video_width or VIEW_W, self.app.video_height or VIEW_H
        return (h, w) if self.rotate in (90, 270) else (w, h)

    def _rotate(self, degrees):
        # Rotation is applied before the mirror, so when mirrored, turn the other
        # way to make the picture on screen turn the way the button says.
        self.rotate = (self.rotate + (-degrees if self.flip else degrees)) % 360
        x, y, w, h = self.crop
        if degrees > 0:  # the crop box turns with the picture
            self.crop = [1 - y - h, x, h, w]
        else:
            self.crop = [y, 1 - x - w, h, w]
        if self.aspect.get() == "Original":
            self.crop = list(FULL)
        self._reload()

    def _toggle_flip(self):
        self.flip = not self.flip
        x, y, w, h = self.crop
        self.crop = [1 - x - w, y, w, h]
        self._reload()

    def _reset(self):
        self.rotate, self.flip, self.crop = 0, False, list(FULL)
        self.aspect.set("Free")
        self._reload()

    def _aspect_ratio(self):
        """Required width/height of the crop in output pixels, or None for free."""
        value = ASPECTS[self.aspect.get()]
        if value == "original":
            rw, rh = self._rotated_size()
            return rw / rh
        return value

    def _apply_aspect(self):
        ratio = self._aspect_ratio()
        if ratio is None:
            return
        # Largest box of this shape that fits, centred on the current crop.
        rw, rh = self._rotated_size()
        x, y, w, h = self.crop
        cx, cy = x + w / 2, y + h / 2
        if rw / rh > ratio:
            w, h = ratio * rh / rw, 1.0
        else:
            w, h = 1.0, rw / ratio / rh
        x = min(max(0.0, cx - w / 2), 1 - w)
        y = min(max(0.0, cy - h / 2), 1 - h)
        self.crop = [x, y, w, h]
        self._draw_crop()

    # ----------------------------------------------------------- preview
    def _reload(self):
        """Fit the rotated frame in the view and (re)start the preview."""
        rw, rh = self._rotated_size()
        scale = min(VIEW_W / rw, VIEW_H / rh)
        dw, dh = max(2, int(rw * scale) // 2 * 2), max(2, int(rh * scale) // 2 * 2)
        self.box = ((VIEW_W - dw) // 2, (VIEW_H - dh) // 2, dw, dh)
        self.canvas.coords(self.image_item, self.box[0], self.box[1])
        rot = ff.transform_filters(self.rotate, self.flip)
        if self.mode == "gif":
            self.player.start(self.app.video_path, self.start, (dw, dh), self.fps,
                              end=self.end, pre_vf=rot, speed=self.speed)
        else:
            self.grab_token += 1
            token, path, t = self.grab_token, self.app.video_path, self.time
            vf = ff.chain(rot, f"scale={dw}:{dh}")

            def work():
                data = ff.grab_frame(self.app.ffmpeg, path, t, dw, dh, pre_vf=vf)
                self.after(0, lambda: self._show_still(token, data))

            threading.Thread(target=work, daemon=True).start()
        self._draw_crop()

    def _show_still(self, token, data):
        if token == self.grab_token and data and self.winfo_exists():
            self._on_frame(0, data)

    def _on_frame(self, _t, data):
        try:
            self.photo.configure(data=data, format="ppm")
        except tk.TclError:
            pass

    def _restart_loop(self):
        self._reload()

    # -------------------------------------------------------------- crop
    def _crop_px(self):
        bx, by, bw, bh = self.box
        x, y, w, h = self.crop
        return bx + x * bw, by + y * bh, bx + (x + w) * bw, by + (y + h) * bh

    def _draw_crop(self):
        self.canvas.delete("crop")
        bx, by, bw, bh = self.box
        x0, y0, x1, y1 = self._crop_px()
        shade = dict(fill="black", stipple="gray50", width=0, tags="crop")
        self.canvas.create_rectangle(bx, by, bx + bw, y0, **shade)
        self.canvas.create_rectangle(bx, y1, bx + bw, by + bh, **shade)
        self.canvas.create_rectangle(bx, y0, x0, y1, **shade)
        self.canvas.create_rectangle(x1, y0, bx + bw, y1, **shade)
        self.canvas.create_rectangle(x0, y0, x1, y1, outline="white", width=2, tags="crop")
        for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            self.canvas.create_rectangle(cx - 5, cy - 5, cx + 5, cy + 5, fill="white",
                                         outline="#333", tags="crop")
        self._update_info()

    def _output_size(self):
        """Crop size in pixels, as ffmpeg's crop filter will compute it."""
        rw, rh = self._rotated_size()
        _, _, w, h = self.crop
        return max(2, int(rw * w / 2) * 2), max(2, int(rh * h / 2) * 2)

    def _update_info(self):
        cw, ch = self._output_size()
        if self.mode == "snapshot":
            self.info.config(text=f"Output: {cw} × {ch} px")
            return
        ow = min(self.gif_width, cw) if self.gif_width > 0 else cw
        oh = max(2, round(ch * ow / cw / 2) * 2)
        secs = (self.end - self.start) / self.speed
        self.info.config(text=f"GIF: {ow} × {oh} px, {secs:.2f} s at {self.fps} fps "
                              f"({round(secs * self.fps)} frames)")

    def _hover(self, event):
        if self.drag:
            return
        mode = self._hit(event.x, event.y)[0]
        cursor = {"corner": "sizing", "move": "fleur"}.get(mode, "crosshair")
        self.canvas.config(cursor=cursor)

    def _hit(self, px, py):
        x0, y0, x1, y1 = self._crop_px()
        for cx, cy, ax, ay in ((x0, y0, x1, y1), (x1, y0, x0, y1), (x0, y1, x1, y0), (x1, y1, x0, y0)):
            if abs(px - cx) <= HANDLE and abs(py - cy) <= HANDLE:
                return "corner", (ax, ay)  # anchor: the opposite corner
        # Inside the box moves it, unless it's still the whole frame (nothing to move).
        if x0 < px < x1 and y0 < py < y1 and tuple(self.crop) != FULL:
            return "move", (px - x0, py - y0)
        return "new", (px, py)

    def _press(self, event):
        bx, by, bw, bh = self.box
        px = min(max(event.x, bx), bx + bw)
        py = min(max(event.y, by), by + bh)
        self.drag = self._hit(px, py)

    def _motion(self, event):
        if not self.drag:
            return
        mode, data = self.drag
        bx, by, bw, bh = self.box
        px = min(max(event.x, bx), bx + bw)
        py = min(max(event.y, by), by + bh)
        if mode == "move":
            x0, y0, x1, y1 = self._crop_px()
            w, h = x1 - x0, y1 - y0
            nx = min(max(px - data[0], bx), bx + bw - w)
            ny = min(max(py - data[1], by), by + bh - h)
            self.crop[0], self.crop[1] = (nx - bx) / bw, (ny - by) / bh
        else:
            self._resize(data, px, py)
        self._draw_crop()

    def _resize(self, anchor, px, py):
        """Make the crop the box between anchor and the pointer (keeping the shape)."""
        bx, by, bw, bh = self.box
        ax, ay = anchor
        rw, rh = self._rotated_size()
        sx, sy = rw / bw, rh / bh  # output pixels per view pixel
        dx, dy = px - ax, py - ay
        ratio = self._aspect_ratio()
        if ratio is not None:
            # Fit the shape inside the pointer box, then inside the room available.
            room_x = (bx + bw - ax) if dx >= 0 else (ax - bx)
            room_y = (by + bh - ay) if dy >= 0 else (ay - by)
            w = max(abs(dx), abs(dy) * ratio * sy / sx)
            w = min(w, room_x, room_y * ratio * sy / sx)
            h = w * sx / sy / ratio
            dx, dy = (w if dx >= 0 else -w), (h if dy >= 0 else -h)
        min_w, min_h = MIN_CROP_PX / sx, MIN_CROP_PX / sy
        if abs(dx) < min_w or abs(dy) < min_h:
            return
        x0, x1 = sorted((ax, ax + dx))
        y0, y1 = sorted((ay, ay + dy))
        self.crop = [(x0 - bx) / bw, (y0 - by) / bh, (x1 - x0) / bw, (y1 - y0) / bh]

    # ------------------------------------------------------------ finish
    def _save(self):
        transform = self.transform()
        self.close()
        self.on_save(transform)

    def close(self):
        self.player.close()
        self.grab_release()
        self.destroy()
