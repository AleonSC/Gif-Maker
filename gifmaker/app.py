"""GifMaker: pick a section of a video and turn it into a GIF."""

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import ffmpeg_tools as ff

PREVIEW_W, PREVIEW_H = 720, 405
VIDEO_TYPES = [
    ("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.wmv *.flv *.m4v *.mpg *.mpeg *.ts *.gif"),
    ("All files", "*.*"),
]


class GifMakerApp:
    def __init__(self, root, ffmpeg):
        self.root = root
        self.ffmpeg = ffmpeg
        root.title("GifMaker")
        root.minsize(760, 640)
        self.video_path = None
        self.duration = 0.0
        self.video_fps = 0.0
        self.video_width = 0
        self.preview_image = None
        self.preview_job = None
        self.preview_seq = 0
        self.preview_queue = queue.Queue()
        self.busy = False
        self.cancel_requested = False

        self.position = tk.DoubleVar(value=0.0)
        self.start_text = tk.StringVar(value="0:00.000")
        self.end_text = tk.StringVar(value="0:00.000")
        self.fps = tk.IntVar(value=15)
        self.width = tk.IntVar(value=480)
        self.speed = tk.DoubleVar(value=1.0)
        self.colors = tk.IntVar(value=256)
        self.loop = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Open a video to get started.")

        self._build_ui()
        self._poll_preview_queue()

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        top = ttk.Frame(main)
        top.pack(fill="x")
        ttk.Button(top, text="Open video...", command=self.open_video).pack(side="left")
        self.file_label = ttk.Label(top, text="No video loaded", foreground="#666")
        self.file_label.pack(side="left", padx=10)

        self.canvas = tk.Canvas(main, width=PREVIEW_W, height=PREVIEW_H,
                                bg="#111", highlightthickness=0)
        self.canvas.pack(pady=8)
        self.canvas_text = self.canvas.create_text(
            PREVIEW_W // 2, PREVIEW_H // 2, fill="#888",
            text="Open a video, or drag the slider to scrub through it.")
        self.canvas_img = self.canvas.create_image(PREVIEW_W // 2, PREVIEW_H // 2)

        scrub = ttk.Frame(main)
        scrub.pack(fill="x")
        self.slider = ttk.Scale(scrub, from_=0, to=1, variable=self.position,
                                command=lambda _v: self._on_scrub())
        self.slider.pack(side="left", fill="x", expand=True)
        self.pos_label = ttk.Label(scrub, text="0:00.000", width=12, anchor="e")
        self.pos_label.pack(side="left", padx=(8, 0))

        step = ttk.Frame(main)
        step.pack(pady=4)
        for label, delta in (("<< 1s", -1), ("< frame", "-f"), ("frame >", "+f"), ("1s >>", 1)):
            ttk.Button(step, text=label, width=9,
                       command=lambda d=delta: self.step(d)).pack(side="left", padx=2)

        rng = ttk.LabelFrame(main, text="Clip range  (seconds or m:ss.sss)", padding=6)
        rng.pack(fill="x", pady=6)
        ttk.Label(rng, text="Start").grid(row=0, column=0, **pad)
        ttk.Entry(rng, textvariable=self.start_text, width=12).grid(row=0, column=1, **pad)
        ttk.Button(rng, text="Set to current", command=self.set_start).grid(row=0, column=2, **pad)
        ttk.Button(rng, text="Go", width=4,
                   command=lambda: self.goto(self.start_text)).grid(row=0, column=3, **pad)
        ttk.Label(rng, text="End").grid(row=0, column=4, **pad)
        ttk.Entry(rng, textvariable=self.end_text, width=12).grid(row=0, column=5, **pad)
        ttk.Button(rng, text="Set to current", command=self.set_end).grid(row=0, column=6, **pad)
        ttk.Button(rng, text="Go", width=4,
                   command=lambda: self.goto(self.end_text)).grid(row=0, column=7, **pad)
        self.length_label = ttk.Label(rng, text="Length: 0.00s")
        self.length_label.grid(row=0, column=8, **pad)
        for var in (self.start_text, self.end_text):
            var.trace_add("write", lambda *_: self._update_length())

        opts = ttk.LabelFrame(main, text="GIF options", padding=6)
        opts.pack(fill="x", pady=6)
        ttk.Label(opts, text="FPS").grid(row=0, column=0, **pad)
        ttk.Spinbox(opts, from_=1, to=50, textvariable=self.fps, width=6).grid(row=0, column=1, **pad)
        ttk.Label(opts, text="Width (px, 0 = original)").grid(row=0, column=2, **pad)
        ttk.Combobox(opts, textvariable=self.width, width=7,
                     values=(0, 240, 320, 480, 640, 800, 1080)).grid(row=0, column=3, **pad)
        ttk.Label(opts, text="Speed").grid(row=0, column=4, **pad)
        ttk.Combobox(opts, textvariable=self.speed, width=5,
                     values=(0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0)).grid(row=0, column=5, **pad)
        ttk.Label(opts, text="Colors").grid(row=0, column=6, **pad)
        ttk.Combobox(opts, textvariable=self.colors, width=5,
                     values=(256, 128, 64, 32, 16)).grid(row=0, column=7, **pad)
        ttk.Checkbutton(opts, text="Loop forever", variable=self.loop).grid(row=0, column=8, **pad)

        bottom = ttk.Frame(main)
        bottom.pack(fill="x", pady=(8, 0))
        self.make_btn = ttk.Button(bottom, text="Make GIF...", command=self.make_gif)
        self.make_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bottom, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(bottom, maximum=1.0)
        self.progress.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(main, textvariable=self.status, foreground="#444").pack(fill="x", pady=(6, 0))

        self.root.bind("<Left>", lambda e: self._key_step(e, "-f"))
        self.root.bind("<Right>", lambda e: self._key_step(e, "+f"))
        self.root.bind("<bracketleft>", lambda e: self._key_set(e, self.set_start))
        self.root.bind("<bracketright>", lambda e: self._key_set(e, self.set_end))

    # ----------------------------------------------------------- loading
    def open_video(self, path=None):
        if self.busy:
            return
        path = path or filedialog.askopenfilename(title="Open video", filetypes=VIDEO_TYPES)
        if not path:
            return
        try:
            duration, w, _h, fps = ff.probe(self.ffmpeg, path)
        except Exception as e:
            messagebox.showerror("Could not open video", str(e))
            return
        self.video_path = path
        self.duration = duration
        self.video_fps = fps or 30.0
        self.video_width = w
        self.file_label.config(text=f"{os.path.basename(path)}   ({ff.format_time(duration)}, "
                                    f"{w}x{_h}, {self.video_fps:g} fps)", foreground="#000")
        self.slider.config(to=max(duration, 0.001))
        self.position.set(0.0)
        self.start_text.set(ff.format_time(0))
        self.end_text.set(ff.format_time(min(duration, 3.0)))
        if w and w < self.width.get():
            self.width.set(0)
        self.status.set("Scrub to the part you want, set Start and End, then click Make GIF.")
        self._request_preview()

    # ----------------------------------------------------------- preview
    def _on_scrub(self):
        self.pos_label.config(text=ff.format_time(self.position.get()))
        if self.preview_job:
            self.root.after_cancel(self.preview_job)
        self.preview_job = self.root.after(60, self._request_preview)

    def _request_preview(self):
        self.preview_job = None
        if not self.video_path:
            return
        self.preview_seq += 1
        seq, t, path = self.preview_seq, self.position.get(), self.video_path
        self.pos_label.config(text=ff.format_time(t))

        def work():
            data = ff.grab_frame(self.ffmpeg, path, t, PREVIEW_W, PREVIEW_H)
            self.preview_queue.put((seq, data))

        threading.Thread(target=work, daemon=True).start()

    def _poll_preview_queue(self):
        latest = None
        try:
            while True:
                latest = self.preview_queue.get_nowait()
        except queue.Empty:
            pass
        if latest and latest[0] == self.preview_seq and latest[1]:
            try:
                self.preview_image = tk.PhotoImage(data=latest[1], format="png")
                self.canvas.itemconfig(self.canvas_img, image=self.preview_image)
                self.canvas.itemconfig(self.canvas_text, text="")
            except tk.TclError:
                pass
        self.root.after(40, self._poll_preview_queue)

    def step(self, delta):
        if not self.video_path:
            return
        if delta == "-f":
            delta = -1.0 / self.video_fps
        elif delta == "+f":
            delta = 1.0 / self.video_fps
        self.position.set(min(self.duration, max(0.0, self.position.get() + delta)))
        self._on_scrub()

    def goto(self, var):
        try:
            t = ff.parse_time(var.get())
        except ValueError:
            return
        self.position.set(min(self.duration, t))
        self._on_scrub()

    def _key_step(self, event, delta):
        if not isinstance(event.widget, (ttk.Entry, tk.Entry, ttk.Combobox, ttk.Spinbox)):
            self.step(delta)

    def _key_set(self, event, fn):
        if not isinstance(event.widget, (ttk.Entry, tk.Entry, ttk.Combobox, ttk.Spinbox)):
            fn()

    # ------------------------------------------------------------- range
    def set_start(self):
        self.start_text.set(ff.format_time(self.position.get()))

    def set_end(self):
        self.end_text.set(ff.format_time(self.position.get()))

    def _range(self):
        start = ff.parse_time(self.start_text.get())
        end = ff.parse_time(self.end_text.get())
        return start, end

    def _update_length(self):
        try:
            start, end = self._range()
            self.length_label.config(text=f"Length: {end - start:.2f}s")
        except ValueError:
            self.length_label.config(text="Length: ?")

    # -------------------------------------------------------------- make
    def make_gif(self):
        if self.busy:
            return
        if not self.video_path:
            messagebox.showinfo("GifMaker", "Open a video first.")
            return
        try:
            start, end = self._range()
        except ValueError:
            messagebox.showerror("GifMaker", "Start and End must be times like 12.5 or 1:02.25")
            return
        end = min(end, self.duration)
        if end <= start:
            messagebox.showerror("GifMaker", "End must be after Start.")
            return
        try:
            fps = int(self.fps.get())
            width = int(float(self.width.get()))
            speed = float(self.speed.get())
            colors = int(self.colors.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("GifMaker", "Check the GIF options: they must be numbers.")
            return
        if not (1 <= fps <= 50 and width >= 0 and 0.1 <= speed <= 10 and 2 <= colors <= 256):
            messagebox.showerror(
                "GifMaker",
                "Options out of range:\nFPS 1-50, Width 0 or more, Speed 0.1-10, Colors 2-256.")
            return
        if end - start > 60 and not messagebox.askyesno(
                "Long clip", f"This clip is {end - start:.0f} seconds long, which can make a "
                             "very large GIF. Continue?"):
            return

        base = os.path.splitext(os.path.basename(self.video_path))[0]
        default = f"{base}_{start:.1f}-{end:.1f}.gif".replace(":", "-")
        dst = filedialog.asksaveasfilename(
            title="Save GIF as", defaultextension=".gif", initialfile=default,
            initialdir=os.path.dirname(self.video_path),
            filetypes=[("GIF image", "*.gif")])
        if not dst:
            return

        cmd = ff.build_gif_command(self.ffmpeg, self.video_path, dst, start, end, fps,
                                   width, speed, colors, self.loop.get())
        out_seconds = (end - start) / speed
        self._set_busy(True)
        self.status.set("Making GIF...")

        def progress(frac):
            self.root.after(0, lambda: self.progress.config(value=frac))

        def work():
            err = ff.make_gif(cmd, out_seconds, progress, lambda: self.cancel_requested)
            self.root.after(0, lambda: self._done(err, dst))

        threading.Thread(target=work, daemon=True).start()

    def cancel(self):
        self.cancel_requested = True
        self.status.set("Cancelling...")

    def _set_busy(self, busy):
        self.busy = busy
        self.cancel_requested = False
        self.make_btn.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")
        self.progress.config(value=0)

    def _done(self, err, dst):
        self._set_busy(False)
        if err == "cancelled":
            self.status.set("Cancelled.")
            return
        if err:
            self.status.set("Failed.")
            messagebox.showerror("GIF failed", err[-1500:])
            return
        size_mb = os.path.getsize(dst) / (1024 * 1024)
        self.progress.config(value=1.0)
        self.status.set(f"Saved {dst}  ({size_mb:.2f} MB)")
        if messagebox.askyesno("Done", f"GIF saved ({size_mb:.2f} MB).\n\nShow it in its folder?"):
            show_in_folder(dst)


def show_in_folder(path):
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except OSError:
        pass


def main():
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    try:
        ffmpeg = ff.find_ffmpeg()
    except RuntimeError as e:
        root.withdraw()
        messagebox.showerror("GifMaker", str(e))
        return
    app = GifMakerApp(root, ffmpeg)
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        root.after(100, lambda: app.open_video(sys.argv[1]))
    root.mainloop()


if __name__ == "__main__":
    main()
