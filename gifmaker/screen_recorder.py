"""Recording the screen (or part of it) to a video, with the sound the computer plays.

The picture comes from ffmpeg's gdigrab (x11grab when running from source on
Linux), the sound from WASAPI loopback through PyAudioWPatch, recorded side by
side and joined when recording stops. gdigrab stamps each frame with the wall
clock, which is how the sound is lined up with the picture.
"""

import collections
import datetime
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import wave
from tkinter import filedialog, ttk

from . import ffmpeg_tools as ff
from . import files
from . import theme
from . import ui_thread
from .loupe import Loupe, snap
from .tooltip import Tooltip

FPS_CHOICES = (15, 24, 30, 60)
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_START = re.compile(r"Duration: N/A, start: (\d+\.\d+)")
_HOLE = "#ff00fe"  # the see-through colour of the area picker


# ------------------------------------------------------------- screens
def _user32():
    import ctypes

    return ctypes.windll.user32


def virtual_screen(root):
    """(x, y, width, height) of all the monitors together, in screen pixels."""
    if sys.platform == "win32":
        u = _user32()
        return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
                u.GetSystemMetrics(78), u.GetSystemMetrics(79))
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def monitor_of(window):
    """(x, y, width, height) of the monitor most of window is on."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        u = _user32()
        u.MonitorFromWindow.restype = ctypes.c_void_p
        u.MonitorFromWindow.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        try:
            hwnd = int(window.wm_frame(), 16)
            monitor = u.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(info)
            if monitor and u.GetMonitorInfoW(monitor, ctypes.byref(info)):
                r = info.rcMonitor
                return r.left, r.top, r.right - r.left, r.bottom - r.top
        except (OSError, ValueError, tk.TclError):
            pass
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def hide_from_capture(window):
    """Keep window out of recordings and screenshots (Windows 10 2004 and later).
    Returns False if that isn't possible here."""
    if sys.platform != "win32":
        return False
    try:
        window.update_idletasks()
        hwnd = int(window.wm_frame(), 16)
        return bool(_user32().SetWindowDisplayAffinity(hwnd, 0x11))  # WDA_EXCLUDEFROMCAPTURE
    except (OSError, ValueError, tk.TclError):
        return False


