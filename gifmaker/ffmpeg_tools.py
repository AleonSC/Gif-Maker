"""Thin wrappers around ffmpeg for probing videos, grabbing frames and making GIFs."""

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from collections import namedtuple

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


VideoInfo = namedtuple("VideoInfo", "duration width height fps has_audio")

# Formats without a keyframe index (.ts and .mpg recordings, raw streams): a seek
# into them can land between keyframes, and ffmpeg 7 then loses the pictures up to
# the next keyframe; a short cut can come out with none at all ("received no
# packets"). For these, seek PREROLL seconds early, keep the file's own timestamps
# (-copyts) and trim at the exact time after decoding. probe() records which files
# need it, with the file's starting timestamp.
PREROLL_FORMATS = {"mpegts", "mpeg", "mpegvideo", "h264", "hevc", "flv"}
PREROLL = 10.0
_preroll = {}  # path -> the file's start timestamp, for files that need pre-roll


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def _seek(path, start):
    """How to read path from `start` (seconds into the video).

    Returns (input args, extra seconds read before start, origin): origin is the
    file's own timestamp for `start` when pre-roll is used (trim there), else None.
    """
    file_start = _preroll.get(_key(path))
    if file_start is None:
        return ["-ss", f"{start:.3f}"], 0.0, None
    seek = max(0.0, start - PREROLL)
    return ["-ss", f"{seek:.3f}", "-copyts"], start - seek, file_start + start


def _trim_video(origin):
    return f"trim=start={origin:.6f},setpts=PTS-STARTPTS" if origin is not None else ""


def _trim_audio(origin):
    return f"atrim=start={origin:.6f},asetpts=PTS-STARTPTS" if origin is not None else ""


def probe(ffmpeg, path):
    """Return a VideoInfo for a video file.

    width and height are as displayed: swapped for phone videos flagged as rotated.
    """
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
    m = (re.search(r"rotation of (-?[\d.]+) degrees", info)
         or re.search(r"^\s*rotate\s*:\s*(-?\d+)", info, re.MULTILINE))
    if m and round(abs(float(m.group(1)))) % 180 == 90:
        width, height = height, width
    has_audio = any("Audio:" in l for l in info.splitlines())
    m = re.search(r"Input #0, (.+?), from ", info)
    formats = set(m.group(1).split(",")) if m else set()
    if formats & PREROLL_FORMATS:
        _preroll[_key(path)] = _first_frame_time(ffmpeg, path, info)
    else:
        _preroll.pop(_key(path), None)
    return VideoInfo(duration, width, height, fps, has_audio)


def _first_frame_time(ffmpeg, path, info):
    """The file's own timestamp of its first picture, where the video really starts.

    The "start:" ffmpeg reports can be earlier: it counts the sound's priming
    samples, which the decoder drops.
    """
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-copyts", "-i", path, "-map", "0:v:0", "-frames:v", "1",
         "-vf", "showinfo", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace", creationflags=_NO_WINDOW)
    m = re.search(r"pts_time:\s*(-?[\d.]+)", proc.stderr)
    if m:
        return float(m.group(1))
    m = re.search(r"start:\s*(-?[\d.]+)", info)
    return float(m.group(1)) if m else 0.0


def transform_filters(rotate=0, flip=False, crop=None):
    """ffmpeg filters for a user rotation (degrees clockwise), mirror and crop.

    crop is (x, y, w, h) as fractions of the rotated frame, or None for no crop.
    Returns a filter chain string, possibly empty.
    """
    filters = []
    rotate %= 360
    if rotate == 90:
        filters.append("transpose=1")
    elif rotate == 180:
        filters.append("hflip,vflip")
    elif rotate == 270:
        filters.append("transpose=2")
    if flip:
        filters.append("hflip")
    if crop and tuple(crop) != (0, 0, 1, 1):
        x, y, w, h = crop
        filters.append(
            f"crop=trunc(iw*{w:.6f}/2)*2:trunc(ih*{h:.6f}/2)*2"
            f":trunc(iw*{x:.6f}):trunc(ih*{y:.6f})"
        )
    return ",".join(filters)


def chain(*parts):
    return ",".join(p for p in parts if p)


