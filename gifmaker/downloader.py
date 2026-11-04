"""Downloading videos from links (YouTube and many other sites) with yt-dlp.

yt-dlp isn't bundled: sites change often and yt-dlp keeps up with frequent
releases, so the latest yt-dlp.exe is fetched into %LOCALAPPDATA%\\GifMaker on
first use and asked to update itself at most once a day after that. Running
from source can use an installed yt-dlp instead (`pip install yt-dlp`).
"""

import collections
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import threading
import urllib.parse
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


# Browsers yt-dlp can borrow a login from (label -> its name for --cookies-from-browser).
BROWSERS = {"Firefox": "firefox", "Chrome": "chrome", "Edge": "edge", "Brave": "brave",
            "Opera": "opera", "Vivaldi": "vivaldi", "Chromium": "chromium"}
LIST_LIMIT = 40  # videos listed from one page at most
BROWSER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like "
                 "Gecko) Chrome/130.0.0.0 Safari/537.36")


def _login(browser, url):
    """(yt-dlp arguments for downloading url logged in as in browser, a note for the
    user or None). Firefox's login is read here (see firefox_cookies); other
    browsers' by yt-dlp."""
    name = BROWSERS.get(browser)
    if not name:
        return [], None
    if name == "firefox":
        from . import firefox_cookies

        site = urllib.parse.urlsplit(url).hostname or "the site"
        try:
            cookies, profile = firefox_cookies.cookies_for(url)
        except (OSError, sqlite3.Error):
            return ["--cookies-from-browser", name], None  # let yt-dlp try
        if not cookies:
            return [], (f"Firefox has no login for {site}" +
                        ("" if profile else " (Firefox's files weren't found)") +
                        ", so this is what visitors who aren't logged in get. Log in to "
                        "the site in Firefox, then try again.")
        path = os.path.join(tempfile.gettempdir(), f"gifmaker-cookies-{uuid.uuid4().hex}.txt")
        firefox_cookies.write_netscape(cookies, path)
        return ["--cookies", path], None
    return ["--cookies-from-browser", name], None


def _forget_login(cmd):
    """Delete the cookies file a command used, if GifMaker made it."""
    if "--cookies" in cmd:
        path = cmd[cmd.index("--cookies") + 1]
        if os.path.basename(path).startswith("gifmaker-cookies-"):
            try:
                os.remove(path)
            except OSError:
                pass


def list_videos(cmd, url, ffmpeg, browser=None):
    """The videos on the page at url, for picking one: (page title, [[key, name,
    seconds or None, height or None], ...], note). key is the video's number for
    yt-dlp, or the link of one GifMaker found in the page itself (see _scan). The list
    is empty when url is simply one video. note says if the login couldn't be used.
    Raises DownloadError."""
    folder = tempfile.mkdtemp(prefix="gifmaker-page-")
    login, note = _login(browser, normalize(url))
    try:
        proc = subprocess.run(
            cmd + ["-J", "--flat-playlist", "--no-playlist", "--no-warnings",
                   "-I", f"1:{LIST_LIMIT}", "--write-pages", *login, normalize(url)],
            capture_output=True, timeout=180, creationflags=_NO_WINDOW, cwd=folder,
            stdin=subprocess.DEVNULL, env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))
        found = _scan(folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        _forget_login(login)
    data = None
    if proc.returncode == 0:
        try:
            data = json.loads(proc.stdout.decode("utf-8", "replace"))
        except ValueError:
            pass
    if data is None and not found:
        errors = [l[6:].strip() for l in proc.stderr.decode("utf-8", "replace").splitlines()
                  if l.startswith("ERROR:")]
        raise DownloadError(_plain(errors[-1] if errors else f"yt-dlp stopped (code {proc.returncode})."))
    data = data or {}
    title = str(data.get("title") or "")
    if data.get("_type") != "playlist":  # the page's one video, titled "Page title (1)"
        title = re.sub(r" \(\d+\)$", "", title)
    entries = [e for e in (data.get("entries") or []) if isinstance(e, dict)] \
        if data.get("_type") == "playlist" else [data] if data else []
    if data and data.get("_type") != "playlist" and data.get("extractor_key") not in _GENERIC:
        return title, [], note  # a site yt-dlp knows, with one video: just download it
    videos = [[i, _entry_name(e), e.get("duration"), e.get("height")]
              for i, e in enumerate(entries, 1)]
    measure = [(v, _entry_url(e), e.get("http_headers"), e.get("cookies"))
               for v, e in zip(videos, entries) if not v[2] and _entry_url(e)]
    # Links in the page yt-dlp left out: streams it couldn't open (servers that only
    # serve visitors coming from their site), or players it doesn't know.
    known = {_entry_url(e) for e in entries}
    for link in found:
        if link not in known and len(videos) < LIST_LIMIT:
            video = [link, _link_name(link), None, None]
            videos.append(video)
            measure.append((video, link, None, None))
    if len(videos) < 2 and not any(isinstance(v[0], str) for v in videos):
        return title, [], note
    links = [_entry_url(e) for e in entries] + [v[0] for v in videos[len(entries):]]
    _tell_apart(videos, links)
    # Videos found in a page's HTML come without a length: ask ffmpeg (all at once).
    threads = [threading.Thread(target=_measure, args=(ffmpeg, link, url, headers, cookies, v),
                                daemon=True) for v, link, headers, cookies in measure]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=25)
    return title, videos, note