def _even_area(area, screen):
    """area clipped to the screen, with an even width and height (video needs them)."""
    x, y, w, h = area
    sx, sy, sw, sh = screen
    x, y = max(x, sx), max(y, sy)
    w, h = min(w, sx + sw - x), min(h, sy + sh - y)
    return x, y, max(2, w // 2 * 2), max(2, h // 2 * 2)


# --------------------------------------------------------------- sound
def sound_available():
    """None if the computer's sound can be recorded, else why not."""
    if sys.platform != "win32":
        return "Recording the computer's sound only works on Windows."
    try:
        import pyaudiowpatch as pa
    except ImportError:
        return "The sound recorder isn't installed."
    p = pa.PyAudio()
    try:
        p.get_default_wasapi_loopback()
        return None
    except (OSError, LookupError) as e:
        return f"No speakers to record from ({e})."
    finally:
        p.terminate()


class LoopbackSound:
    """Records what the computer plays (the default speakers) to a WAV file.

    Windows sends nothing while nothing plays, so silence is filled in from the clock
    to keep the sound in step with the picture."""

    def __init__(self, path):
        import pyaudiowpatch as pa

        self.pa_module = pa
        self.pa = pa.PyAudio()
        try:
            device = self.pa.get_default_wasapi_loopback()
            self.rate = int(device["defaultSampleRate"])
            self.channels = max(1, int(device["maxInputChannels"]))
            self.wav = wave.open(path, "wb")
            self.wav.setnchannels(self.channels)
            self.wav.setsampwidth(2)
            self.wav.setframerate(self.rate)
            self.stream = self.pa.open(format=pa.paInt16, channels=self.channels, rate=self.rate,
                                       input=True, input_device_index=device["index"],
                                       frames_per_buffer=self.rate // 20,
                                       stream_callback=self._callback, start=False)
        except Exception:
            self.pa.terminate()
            raise
        self.lock = threading.Lock()
        self.written = 0
        self.closed = False
        self.started_at = None  # wall clock, for lining up with the picture
        self.t0 = None

    def start(self):
        self.started_at, self.t0 = time.time(), time.perf_counter()
        self.stream.start_stream()

    def _fill_to(self, frames):
        gap = frames - self.written
        if gap > 0:
            self.wav.writeframesraw(b"\0" * (gap * self.channels * 2))
            self.written += gap

    def _callback(self, data, frames, _info, _status):
        with self.lock:
            if self.closed:
                return None, self.pa_module.paComplete
            due = int((time.perf_counter() - self.t0) * self.rate) - frames
            if due - self.written > self.rate // 10:  # a silent stretch
                self._fill_to(due)
            self.wav.writeframesraw(data)
            self.written += frames
        return None, self.pa_module.paContinue

    def stop(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.t0 is not None:
                self._fill_to(int((time.perf_counter() - self.t0) * self.rate))
        try:
            self.stream.stop_stream()
            self.stream.close()
        except OSError:
            pass
        self.pa.terminate()
        self.wav.close()


# ----------------------------------------------------------- recording
def default_folder():
    videos = os.path.join(os.path.expanduser("~"), "Videos")
    return os.path.join(videos if os.path.isdir(videos) else tempfile.gettempdir(),
                        "GifMaker recordings")


def grab_input(area, fps, mouse):
    """ffmpeg's input arguments for grabbing area of the screen."""
    x, y, w, h = area
    if sys.platform == "win32":
        return ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1" if mouse else "0",
                "-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}",
                "-i", "desktop"]
    return ["-f", "x11grab", "-framerate", str(fps), "-draw_mouse", "1" if mouse else "0",
            "-video_size", f"{w}x{h}", "-i", f"{os.environ.get('DISPLAY', ':0')}+{x},{y}"]


def cursor_monitor(root):
    """(x, y, width, height) of the monitor the mouse is on (GifMaker's if unknown)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        u = _user32()
        try:
            pt = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            u.MonitorFromPoint.restype = ctypes.c_void_p
            u.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
            u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            monitor = u.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(info)
            if monitor and u.GetMonitorInfoW(monitor, ctypes.byref(info)):
                r = info.rcMonitor
                return r.left, r.top, r.right - r.left, r.bottom - r.top
        except (OSError, ValueError):
            pass
    return monitor_of(root)


def take_screenshot(ffmpeg, area, folder, on_done):
    """Save a PNG of area (x, y, w, h) in folder, on a background thread.
    on_done(path, error) runs on the Tk thread."""
    def work():
        try:
            os.makedirs(folder, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H-%M-%S")
            out = files.free_name(folder, f"Screenshot {stamp}.png")
            cmd = [ffmpeg, "-y", "-loglevel", "error", *grab_input(area, 1, True),
                   "-frames:v", "1", out]
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                                  timeout=20, creationflags=_NO_WINDOW)
            if proc.returncode != 0 or not os.path.exists(out):
                raise OSError((proc.stderr or "ffmpeg couldn't grab the screen").strip()[-300:])
            result = (out, None)
        except (OSError, subprocess.SubprocessError) as e:
            result = (None, str(e))
        ui_thread.call(lambda: on_done(*result))

    threading.Thread(target=work, daemon=True).start()


class Recording:
    """One recording: start() it, then stop() (or cancel()). on_done(path, error) runs
    on the Tk thread once the video is saved (path None if cancelled or failed)."""

    def __init__(self, ffmpeg, area, fps, mouse, sound, out_path, on_done):
        self.ffmpeg, self.area, self.fps, self.mouse = ffmpeg, area, fps, mouse
        self.want_sound, self.out_path, self.on_done = sound, out_path, on_done
        self.temp = tempfile.mkdtemp(prefix="gifmaker-rec-")
        self.video = os.path.join(self.temp, "picture.mkv")
        self.wav = os.path.join(self.temp, "sound.wav")
        self.sound = None
        self.sound_error = None
        self.proc = None
        self.video_start = None  # wall clock of the first frame
        self.launched = None
        self.log = collections.deque(maxlen=30)
        self.started = None
        self.finished = False
        self.cancelled = False

    def start(self):
        """Raises OSError if ffmpeg can't be started."""
        if self.want_sound:
            try:
                self.sound = LoopbackSound(self.wav)
                self.sound.start()
            except Exception as e:  # recording without sound beats not recording
                self.sound, self.sound_error = None, str(e) or e.__class__.__name__
        cmd = [self.ffmpeg, "-hide_banner", "-y", "-thread_queue_size", "512",
               *grab_input(self.area, self.fps, self.mouse),
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
               self.video]
        self.launched = time.time()
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
        except OSError:
            if self.sound:
                self.sound.stop()
            raise
        self.started = time.monotonic()
        threading.Thread(target=self._read_log, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def elapsed(self):
        return time.monotonic() - self.started if self.started else 0.0

    def _read_log(self):
        buf = b""
        for chunk in iter(lambda: self.proc.stderr.read1(4096), b""):
            buf += chunk
            *lines, buf = re.split(rb"[\r\n]", buf)
            for raw in lines:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                self.log.append(line)
                m = self.video_start is None and _START.search(line)
                if m:
                    self.video_start = float(m.group(1))

    def _watch(self):
        """ffmpeg stopping by itself means something went wrong."""
        self.proc.wait()
        if not self.finished:
            self._finish()

    def stop(self):
        threading.Thread(target=self._stop, daemon=True).start()

    def cancel(self):
        self.cancelled = True
        self.stop()

    def abort(self):
        """GifMaker is closing: stop at once and throw the recording away."""
        self.cancelled = self.finished = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if self.sound:
            self.sound.stop()
        self._clean()

    def _stop(self):
        try:
            self.proc.stdin.write(b"q")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def _finish(self):
        self.finished = True
        if self.sound:
            self.sound.stop()
        error, path = None, None
        if self.cancelled:
            pass
        elif not os.path.exists(self.video) or os.path.getsize(self.video) < 1024:
            error = "The screen couldn't be recorded.\n\n" + "\n".join(
                l for l in list(self.log)[-6:] if not l.startswith("frame="))
        else:
            try:
                self._join()
                path = self.out_path
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                error = f"The recording couldn't be saved: {e}"
        self._clean()
        ui_thread.call(lambda: self.on_done(path, error))

    def _join(self):
        """Put the picture (and sound, lined up) into the final mp4."""
        os.makedirs(os.path.dirname(self.out_path), exist_ok=True)
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", self.video]
        has_sound = self.sound is not None and self.sound.written > 0
        if has_sound:
            start = self.video_start or (self.launched + 0.3)
            offset = start - self.sound.started_at  # how much sound came before frame one
            if offset >= 0:
                cmd += ["-ss", f"{offset:.3f}", "-i", self.wav]
            else:
                cmd += ["-itsoffset", f"{-offset:.3f}", "-i", self.wav]
            # padded with silence, then cut to the picture's length (-shortest
            # never ends with apad while the picture is only copied)
            length = ff.probe(self.ffmpeg, self.video).duration
            cmd += ["-map", "0:v", "-map", "1:a", "-c:v", "copy", "-af", "apad",
                    "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-t", f"{length:.3f}"]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-movflags", "+faststart", self.out_path]
        result = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
        if result.returncode != 0 or not os.path.exists(self.out_path):
            raise OSError(result.stderr.decode("utf-8", "replace").strip()[-300:] or
                          f"ffmpeg stopped (code {result.returncode})")

    def _clean(self):
        for name in (self.video, self.wav):
            try:
                os.remove(name)
            except OSError:
                pass
        try:
            os.rmdir(self.temp)
        except OSError:
            pass


# ------------------------------------------------------------- windows
def monitors(root):
    """(x, y, width, height) of each monitor, in screen pixels."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        found = []
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                  ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def add(_monitor, _dc, rect, _data):
            r = rect.contents
            found.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
            return True

        try:
            _user32().EnumDisplayMonitors(None, None, proc(add), 0)
        except (OSError, AttributeError):
            found = []
        if found:
            return found
    return [virtual_screen(root)]


def grab_screen_ppm(ffmpeg, area):
    """A picture of area (x, y, w, h) of the screen as PPM bytes, or None."""
    cmd = [ffmpeg, "-loglevel", "error", *grab_input(area, 30, False), "-frames:v", "1",
           "-f", "image2pipe", "-vcodec", "ppm", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=10, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


class AreaPicker(tk.Toplevel):
    """Darkens every screen; drag a rectangle to pick the part to record.
    on_pick((x, y, w, h)) gets it in screen pixels, or None if cancelled (Esc).

    A magnifier beside the pointer shows exactly where it is (from a picture of the
    screen taken just before), and the corners snap to the edges of the monitors.
    """

    def __init__(self, root, on_pick, ffmpeg=None):
        super().__init__(root)
        self.withdraw()
        self.root, self.on_pick = root, on_pick
        self.vx, self.vy, self.vw, self.vh = virtual_screen(root)
        self.screens = monitors(root)
        self.shot = None  # the picture of the screen, for the magnifier
        self.loupe = Loupe(self)
        if ffmpeg:
            area = (self.vx, self.vy, self.vw, self.vh)
            threading.Thread(target=lambda: self._grabbed(grab_screen_ppm(ffmpeg, area)),
                             daemon=True).start()
        else:
            self._open()

    def _grabbed(self, data):
        ui_thread.call(lambda: self._open(data))

    def _open(self, data=None):
        if data:
            try:
                self.shot = tk.PhotoImage(master=self, data=data, format="ppm")
            except tk.TclError:
                self.shot = None
        root, vw, vh = self.root, self.vw, self.vh
        self.overrideredirect(True)
        # Across every monitor: Tk otherwise caps a window at the main monitor's size,
        # and "+-1920" (not "-1920", which counts from the right edge) is left of it.
        self.maxsize(vw, vh)
        self.geometry(f"{vw}x{vh}+{self.vx}+{self.vy}")
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", 0.45)
            self.attributes("-transparentcolor", _HOLE)
        except tk.TclError:
            pass
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        # the hint goes at the top of the main monitor, which starts at (0, 0)
        self.canvas.create_text(-self.vx + root.winfo_screenwidth() // 2, -self.vy + 60,
                                fill="white", font=("Segoe UI", 16),
                                text="Drag over the part of the screen to record  ·  "
                                     "Esc or right-click to cancel")
        self.start = None
        self.rect = self.label = None
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Leave>", lambda e: self.loupe.hide())
        self.bind("<Escape>", lambda e: self._done(None))
        self.canvas.bind("<Button-3>", lambda e: self._done(None))
        self.deiconify()
        self.focus_force()
        self.grab_set()

    def _point(self, event):
        """The pointer in canvas pixels, pulled onto a monitor's edge when near one."""
        xs = [e - self.vx for sx, _, sw, _ in self.screens for e in (sx, sx + sw)]
        ys = [e - self.vy for _, sy, _, sh in self.screens for e in (sy, sy + sh)]
        return snap(event.x, xs), snap(event.y, ys)

    def _magnify(self, x, y, label=""):
        if self.shot is None:
            return
        sx, sy = x + self.vx, y + self.vy  # on the screen
        bounds = next(((mx, my, mw, mh) for mx, my, mw, mh in self.screens
                       if mx <= sx < mx + mw and my <= sy < my + mh),
                      (self.vx, self.vy, self.vw, self.vh))
        # (a point on the right or bottom edge shows the last pixel inside it)
        self.loupe.show(self.shot, min(x, self.vw - 1), min(y, self.vh - 1), sx, sy, label,
                        bounds)

    def _hover(self, event):
        if not self.start:
            x, y = self._point(event)
            self._magnify(x, y, f"{x + self.vx}, {y + self.vy}")

    def _press(self, event):
        self.start = self._point(event)

    def _box(self, event):
        (x0, y0), (x1, y1) = self.start, self._point(event)
        return min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)

    def _drag(self, event):
        if not self.start:
            return
        x, y, w, h = self._box(event)
        self.canvas.delete("box")
        self.canvas.create_rectangle(x, y, x + w, y + h, fill=_HOLE, outline="white", width=2,
                                     tags="box")
        self.canvas.create_text(x + 4, y - 4, anchor="sw", fill="white", tags="box",
                                text=f"{w // 2 * 2} × {h // 2 * 2}")
        self._magnify(*self._point(event), f"{w // 2 * 2} × {h // 2 * 2}")

    def _release(self, event):
        if not self.start:
            return
        x, y, w, h = self._box(event)
        self._done((x + self.vx, y + self.vy, w, h) if w >= 16 and h >= 16 else None)

    def _done(self, area):
        self.loupe.destroy()
        self.grab_release()
        self.destroy()
        self.on_pick(area)


class StopBar(tk.Toplevel):
    """The small always-on-top bar shown while recording (left out of the video)."""

    def __init__(self, root, recording, near, on_stop, on_cancel):
        super().__init__(root)
        self.recording = recording
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        theme.style_window(self)
        frame = ttk.Frame(self, padding=(10, 6))
        frame.pack()
        dot = tk.Canvas(frame, width=12, height=12, highlightthickness=0, bg=theme.colors["bg"])
        dot.create_oval(1, 1, 11, 11, fill="#e53935", outline="")
        dot.pack(side="left", padx=(0, 6))
        self.time = ttk.Label(frame, text="0:00", width=6)
        self.time.pack(side="left")
        ttk.Button(frame, text="Stop and open", command=on_stop).pack(side="left", padx=(6, 4))
        cancel = ttk.Button(frame, text="Discard", command=on_cancel)
        cancel.pack(side="left")
        Tooltip(cancel, "Stop and throw the recording away")
        self.update_idletasks()
        x, y, w, _ = near
        bw, bh = self.winfo_reqwidth(), self.winfo_reqheight()
        top = y - bh - 6  # just above the area, or inside its top if there's no room
        if top < virtual_screen(root)[1]:
            top = y + 6
        self.geometry(f"+{x + (w - bw) // 2}+{top}")
        self.hidden = hide_from_capture(self)
        self._tick()

    def _tick(self):
        if not self.winfo_exists():
            return
        s = int(self.recording.elapsed())
        self.time.config(text=f"{s // 60}:{s % 60:02d}")
        self.after(500, self._tick)


class RecorderWindow(tk.Toplevel):
    """Choose what to record and start. on_saved(path) gets each finished video."""

    def __init__(self, app, on_saved):
        super().__init__(app.root)
        self.app, self.on_saved = app, on_saved
        self.title("Record screen")
        self.transient(app.root)
        self.resizable(False, False)
        theme.style_window(self)
        self.recording = None
        self.bar = None
        saved_area = theme.load_setting("record_area", None)
        self.area = tuple(saved_area) if isinstance(saved_area, list) and len(saved_area) == 4 \
            else None
        self.what = tk.StringVar(value=theme.load_setting("record_what", "screen"))
        if self.what.get() == "area" and not self.area:
            self.what.set("screen")
        self.fps = tk.StringVar()
        self.mouse = tk.BooleanVar()
        self.sound = tk.BooleanVar()
        self.hide_app = tk.BooleanVar()
        self.folder = tk.StringVar()
        self.no_sound = sound_available()
        self._load_settings()
        self.status = tk.StringVar()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())

    def _load_settings(self):
        """The recording settings, as set in Settings › Recording."""
        self.fps.set(str(theme.load_setting("record_fps", 30)))
        self.mouse.set(bool(theme.load_setting("record_mouse", True)))
        self.sound.set(bool(theme.load_setting("record_sound", True)) and not self.no_sound)
        self.hide_app.set(bool(theme.load_setting("record_hide", True)))
        self.folder.set(theme.load_setting("record_folder", "") or default_folder())

    def _summary(self):
        parts = [f"{self.fps.get()} fps",
                 "with sound" if self.sound.get() else "no sound",
                 "mouse shown" if self.mouse.get() else "no mouse",
                 f"saved in {self.folder.get()}"]
        return "  \u00B7  ".join(parts)

    def _build(self):
        """What to record, the settings in a line (changed in Settings), and the buttons."""
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)
        what = ttk.LabelFrame(main, text="What to record", padding=(10, 6))
        what.pack(fill="x")
        ttk.Radiobutton(what, text="The whole screen (the one GifMaker is on)",
                        variable=self.what, value="screen").pack(anchor="w", pady=1)
        row = ttk.Frame(what)
        row.pack(anchor="w", pady=(2, 0))
        self.area_radio = ttk.Radiobutton(row, variable=self.what, value="area",
                                          text=self._area_text())
        self.area_radio.pack(side="left")
        ttk.Button(row, text="Pick an area...", command=self.pick_area).pack(side="left",
                                                                           padx=(8, 0))
        info = ttk.Frame(main)
        info.pack(fill="x", pady=(10, 0))
        self.summary = ttk.Label(info, text=self._summary(), style="Muted.TLabel",
                                 wraplength=380, justify="left")
        self.summary.pack(side="left", fill="x", expand=True)
        change = ttk.Button(info, text="Settings...",
                            command=lambda: self.app.open_settings("Recording"))
        change.pack(side="right", padx=(8, 0))
        Tooltip(change, "Frame rate, sound, mouse pointer, where recordings are saved, "
                        "and the screenshot key")
        self.bind("<FocusIn>", lambda e: self._settings_changed())
        ttk.Label(main, textvariable=self.status, style="Muted.TLabel", wraplength=460,
                  justify="left").pack(anchor="w", pady=(6, 0))
        buttons = ttk.Frame(main)
        buttons.pack(fill="x", pady=(10, 0))
        shot = ttk.Button(buttons, text="Take screenshot", command=self.screenshot)
        shot.pack(side="left")
        Tooltip(shot, "Save a picture of what's chosen above (as a PNG, in the same folder)")
        ttk.Button(buttons, text="Close", command=self.close).pack(side="right")
        self.start_btn = ttk.Button(buttons, text="Start recording", command=self.start)
        self.start_btn.pack(side="right", padx=(0, 6))

    def _settings_changed(self):
        self._load_settings()
        self.summary.config(text=self._summary())

    def _chosen_area(self):
        root = self.app.root
        area = self.area if self.what.get() == "area" and self.area else monitor_of(root)
        return _even_area(area, virtual_screen(root))

    def screenshot(self):
        """Save a picture of the chosen screen or area, with this window out of the way."""
        self._load_settings()
        area, folder = self._chosen_area(), self.folder.get().strip() or default_folder()
        theme.save_setting("record_what", self.what.get())
        self.withdraw()

        def done(path, error):
            self.deiconify()
            self.lift()
            self.status.set(f"Couldn't take the screenshot: {error}" if error
                            else f"Saved {path}")

        self.after(300, lambda: take_screenshot(self.app.ffmpeg, area, folder, done))

    def _area_text(self):
        if not self.area:
            return "An area of the screen"
        x, y, w, h = self.area
        return f"An area: {w // 2 * 2} × {h // 2 * 2} at {x}, {y}"

    def _browse(self):
        folder = filedialog.askdirectory(parent=self, title="Save recordings in",
                                         initialdir=self.folder.get() or None)
        if folder:
            self.folder.set(os.path.normpath(folder))

    def pick_area(self):
        self.withdraw()
        self.app.root.after(150, lambda: AreaPicker(self.app.root, self._picked, self.app.ffmpeg))

    def _picked(self, area):
        self.deiconify()
        self.lift()
        if area:
            self.area = area
            theme.save_setting("record_area", list(area))
            self.area_radio.config(text=self._area_text())
            self.what.set("area")

    def _save_settings(self):
        theme.save_setting("record_what", self.what.get())

    def start(self):
        if self.recording:
            return
        self._load_settings()
        folder = self.folder.get().strip() or default_folder()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            self.status.set(f"Can't use that folder: {e}")
            return
        self._save_settings()
        root = self.app.root
        area = self._chosen_area()
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        out = os.path.join(folder, f"Recording {stamp}.mp4")
        self.recording = Recording(self.app.ffmpeg, area, int(self.fps.get()), self.mouse.get(),
                                   self.sound.get(), out, self._done)
        self.withdraw()
        self.app_hidden = self.hide_app.get()
        if self.app_hidden:
            root.iconify()
        # Give the windows a moment to get out of the way.
        root.after(400 if self.app_hidden else 150, lambda: self._go(area))

    def _go(self, area):
        try:
            self.recording.start()
        except OSError as e:
            self.recording = None
            self._show_again()
            self.status.set(f"Couldn't start recording: {e}")
            return
        self.bar = StopBar(self.app.root, self.recording, area, self.stop, self.discard)

    def stop(self):
        if self.recording and not self.recording.finished:
            self._close_bar()
            self.recording.stop()

    def discard(self):
        if self.recording and not self.recording.finished:
            self._close_bar()
            self.recording.cancel()

    def _close_bar(self):
        if self.bar is not None:
            self.bar.destroy()
            self.bar = None

    def _show_again(self):
        root = self.app.root
        if getattr(self, "app_hidden", False):
            root.deiconify()
        if self.winfo_exists():
            self.deiconify()
            self.lift()

    def _done(self, path, error):
        recording, self.recording = self.recording, None
        self._close_bar()
        if not self.winfo_exists():
            return
        self._show_again()
        if error:
            self.status.set(error)
            return
        if not path:
            self.status.set("Recording discarded.")
            return
        note = ""
        if recording and recording.want_sound and recording.sound_error:
            note = f" (without sound: {recording.sound_error})"
        self.status.set(f"Saved {os.path.basename(path)}{note}.")
        self.on_saved(path, note)
        self.close()

    def close(self):
        if self.recording and not self.recording.finished:
            self.discard()
            return
        self._close_bar()
        self.destroy()

    def shutdown(self):
        """GifMaker is closing."""
        if self.recording:
            self.recording.abort()
            self.recording = None
        self._close_bar()
