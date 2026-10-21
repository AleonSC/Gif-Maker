"""Thin wrappers around ffmpeg for probing videos, grabbing frames and making GIFs."""

import os
import queue
import re
import shutil
import subprocess
import sys
import threading

# Stop ffmpeg from flashing a console window when running as a windowed .exe.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def find_ffmpeg():
    """Return the path of an ffmpeg binary: the bundled one first, then PATH."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    raise RuntimeError(
        "ffmpeg was not found. Install it or run the packaged GifMaker.exe, "
        "which includes it."
    )


def parse_time(text):
    """Parse '83.5', '1:23.5' or '0:01:23.5' into seconds."""
    text = text.strip()
    if not text:
        raise ValueError("empty time")
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    if seconds < 0:
        raise ValueError("negative time")
    return seconds


def format_time(seconds):
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h >= 1:
        return f"{int(h)}:{int(m):02d}:{s:06.3f}"
    return f"{int(m)}:{s:06.3f}"


def probe(ffmpeg, path):
    """Return (duration_seconds, width, height, fps) for a video file."""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", path],
        capture_output=True,
        text=True,
        errors="replace",
        creationflags=_NO_WINDOW,
    )
    info = proc.stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", info)
    if not m:
        raise RuntimeError("Could not read this file as a video.\n\n" + info[-800:])
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    width = height = 0
    fps = 0.0
    video_line = next((l for l in info.splitlines() if "Video:" in l), "")
    if not video_line:
        raise RuntimeError("This file has no video stream.")
    m = re.search(r"\b(\d{2,5})x(\d{2,5})\b", video_line)
    if m:
        width, height = int(m.group(1)), int(m.group(2))
    m = re.search(r"([\d.]+)\s*fps", video_line)
    if m:
        fps = float(m.group(1))
    return duration, width, height, fps


def grab_frame(ffmpeg, path, seconds, max_w, max_h, fast=False):
    """Return the frame at `seconds` as PPM bytes for tk.PhotoImage, or None.

    fast=True jumps to the nearest keyframe at or before `seconds` instead of
    decoding up to the exact frame: much quicker, but only approximate.
    """
    vf = (
        f"scale='min({max_w},iw)':'min({max_h},ih)'"
        ":force_original_aspect_ratio=decrease"
    )
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if fast:
        cmd += ["-noaccurate_seek", "-skip_frame", "nokey"]
        vf += ":flags=fast_bilinear"
    cmd += ["-ss", f"{seconds:.3f}"]
    if fast:
        # Without this ffmpeg drops a keyframe that lies before the seek point.
        cmd += ["-copyts"]
    cmd += [
        "-i", path, "-an", "-sn",
        "-frames:v", "1", "-vf", vf,
        "-f", "image2pipe", "-vcodec", "ppm", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
    if proc.returncode != 0 or not proc.stdout:
        return grab_frame(ffmpeg, path, seconds, max_w, max_h) if fast else None
    return proc.stdout


def iter_keyframes(ffmpeg, path, width, height, min_gap, should_stop):
    """Yield (seconds, rgb24 bytes) for keyframes at least min_gap seconds apart.

    Only keyframes are decoded, so this is fast even for long videos. Used to
    build the thumbnail cache that makes scrubbing instant.
    """
    vf = (
        f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{min_gap:.3f})',"
        f"scale={width}:{height}:flags=fast_bilinear,showinfo"
    )
    proc = subprocess.Popen(
        [
            ffmpeg, "-hide_banner", "-nostats", "-skip_frame", "nokey",
            "-i", path, "-an", "-sn", "-dn", "-vf", vf, "-vsync", "passthrough",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )
    times = queue.Queue()

    def read_times():
        for line in proc.stderr:
            m = re.search(rb"pts_time:\s*(-?[\d.]+)", line)
            if m and b"showinfo" in line:
                times.put(float(m.group(1)))

    threading.Thread(target=read_times, daemon=True).start()
    frame_size = width * height * 3
    try:
        while not should_stop():
            buf = proc.stdout.read(frame_size)
            if len(buf) < frame_size:
                break
            try:
                t = times.get(timeout=5)
            except queue.Empty:
                break
            yield t, buf
    finally:
        proc.kill()
        proc.wait()


def build_gif_command(ffmpeg, src, dst, start, end, fps, width, speed=1.0,
                      colors=256, loop=True, dither="sierra2_4a"):
    """Build the ffmpeg command line for a high-quality two-pass-palette GIF."""
    duration = end - start
    filters = []
    if speed != 1.0:
        filters.append(f"setpts=PTS/{speed}")
    filters.append(f"fps={fps}")
    if width > 0:
        filters.append(f"scale={width}:-2:flags=lanczos")
    chain = ",".join(filters)
    graph = (
        f"{chain},split[a][b];"
        f"[a]palettegen=max_colors={colors}:stats_mode=diff[p];"
        f"[b][p]paletteuse=dither={dither}:diff_mode=rectangle"
    )
    return [
        ffmpeg, "-hide_banner", "-y", "-nostats", "-loglevel", "error",
        "-progress", "pipe:1",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", src,
        "-filter_complex", graph,
        "-loop", "0" if loop else "-1",
        dst,
    ]


def make_gif(cmd, output_seconds, on_progress=None, should_cancel=None):
    """Run a command from build_gif_command, reporting progress in 0..1.

    Returns None on success, "cancelled" if cancelled, or an error message.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        creationflags=_NO_WINDOW,
    )
    for line in proc.stdout:
        if should_cancel and should_cancel():
            proc.kill()
            proc.wait()
            dst = cmd[-1]
            if os.path.exists(dst):
                try:
                    os.remove(dst)
                except OSError:
                    pass
            return "cancelled"
        key, _, value = line.strip().partition("=")
        if key == "out_time_us" and on_progress and output_seconds > 0:
            try:
                done = int(value) / 1_000_000
            except ValueError:
                continue
            on_progress(min(1.0, done / output_seconds))
    err = proc.stderr.read()
    proc.wait()
    if proc.returncode != 0:
        return err.strip() or f"ffmpeg exited with code {proc.returncode}"
    if on_progress:
        on_progress(1.0)
    return None


def fit_size(width, height, max_w, max_h):
    """Scale (width, height) down to fit in max_w x max_h, keeping even sizes."""
    if width <= 0 or height <= 0:
        return max_w, max_h
    scale = min(1.0, max_w / width, max_h / height)
    w = max(2, int(width * scale) // 2 * 2)
    h = max(2, int(height * scale) // 2 * 2)
    return w, h


def open_raw_stream(ffmpeg, path, start, width, height, fps, duration=None):
    """Start ffmpeg decoding the video as raw RGB frames of width x height on stdout."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += [
        "-i", path, "-an", "-sn",
        "-vf", f"fps={fps},scale={width}:{height}:flags=bilinear",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )


def save_frame(ffmpeg, path, seconds, dst):
    """Save the full-resolution frame at `seconds` to dst (format from its extension).

    Returns None on success or an error message.
    """
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{seconds:.3f}", "-i", path, "-frames:v", "1", "-update", "1",
    ]
    if os.path.splitext(dst)[1].lower() in (".jpg", ".jpeg"):
        cmd += ["-q:v", "2"]
    cmd.append(dst)
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                          creationflags=_NO_WINDOW)
    if proc.returncode != 0 or not os.path.exists(dst):
        return proc.stderr.strip() or "ffmpeg could not save the frame."
    return None
