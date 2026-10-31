"""Downloading videos from links (YouTube and many other sites) with yt-dlp.

yt-dlp isn't bundled: sites change often and yt-dlp keeps up with frequent
releases, so the latest yt-dlp.exe is fetched into %LOCALAPPDATA%\\GifMaker on
first use and asked to update itself at most once a day after that. Running
from source can use an installed yt-dlp instead (`pip install yt-dlp`).
"""

import collections
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

EXE_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
UPDATE_EVERY = 24 * 3600  # seconds between update checks
TIMEOUT = 30
QUALITIES = {  # label -> yt-dlp format sort (best first within the limit)
    "Best": "res,ext:mp4:m4a",
    "1080p": "res:1080,ext:mp4:m4a",
    "720p": "res:720,ext:mp4:m4a",
    "480p": "res:480,ext:mp4:m4a",
}
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_URL = re.compile(r"^https?://\S+\.\S+", re.I)
# Any RedGifs link: its pages (watch, ifr, i), and its file links as the site's
# download button and "copy video link" give them, e.g. media.redgifs.com/Name.mp4,
# Name-mobile.mp4, Name-silent.mp4, Name-poster.jpg on thumbs*/media/i hosts.
_REDGIFS = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)*redgifs\.com/(?:(?:watch|ifr|i)/)?"
    r"(?P<id>[A-Za-z]+)(?:-(?:mobile|silent|large|poster|small))*(?:\.\w+)?/?(?:[?#].*)?$",
    re.I)
_VIDEO_TYPES = (".mp4", ".webm", ".mkv", ".mov", ".m4v", ".gif", ".avi", ".flv")
_STREAM = re.compile(r"\.f\d[\w-]*\.\w+$")
_REDGIFS_PAGES = {"browse", "users", "niches", "gifs", "explore", "upload", "settings",
                  "login", "signup", "feed", "creators", "about", "terms", "privacy"}


class DownloadError(Exception):
    pass


def looks_like_link(text):
    return bool(text) and bool(_URL.match(text.strip()))


def normalize(url):
    """The link in the form yt-dlp knows. RedGifs file links (media.redgifs.com/
    SomeName.mp4 and the like) become the watch page of that clip; others are kept."""
    url = url.strip()
    m = _REDGIFS.match(url)
    if m and m.group("id").lower() not in _REDGIFS_PAGES:
        return f"https://www.redgifs.com/watch/{m.group('id').lower()}"
    return url


def _tools_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "GifMaker")


def _stamp_path():
    return os.path.join(_tools_dir(), "yt-dlp-checked.json")


def find_ytdlp():
    """The command that runs yt-dlp, or None if it isn't here yet (see fetch_ytdlp)."""
    exe = os.path.join(_tools_dir(), "yt-dlp.exe")
    if sys.platform == "win32" and os.path.exists(exe):
        return [exe]
    if not getattr(sys, "frozen", False):
        try:
            import yt_dlp  # noqa: F401  (running from source with it installed)

            return [sys.executable, "-m", "yt_dlp"]
        except ImportError:
            pass
    found = shutil.which("yt-dlp")
    return [found] if found else None


