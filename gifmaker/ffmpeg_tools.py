"""Thin wrappers around ffmpeg for probing videos, grabbing frames and making GIFs."""

import base64
import os
import re
import shutil
import subprocess
import sys

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


def grab_frame(ffmpeg, path, seconds, max_w, max_h):
    """Return a PNG of the frame at `seconds`, base64-encoded for tk.PhotoImage."""
    vf = (
        f"scale='min({max_w},iw)':'min({max_h},ih)'"
        ":force_original_aspect_ratio=decrease"
    )
    proc = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-ss", f"{seconds:.3f}", "-i", path,
            "-frames:v", "1", "-vf", vf,
            "-f", "image2pipe", "-vcodec", "png", "-",
        ],
        capture_output=True,
        creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    return base64.b64encode(proc.stdout).decode("ascii")


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