_MEDIA_LINK = re.compile(r"""https?:(?:\\?/){2}[^\s"'<>()]+?\.(?:m3u8|mpd|mp4|webm|m4v|mov)"""
                         r"""(?:\?[^\s"'<>]*)?(?=["'\s<>]|$)""", re.I)


def _scan(folder):
    """Video links in the web page(s) yt-dlp saved in folder (--write-pages)."""
    links = []
    for name in sorted(os.listdir(folder)):
        try:
            with open(os.path.join(folder, name), "rb") as f:
                text = f.read(8_000_000).decode("utf-8", "replace")
        except OSError:
            continue
        if "<html" not in text[:5000].lower() and "<video" not in text.lower():
            continue  # a playlist or other file it fetched, not a page
        for m in _MEDIA_LINK.finditer(text):
            link = html.unescape(m.group(0).replace("\\/", "/"))
            if link not in links:
                links.append(link)
    return links


def _from_page(url):
    """Say the requests come from url's page, as a browser does: some video servers
    only serve visitors coming from their own site."""
    parts = urllib.parse.urlsplit(url)
    if not parts.scheme.startswith("http"):
        return []
    return ["--referer", url, "--add-headers", f"Origin:{parts.scheme}://{parts.netloc}"]


def _tell_apart(videos, links):
    """Videos with the same name (every preview called "preview.mp4") get the part of
    their link that differs put in front: "Some-Model \u00B7 preview.mp4"."""
    groups = collections.defaultdict(list)
    for i, v in enumerate(videos):
        groups[v[1]].append(i)
    for same in groups.values():
        if len(same) < 2:
            continue
        paths = [[urllib.parse.unquote(p) for p in urllib.parse.urlsplit(links[i] or "").path.split("/")
                  if p] for i in same]
        for depth in range(max(len(p) for p in paths)):
            parts = [p[depth] if depth < len(p) else "" for p in paths]
            if len(set(parts)) > 1:
                for i, part in zip(same, parts):
                    if part:
                        videos[i][1] = f"{part} \u00B7 {videos[i][1]}"
                break


_GENERIC_PARTS = {"hls", "dash", "video", "videos", "media", "stream", "streams", "vod",
                  "master", "index", "playlist", "manifest"}


def _link_name(link):
    """A readable name for a video link: its file name, or for a stream (".../Some
    Video/hls/master.m3u8") the folder that names it."""
    path = urllib.parse.unquote(urllib.parse.urlsplit(link).path)
    parts = [p for p in path.split("/") if p]
    if not parts:
        return link
    tail = parts[-1]
    if not tail.lower().startswith(("index", "master", "playlist", "manifest")):
        return tail
    for part in reversed(parts[:-1]):
        if part.lower() not in _GENERIC_PARTS and not re.fullmatch(r"\d+p?", part):
            return f"{part} (stream)"
    return tail


_GENERIC = {"Generic", "HTML5MediaEmbed"}  # yt-dlp read the page itself (no site support)


def _entry_url(entry):
    url = entry.get("url")
    if not url and entry.get("formats"):
        url = entry["formats"][-1].get("url")
    return url if isinstance(url, str) and url.startswith("http") else None