def grab_frame(ffmpeg, path, seconds, max_w, max_h, fast=False, pre_vf=""):
    """Return the frame at `seconds` as PPM bytes for tk.PhotoImage, or None.

    fast=True jumps to the nearest keyframe at or before `seconds` instead of
    decoding up to the exact frame: much quicker, but only approximate.
    pre_vf is applied before scaling, e.g. a transform_filters() chain.
    """
    vf = (
        f"scale='min({max_w},iw)':'min({max_h},ih)'"
        ":force_original_aspect_ratio=decrease"
    )
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if fast:
        cmd += ["-noaccurate_seek", "-skip_frame", "nokey"]
        vf += ":flags=fast_bilinear"
        seek, origin = ["-ss", f"{seconds:.3f}"], None
    else:
        seek, _, origin = _seek(path, seconds)
    cmd += seek
    if fast:
        # Without this ffmpeg drops a keyframe that lies before the seek point.
        cmd += ["-copyts"]
    cmd += [
        "-i", path, "-an", "-sn",
        "-frames:v", "1", "-vf", chain(_trim_video(origin), pre_vf, vf),
        "-f", "image2pipe", "-vcodec", "ppm", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
    if proc.returncode != 0 or not proc.stdout:
        return grab_frame(ffmpeg, path, seconds, max_w, max_h, pre_vf=pre_vf) if fast else None
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


# While the video encoder is still warming up, the sound waits in a queue; with
# long or high-resolution clips it can overflow ("Too many packets buffered for
# output stream"), so allow a much longer queue.
MUXING_QUEUE = ["-max_muxing_queue_size", "9999"]

# Animation output formats: name -> (file extension, file type label)
ANIMATION_FORMATS = {
    "GIF": (".gif", "GIF image"),
    "WebP": (".webp", "Animated WebP"),
    "Animated PNG": (".png", "Animated PNG"),
}
DIRECTIONS = ("Forward", "Reverse", "Boomerang")


def animation_seconds(start, end, speed, direction):
    """How long the finished animation runs."""
    seconds = (end - start) / speed
    return seconds * 2 if direction == "Boomerang" else seconds


def build_animation_command(ffmpeg, src, dst, start, end, fps, width, speed=1.0,
                            fmt="GIF", direction="Forward", colors=256, loop=True,
                            pre_vf="", dither="sierra2_4a"):
    """Build the ffmpeg command line for an animated GIF, WebP or PNG of start..end.

    pre_vf (e.g. a transform_filters() chain) is applied first. width 0 keeps
    the size; otherwise it's scaled down (never up) to that width. direction is
    Forward, Reverse, or Boomerang (forward then back). GIFs use a palette made
    for the clip (two-pass), which gives much better colours than the default.
    """
    seek, pre, origin = _seek(src, start)
    filters = [_trim_video(origin), pre_vf]
    if speed != 1.0:
        filters.append(f"setpts=PTS/{speed}")
    filters.append(f"fps={fps}")
    if width > 0:
        filters.append(f"scale='min({width},iw)':-2:flags=lanczos")
    graph = chain(*filters)
    if direction == "Reverse":
        graph += ",reverse"
    elif direction == "Boomerang":
        # The reversed copy skips its first frame, which is the forward clip's
        # last one, so the turn-around doesn't stutter.
        graph += (",split[fw][bw];[bw]reverse,trim=start_frame=1,setpts=PTS-STARTPTS[rv];"
                  "[fw][rv]concat=n=2:v=1:a=0")
    if fmt == "GIF":
        graph += (",split[a][b];"
                  f"[a]palettegen=max_colors={colors}:stats_mode=diff[p];"
                  f"[b][p]paletteuse=dither={dither}:diff_mode=rectangle")
        out = ["-loop", "0" if loop else "-1"]
    elif fmt == "WebP":
        out = ["-c:v", "libwebp_anim", "-lossless", "0", "-q:v", "80",
               "-compression_level", "4", "-loop", "0" if loop else "1", "-f", "webp"]
    elif fmt == "Animated PNG":
        out = ["-f", "apng", "-plays", "0" if loop else "1", "-pred", "mixed"]
    else:
        raise ValueError(f"unknown format {fmt}")
    return [
        ffmpeg, "-hide_banner", "-y", "-nostats", "-loglevel", "error",
        "-progress", "pipe:1",
        *seek, "-t", f"{end - start + pre:.3f}", "-i", src,
        "-filter_complex", graph, "-an", *out, *MUXING_QUEUE,
        dst,
    ]


def available_formats(ffmpeg):
    """The ANIMATION_FORMATS this ffmpeg can write (WebP needs libwebp)."""
    try:
        encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                                  text=True, errors="replace", creationflags=_NO_WINDOW).stdout
    except OSError:
        return ["GIF"]
    needs = {"GIF": " gif ", "WebP": " libwebp_anim ", "Animated PNG": " apng "}
    return [f for f in ANIMATION_FORMATS if needs[f] in encoders] or ["GIF"]