def fetch_ytdlp(on_status=None):
    """Download yt-dlp.exe (Windows) and return its command. Raises DownloadError."""
    if sys.platform != "win32":
        raise DownloadError("Install yt-dlp first: pip install yt-dlp")
    os.makedirs(_tools_dir(), exist_ok=True)
    exe = os.path.join(_tools_dir(), "yt-dlp.exe")
    if on_status:
        on_status("Getting yt-dlp (the downloader, about 18 MB) the first time...")
    temp = exe + ".part"
    try:
        request = urllib.request.Request(EXE_URL, headers={"User-Agent": "GifMaker"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response, open(temp, "wb") as f:
            shutil.copyfileobj(response, f)
        os.replace(temp, exe)
    except OSError as e:
        raise DownloadError(f"Couldn't download yt-dlp: {e}") from e
    _mark_checked()
    return [exe]


def _mark_checked():
    try:
        with open(_stamp_path(), "w", encoding="utf-8") as f:
            json.dump({"checked": time.time()}, f)
    except OSError:
        pass


def update_if_due(cmd, on_status=None):
    """Let yt-dlp.exe update itself if that hasn't been tried today."""
    if not cmd[0].lower().endswith("yt-dlp.exe"):
        return  # pip-installed copies are updated with pip
    try:
        with open(_stamp_path(), encoding="utf-8") as f:
            if time.time() - json.load(f)["checked"] < UPDATE_EVERY:
                return
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if on_status:
        on_status("Checking for a newer yt-dlp...")
    try:
        subprocess.run(cmd + ["-U"], capture_output=True, timeout=120, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        pass  # an old yt-dlp may still work; try again next time
    _mark_checked()


def build_command(cmd, url, folder, quality, ffmpeg):
    """The yt-dlp command line that downloads url as an mp4 into folder."""
    # Where yt-dlp writes the saved file's path, as UTF-8: printed to the pipe it goes
    # through the Windows code page, which mangles emoji and many accented letters.
    record = os.path.join(tempfile.gettempdir(), f"gifmaker-saved-{uuid.uuid4().hex}.txt")
    return cmd + [
        "--print-to-file", "after_move:%(filepath)s", record,
        "--no-playlist", "--newline", "--progress", "--no-colors", "--windows-filenames",
        "--ffmpeg-location", ffmpeg,
        "-f", "bv*+ba/b", "-S", QUALITIES.get(quality, QUALITIES["Best"]),
        "--merge-output-format", "mp4",
        "-o", os.path.join(folder, "%(title).120B [%(id)s].%(ext)s"),
        "--progress-template", "download:PROGRESS %(progress._percent_str)s|"
                               "%(progress._speed_str)s|%(progress._eta_str)s",
        "--print", "after_move:SAVED %(filepath)s",
        normalize(url),
    ]


def run(cmd, on_progress=None, should_cancel=None):
    """Run a build_command() line. Returns the saved file's path.

    on_progress(fraction or None, text) gets updates. Raises DownloadError with
    yt-dlp's own explanation if it fails, or "cancelled".
    """
    record = cmd[cmd.index("--print-to-file") + 2] if "--print-to-file" in cmd else None
    folder = os.path.dirname(cmd[cmd.index("-o") + 1]) if "-o" in cmd else None
    started = time.time()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, env=env,
                            encoding="utf-8", errors="replace", bufsize=1)
    saved, errors, stage, tail = None, [], 0, collections.deque(maxlen=4)
    try:
        for line in proc.stdout:
            if should_cancel and should_cancel():
                proc.kill()
                raise DownloadError("cancelled")
            line = line.strip()
            if line:
                tail.append(line)
            if line.startswith("PROGRESS "):
                pct, speed, eta = (line[9:].split("|") + ["", ""])[:3]
                try:
                    frac = float(pct.strip().rstrip("%")) / 100
                except ValueError:
                    frac = None
                if frac is not None and frac >= 0.999:
                    stage += 1  # video and sound come down separately
                part = " (sound)" if stage >= 1 and frac is not None and frac < 0.999 else ""
                if on_progress:
                    on_progress(frac, f"Downloading{part}... {pct.strip()}  {speed.strip()}"
                                      f"{'  ' + eta.strip() + ' left' if eta.strip() not in ('', 'NA', 'Unknown') else ''}")
            elif line.startswith("SAVED "):
                saved = line[6:].strip()
            elif "[Merger]" in line or "[VideoConvertor]" in line or "[ExtractAudio]" in line:
                if on_progress:
                    on_progress(None, "Joining the video and sound...")
            elif line.startswith("ERROR:"):
                errors.append(line[6:].strip())
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode == 0 and not (saved and os.path.exists(saved)):
        saved = _recorded(record) or _newest(folder, started) or saved
    if record:
        try:
            os.remove(record)
        except OSError:
            pass
    if proc.returncode != 0 or not saved or not os.path.exists(saved):
        if errors:
            reason = errors[-1]
        elif proc.returncode == 0:
            last = next((t for t in reversed(tail) if not t.startswith("PROGRESS ")), "")
            reason = ("yt-dlp finished but no video was saved (the link may not lead to a "
                      "video, or it's a list of them)." + (f" Last message: {last[:200]}"
                                                           if last else ""))
        else:
            reason = f"yt-dlp stopped (code {proc.returncode})."
        raise DownloadError(_plain(reason))
    return saved


def _recorded(record):
    """The last path yt-dlp wrote to the record file, if it's there."""
    try:
        with open(record, encoding="utf-8", errors="replace") as f:
            paths = [p.strip() for p in f if p.strip()]
    except (OSError, TypeError):
        return None
    return next((p for p in reversed(paths) if os.path.exists(p)), None)


def _newest(folder, since):
    """The newest finished video in folder written since the download started."""
    best = None
    try:
        with os.scandir(folder) as entries:
            for e in entries:
                name = e.name.lower()
                # finished videos only: not parts, nor the separate video and sound
                # streams (name.f137.mp4) that get joined
                if not e.is_file() or not name.endswith(_VIDEO_TYPES) or _STREAM.search(name):
                    continue
                stamp = e.stat().st_mtime
                if stamp >= since - 2 and (best is None or stamp > best[0]):
                    best = (stamp, e.path)
    except (OSError, TypeError):
        return None
    return best[1] if best else None


def _plain(reason):
    """Shorten yt-dlp's usual complaints into something actionable."""
    low = reason.lower()
    if "unsupported url" in low:
        return "That site or link isn't supported."
    if "sign in to confirm" in low or ("age" in low and "restrict" in low):
        return ("The site wants you to sign in first (age-restricted or bot check), so it "
                "can't be downloaded this way. " + reason[:200])
    if "drm" in low:
        return "That video is copy-protected (DRM), so it can't be downloaded."
    if "private video" in low or "video unavailable" in low or "removed" in low:
        return "The video isn't available (private, removed, or blocked where you are)."
    return reason[:400]


def default_folder():
    videos = os.path.join(os.path.expanduser("~"), "Videos")
    return os.path.join(videos if os.path.isdir(videos) else tempfile.gettempdir(),
                        "GifMaker downloads")
