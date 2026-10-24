"""Preview popup for snapshots, animations and videos: rotate, flip and crop before saving."""

import os
import shutil
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import ffmpeg_tools as ff
from . import theme
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
# ffmpeg 4.2 can write animated WebP but not read it back, so its result can't be played.
PLAYABLE_RESULTS = ("GIF", "Animated PNG")


class ExportDialog(tk.Toplevel):
    """Shows what will be saved and lets the user rotate, flip and crop it.

    mode is "snapshot" (the frame at `time`), "gif" (an animation, played at its
    fps and speed) or "video" (played with sound). choices is a list of
    (label, sections, separate): what can be saved, e.g. the loop markers or each
    section as its own file; the preview plays the chosen sections in turn. With
    more than one choice the user picks with radio buttons.

    transform is a dict with rotate (degrees clockwise), flip (mirror) and crop
    ((x, y, w, h) fractions of the rotated frame).

    For animations the user also picks the format (from `formats`) and direction
    here, and render(transform, start, end, dst, fmt, direction) returns the ffmpeg
    command that makes the file; "Preview result" uses it to make and play the
    real thing. Each animation must be at most max_seconds long.

    on_save(transform, choice_index, rendered, options) is called when Save is
    clicked; rendered is the path of an already-made result that matches, or None,
    and options holds the animation's fmt and direction.
    """

    def __init__(self, app, mode, transform, on_save, time=0.0, choices=None, fps=15,
                 width=0, speed=1.0, sound=False, fmt="GIF", direction="Forward", render=None,
                 formats=("GIF",), max_seconds=None):
        super().__init__(app.root)
        self.app = app
        self.mode = mode
        self.on_save = on_save
        self.time = time
        self.choices = choices or [("", [(time, time)], False)]
        self.choice = tk.IntVar(value=0)
        self.section = 0  # which section of the chosen ones the preview is playing
        self.fps, self.out_width, self.speed = fps, width, speed
        self.sound = sound
        self.fmt_var = tk.StringVar(value=fmt if fmt in formats else formats[0])
        self.direction_var = tk.StringVar(value=direction)
        self.formats, self.render, self.max_seconds = formats, render, max_seconds
        self.rotate = transform["rotate"] % 360
        self.flip = transform["flip"]
        self.crop = list(transform["crop"])
        self.aspect = tk.StringVar(value="Free")
        self.drag = None  # (mode, data) while dragging the crop box
        self.photo = tk.PhotoImage(master=self)
        self.box = (0, 0, 2, 2)  # where the image is drawn: x, y, w, h
        self.grab_token = 0
        self.closed = False
        # Result preview: the real file made in a temp folder, and what it was made from.
        self.temp_dir = tempfile.mkdtemp(prefix="gifmaker-")
        self.result = None  # (path, transform, choice) of the finished result
        self.showing_result = False
        self.render_token = 0

        self._set_what()
        self.transient(app.root)
        self.resizable(False, False)
        theme.style_window(self)
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
        if self.mode == "gif":
            anim = ttk.Frame(main)
            anim.pack(fill="x", pady=(0, 6))
            ttk.Label(anim, text="Format").pack(side="left")
            fmt = ttk.Combobox(anim, textvariable=self.fmt_var, values=self.formats, width=13,
                               state="readonly")
            fmt.pack(side="left", padx=(4, 16))
            fmt.bind("<<ComboboxSelected>>", lambda e: self._options_changed())
            ttk.Label(anim, text="Direction").pack(side="left")
            direction = ttk.Combobox(anim, textvariable=self.direction_var, values=ff.DIRECTIONS,
                                     width=10, state="readonly")
            direction.pack(side="left", padx=4)
            direction.bind("<<ComboboxSelected>>", lambda e: self._options_changed())
            ttk.Label(anim, text="WebP and animated PNG keep full colour; boomerang plays "
                                 "forward then back.", style="Muted.TLabel").pack(side="left", padx=(10, 0))
            self.result_btn = ttk.Button(anim, text="Preview result", command=self._toggle_result)
            self.result_btn.pack(side="right")

        self.canvas = tk.Canvas(main, width=VIEW_W, height=VIEW_H, bg="#111",
                                highlightthickness=0, cursor="crosshair")
        self.canvas.pack()
        self.image_item = self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag", None))
        self.canvas.bind("<Motion>", self._hover)

        self.hint = ttk.Label(main, style="Muted.TLabel")
        self.hint.pack(anchor="w", pady=(6, 0))
        if len(self.choices) > 1:
            pick = ttk.Frame(main)
            pick.pack(anchor="w", pady=(6, 0))
            ttk.Label(pick, text="Save:").pack(side="left")
            for i, (label, _, _) in enumerate(self.choices):
                ttk.Radiobutton(pick, text=label, value=i, variable=self.choice,
                                command=self._choice_changed).pack(side="left", padx=(8, 0))
        bottom = ttk.Frame(main)
        bottom.pack(fill="x", pady=(6, 0))
        self.info = ttk.Label(bottom)
        self.info.pack(side="left")
        ttk.Button(bottom, text="Cancel", command=self.close).pack(side="right")
        self.save_btn = ttk.Button(bottom, command=self._save)
        self.save_btn.pack(side="right", padx=6)
        self._update_labels()

    @property
    def fmt(self):
        return self.fmt_var.get()

    @property
    def direction(self):
        return self.direction_var.get()

    def _set_what(self):
        names = {"GIF": "GIF", "WebP": "WebP", "Animated PNG": "animated PNG"}
        self.what = {"snapshot": "snapshot", "video": "video"}.get(self.mode) or names[self.fmt]
        self.title(f"Save {self.what}")

    def _options_changed(self):
        """A different format or direction: the made preview no longer applies."""
        self._set_what()
        self._leave_result()
        self._update_labels()
        self._update_info()

    def _sections(self):
        return self.choices[self.choice.get()][1]

    def _separate(self):
        return self.choices[self.choice.get()][2]

    def _update_labels(self):
        n = len(self._sections())
        files = f" ({n} files)" if self._separate() and n > 1 else ""
        self.save_btn.config(text=f"Save {self.what}{files}...")
        if self.showing_result:
            hint = "Showing the finished result. Click Back to editing to change it."
        else:
            hint = "Drag on the picture to crop; drag the box or its corners to adjust."
        self.hint.config(text=hint)

    def _choice_changed(self):
        self.section = 0
        self._leave_result()
        self._update_labels()
        self._reload()

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
        self._leave_result()
        self._reload()

    def _toggle_flip(self):
        self.flip = not self.flip
        x, y, w, h = self.crop
        self.crop = [1 - x - w, y, w, h]
        self._leave_result()
        self._reload()

    def _reset(self):
        self.rotate, self.flip, self.crop = 0, False, list(FULL)
        self.aspect.set("Free")
        self._leave_result()
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
        self._leave_result()
        self._draw_crop()

    # ----------------------------------------------------------- preview
    def _reload(self):
        """Fit the rotated frame in the view and (re)start the preview."""
        if self.showing_result:
            self._play_result()
            return
        rw, rh = self._rotated_size()
        scale = min(VIEW_W / rw, VIEW_H / rh)
        dw, dh = max(2, int(rw * scale) // 2 * 2), max(2, int(rh * scale) // 2 * 2)
        self.box = ((VIEW_W - dw) // 2, (VIEW_H - dh) // 2, dw, dh)
        self.canvas.coords(self.image_item, self.box[0], self.box[1])
        rot = ff.transform_filters(self.rotate, self.flip)
        if self.mode in ("gif", "video"):
            start, end = self._sections()[self.section]
            self.player.muted = not self.app.sound.get()
            self.player.volume = self.app.volume.get() / 100
            self.player.start(self.app.video_path, start, (dw, dh), self.fps,
                              end=end, pre_vf=rot, speed=self.speed, sound=self.sound)
        else:
            self.grab_token += 1
            token, path, t = self.grab_token, self.app.video_path, self.time
            vf = ff.chain(rot, f"scale={dw}:{dh}")

            def work():
                data = ff.grab_frame(self.app.ffmpeg, path, t, dw, dh, pre_vf=vf)
                self._from_thread(lambda: self._show_still(token, data))

            threading.Thread(target=work, daemon=True).start()
        self._draw_crop()

    def _show_still(self, token, data):
        if token == self.grab_token and data and not self.closed:
            self._on_frame(0, data)

    def _on_frame(self, _t, data):
        try:
            self.photo.configure(data=data, format="ppm")
        except tk.TclError:
            pass

    def _restart_loop(self):
        if not self.showing_result:  # next section, then round again
            self.section = (self.section + 1) % len(self._sections())
        self._reload()

    # ---------------------------------------------------- result preview
    def _result_valid(self):
        return (self.result is not None and self.result[1:] == (
            self.transform(), self.choice.get(), self.fmt, self.direction))

    def _toggle_result(self):
        if self.showing_result:
            self._leave_result()
            self._reload()
            return
        if self._result_valid():
            self._show_result()
            return
        # Make the real file in the background, then play it.
        self.render_token += 1
        token, transform, choice = self.render_token, self.transform(), self.choice.get()
        fmt, direction = self.fmt, self.direction
        start, end = self._sections()[0]
        dst = os.path.join(self.temp_dir, f"result{self.render_token}{ff.ANIMATION_FORMATS[fmt][0]}")
        cmd = self.render(transform, start, end, dst, fmt, direction)
        seconds = ff.animation_seconds(start, end, self.speed, self.direction)
        self.result_btn.config(text="Making preview...", state="disabled")

        def progress(frac):
            self._from_thread(lambda: token == self.render_token and not self.closed and
                              self.info.config(text=f"Making the preview... {frac:.0%}"))

        def work():
            err = ff.run_with_progress(cmd, seconds, progress,
                                       lambda: self.closed or token != self.render_token)
            self._from_thread(lambda: self._render_done(token, err, dst, (transform, choice, fmt,
                                                                          direction)))

        threading.Thread(target=work, daemon=True).start()

    def _from_thread(self, fn):
        """Run fn on the Tk thread, unless the window has gone in the meantime."""
        try:
            self.after(0, fn)
        except (RuntimeError, tk.TclError):
            pass

    def _render_done(self, token, err, dst, made_from):
        if self.closed or token != self.render_token:
            return
        self.result_btn.config(text="Preview result", state="normal")
        if err:
            self.info.config(text="Couldn't make the preview: " + err.splitlines()[-1][:80])
            return
        self.result = (dst, *made_from)
        self._show_result()

    def _show_result(self):
        self.showing_result = True
        self.result_btn.config(text="Back to editing")
        self.canvas.delete("crop")
        self.canvas.config(cursor="")
        self._update_labels()
        self._play_result()

    def _play_result(self):
        """Play the finished file at its real size (shrunk only if it doesn't fit)."""
        path = self.result[0]
        ow, oh = self._animation_size()
        dw, dh = ff.fit_size(ow, oh, VIEW_W, VIEW_H)
        self.box = ((VIEW_W - dw) // 2, (VIEW_H - dh) // 2, dw, dh)
        self.canvas.coords(self.image_item, self.box[0], self.box[1])
        size_mb = os.path.getsize(path) / (1024 * 1024)
        start, end = self._sections()[0]
        seconds = ff.animation_seconds(start, end, self.speed, self.direction)
        which = " (first section)" if len(self._sections()) > 1 else ""
        text = f"Result{which}: {ow} × {oh} px, {seconds:.2f} s, {size_mb:.2f} MB"
        if self.fmt in PLAYABLE_RESULTS:
            self.player.start(path, 0.0, (dw, dh), self.fps)
        else:  # can't decode it, so show its size and keep playing the clip
            text += " (WebP can't be played back here; showing the video frames)"
            rot = ff.transform_filters(self.rotate, self.flip)
            crop = ff.transform_filters(crop=self.crop)
            self.player.start(self.app.video_path, start, (dw, dh), self.fps, end=end,
                              pre_vf=ff.chain(rot, crop), speed=self.speed)
        self.info.config(text=text)

    def _leave_result(self):
        self.render_token += 1  # cancels a preview that's still being made
        if self.mode == "gif":
            self.result_btn.config(text="Preview result", state="normal")
        if self.showing_result:
            self.showing_result = False
            self.canvas.config(cursor="crosshair")
            self._update_labels()

    # -------------------------------------------------------------- crop
    def _crop_px(self):
        bx, by, bw, bh = self.box
        x, y, w, h = self.crop
        return bx + x * bw, by + y * bh, bx + (x + w) * bw, by + (y + h) * bh

    def _draw_crop(self):
        self.canvas.delete("crop")
        if self.showing_result:
            return
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

    def _animation_size(self):
        """Size of the animation: the crop, scaled down to the chosen width."""
        cw, ch = self._output_size()
        ow = min(self.out_width, cw) if self.out_width > 0 else cw
        return ow, max(2, round(ch * ow / cw / 2) * 2)

    def _update_info(self):
        cw, ch = self._output_size()
        sections = self._sections()
        if self.mode == "snapshot":
            self.info.config(text=f"Output: {cw} × {ch} px")
            return
        if self.mode == "video":
            total = sum(e - s for s, e in sections)
            if self._separate():
                text = f"{len(sections)} videos, {cw} × {ch} px, {total:.2f} s in all"
            else:
                parts = f" from {len(sections)} sections" if len(sections) > 1 else ""
                text = f"Video: {cw} × {ch} px, {total:.2f} s{parts}"
            self.info.config(text=text)
            return
        ow, oh = self._animation_size()
        lengths = [ff.animation_seconds(s, e, self.speed, self.direction) for s, e in sections]
        if len(sections) > 1:
            text = (f"{len(sections)} files, {ow} × {oh} px, "
                    f"{min(lengths):.2f}-{max(lengths):.2f} s each at {self.fps} fps")
        else:
            text = (f"{self.what}: {ow} × {oh} px, {lengths[0]:.2f} s at {self.fps} fps "
                    f"({round(lengths[0] * self.fps)} frames)")
        if self.direction != "Forward":
            text += f", {self.direction.lower()}"
        self.info.config(text=text)

    def _hover(self, event):
        if self.drag or self.showing_result:
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
        if self.showing_result:
            return
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
        self._leave_result()
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
        transform, choice = self.transform(), self.choice.get()
        if self.mode == "gif" and self.max_seconds:
            longest = max(ff.animation_seconds(s, e, self.speed, self.direction)
                          for s, e in self._sections())
            if longest > self.max_seconds:
                messagebox.showerror(
                    "Too long", f"This would be {longest:.1f} s long; animations can be at most "
                                f"{self.max_seconds} s (a boomerang counts twice). Pick another "
                                "direction, or shorten the clip in the main window.", parent=self)
                return
        rendered = None
        if self._result_valid() and not self._separate():
            rendered = self.result[0]
        options = {"fmt": self.fmt, "direction": self.direction}
        self.close(remove_temp=False)
        try:
            self.on_save(transform, choice, rendered, options)  # copies `rendered` if it uses it
        finally:
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def close(self, remove_temp=True):
        self.closed = True
        self.render_token += 1
        self.player.close()
        self.grab_release()
        self.destroy()
        if remove_temp:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