def _entry_name(entry):
    """The file name in the video's link (often telling: "trailer", "preview"), or its title."""
    url = _entry_url(entry)
    name = _link_name(url) if url else ""
    return name if name and "." in name or "(stream)" in name else \
        str(entry.get("title") or name or "video")


def _measure(ffmpeg, link, page, headers, cookies, video):
    """Fill in video's length and height by having ffmpeg open its link."""
    headers = dict(headers or {})
    headers.setdefault("User-Agent", BROWSER_AGENT)
    headers.setdefault("Referer", page)
    parts = urllib.parse.urlsplit(page)
    headers.setdefault("Origin", f"{parts.scheme}://{parts.netloc}")
    cookie = _cookie_header(cookies)
    if cookie:
        headers["Cookie"] = cookie
    cmd = [ffmpeg, "-hide_banner", "-headers",
           "".join(f"{k}: {v}\r\n" for k, v in headers.items()), "-i", link]
    try:
        err = subprocess.run(cmd, capture_output=True, timeout=20, creationflags=_NO_WINDOW,
                             stdin=subprocess.DEVNULL).stderr
    except (OSError, subprocess.SubprocessError):
        return
    text = err.decode("utf-8", "replace")
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        video[2] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    heights = [int(h) for h in re.findall(r"Video: .*?, \d{2,5}x(\d{2,5})", text)]
    if heights and not video[3]:
        video[3] = max(heights)  # a stream offers several sizes: the best


def _cookie_header(cookies):
    """yt-dlp's cookies field ("a=1; Domain=...; b=2; ...") as a Cookie header."""
    if not isinstance(cookies, str):
        return ""
    skip = {"domain", "path", "expires", "max-age", "samesite", "secure", "httponly"}
    pairs = [p.strip() for p in cookies.split(";")]
    return "; ".join(p for p in pairs if "=" in p and p.split("=", 1)[0].strip().lower() not in skip)


def build_command(cmd, url, folder, quality, ffmpeg, browser=None, item=None, title=None):
    """The yt-dlp command line that downloads url as an mp4 into folder, logged in as in
    browser if given. item picks one video from a page with several (see list_videos):
    its number, or a link found in the page, downloaded as coming from that page and
    named after title."""
    name = "%(title).120B [%(id)s].%(ext)s"
    login = _login(browser, normalize(url))[0]
    extra = ["-I", str(item)] if isinstance(item, int) else []
    if isinstance(item, str):
        extra = _from_page(url)
        url = item
        if title:
            clean = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip(" .")[:120]
            name = clean.replace("%", "%%") + ".%(ext)s"
    # Where yt-dlp writes the saved file's path, as UTF-8: printed to the pipe it goes
    # through the Windows code page, which mangles emoji and many accented letters.
    record = os.path.join(tempfile.gettempdir(), f"gifmaker-saved-{uuid.uuid4().hex}.txt")
    return cmd + [
        *login, *extra,
        "--print-to-file", "after_move:%(filepath)s", record,
        "--no-playlist", "--newline", "--progress", "--no-colors", "--windows-filenames",
        "--ffmpeg-location", ffmpeg,
        "-f", "bv*+ba/b", "-S", QUALITIES.get(quality, QUALITIES["Best"]),
        "--merge-output-format", "mp4",
        "-o", os.path.join(folder, name),
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
    _forget_login(cmd)
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
    if "cookie" in low and ("decrypt" in low or "dpapi" in low or "app-bound" in low):
        return ("Couldn't use that browser's login: Chrome and Edge now lock it away from "
                "other programs. Log in to the site in Firefox and pick Firefox instead.")
    if "cookie" in low and ("could not copy" in low or "permission" in low or "locked" in low):
        return ("Couldn't read that browser's login while it's open. Close the browser "
                "completely and try again (or use Firefox, which works while open).")
    if "could not find" in low and "cookies database" in low:
        return "That browser isn't installed, or has no login saved. Pick the one you log in with."
    if "drm" in low:
        return "That video is copy-protected (DRM), so it can't be downloaded."
    if "private video" in low or "video unavailable" in low or "removed" in low:
        return "The video isn't available (private, removed, or blocked where you are)."
    return reason[:400]


def default_folder():
    videos = os.path.join(os.path.expanduser("~"), "Videos")
    return os.path.join(videos if os.path.isdir(videos) else tempfile.gettempdir(),
                        "GifMaker downloads")