def merge_sections(sections):
    """Sort (start, end) sections by time and merge any that overlap or touch."""
    merged = []
    for start, end in sorted(sections):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def build_clip_command(ffmpeg, src, dst, sections, has_audio, pre_vf=""):
    """Build the ffmpeg command line to save sections of a video, joined, as an MP4.

    sections is a list of (start, end) in play order. Each is read with its own
    exact seek and the pieces are joined, so everything outside them is cut out.
    The video is re-encoded (H.264 + AAC, quality close to the original) so cuts
    land exactly on the given times. pre_vf (e.g. transform_filters()) is applied
    to the joined video.
    """
    cmd = [ffmpeg, "-hide_banner", "-y", "-nostats", "-loglevel", "error",
           "-progress", "pipe:1"]
    graph = []
    joined = ""
    for i, (start, end) in enumerate(sections):
        length = end - start
        seek, pre, origin = _seek(src, start)
        cmd += [*seek, "-t", f"{length + pre:.3f}", "-i", src]
        # Timestamps are kept relative to the cut (not reset to the first whole frame)
        # so each piece's picture and sound end together and joins don't drift.
        if origin is None:
            cut = f"trim=end={length:.3f}"
        else:
            cut = f"trim=start={origin:.6f}:end={origin + length:.6f},setpts=PTS-{origin:.6f}/TB"
        graph.append(f"[{i}:v:0]{cut}[v{i}]")
        joined += f"[v{i}]"
        if has_audio:
            # Pad or trim each piece's sound to the piece's exact length, so the
            # sound can't drift out of step with the picture across the joins.
            if origin is None:
                cut = f"atrim=end={length:.3f}"
            else:
                cut = f"atrim=start={origin:.6f}:end={origin + length:.6f},asetpts=PTS-{origin:.6f}/TB"
            graph.append(f"[{i}:a:0]apad,{cut}[a{i}]")
            joined += f"[a{i}]"
    audio = 1 if has_audio else 0
    graph.append(f"{joined}concat=n={len(sections)}:v=1:a={audio}[vj]" + ("[aj]" if audio else ""))
    # H.264 in yuv420p needs even dimensions
    graph.append("[vj]" + chain(pre_vf, "scale=trunc(iw/2)*2:trunc(ih/2)*2") + "[vout]")
    cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]"]
    if audio:
        cmd += ["-map", "[aj]", "-c:a", "aac", "-b:a", "192k"]
    cmd += [
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", *MUXING_QUEUE,
        dst,
    ]
    return cmd


def run_with_progress(cmd, output_seconds, on_progress=None, should_cancel=None):
    """Run a command from build_animation_command or build_clip_command, reporting
    progress in 0..1. The output file is the last argument.

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


def fit_size(width, height, max_w, max_h, upscale=False):
    """Scale (width, height) to fit in max_w x max_h, keeping even sizes.

    Only scales down unless upscale is set.
    """
    if width <= 0 or height <= 0:
        return max_w, max_h
    scale = min(max_w / width, max_h / height)
    if not upscale:
        scale = min(1.0, scale)
    w = max(2, int(width * scale) // 2 * 2)
    h = max(2, int(height * scale) // 2 * 2)
    return w, h


def open_raw_stream(ffmpeg, path, start, width, height, fps, duration=None,
                    pre_vf="", speed=1.0):
    """Start ffmpeg decoding the video as raw RGB frames of width x height on stdout.

    duration is in source seconds; output frames come at `fps` after the speed change.
    """
    seek, pre, origin = _seek(path, start)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", *seek]
    if duration is not None:
        cmd += ["-t", f"{duration + pre:.3f}"]
    setpts = f"setpts=PTS/{speed}" if speed != 1.0 else ""
    cmd += [
        "-i", path, "-an", "-sn",
        "-vf", chain(_trim_video(origin), pre_vf, setpts, f"fps={fps}",
                     f"scale={width}:{height}:flags=bilinear"),
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )


def atempo_filters(speed):
    """Audio filters that change the speed without changing the pitch."""
    parts = []
    while speed > 2.0:  # each atempo handles 0.5x to 2x
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    if abs(speed - 1.0) > 1e-6:
        parts.append(f"atempo={speed:.6f}")
    return ",".join(parts)


def open_audio_stream(ffmpeg, path, start, rate, channels, speed=1.0):
    """Start ffmpeg decoding the audio from `start` as s16le PCM on stdout.

    speed plays it faster or slower, keeping the pitch.
    """
    seek, _, origin = _seek(path, start)
    af = chain(_trim_audio(origin), atempo_filters(speed) if speed != 1.0 else "")
    return subprocess.Popen(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", *seek,
            "-i", path, "-vn", "-sn", "-dn", *(["-af", af] if af else []),
            "-f", "s16le", "-acodec", "pcm_s16le", "-ac", str(channels), "-ar", str(rate), "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )


def save_frame(ffmpeg, path, seconds, dst, vf=""):
    """Save the full-resolution frame at `seconds` to dst (format from its extension).

    vf is an optional filter chain such as transform_filters().
    Returns None on success or an error message.
    """
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
    ]
    seek, _, origin = _seek(path, seconds)
    cmd += [*seek, "-i", path, "-frames:v", "1", "-update", "1"]
    vf = chain(_trim_video(origin), vf)
    if vf:
        cmd += ["-vf", vf]
    if os.path.splitext(dst)[1].lower() in (".jpg", ".jpeg"):
        cmd += ["-q:v", "2"]
    cmd.append(dst)
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                          creationflags=_NO_WINDOW)
    if proc.returncode != 0 or not os.path.exists(dst):
        return proc.stderr.strip() or "ffmpeg could not save the frame."
    return None
