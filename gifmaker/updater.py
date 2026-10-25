"""Finds, downloads and installs newer versions from the GitHub releases.

Each release has the installer (GifMaker-Setup.exe) attached. An installed copy
updates itself by running the new installer silently, which replaces the files
and starts the app again. A portable copy just points to the release page.
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.request

from .version import __version__

REPO = "AleonSC/Gif-Maker"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
SETUP_NAME = "GifMaker-Setup.exe"
INSTALL_MARKER = "install.ini"  # written next to the exe by the installer
TIMEOUT = 15  # seconds


def parse_version(text):
    """'v1.0.42' -> (1, 0, 42), or None if it isn't a version number."""
    try:
        return tuple(int(part) for part in text.strip().lstrip("vV").split("."))
    except ValueError:
        return None


def can_update():
    """Whether this is a released build (not running from source)."""
    return getattr(sys, "frozen", False) and parse_version(__version__) is not None


def is_installed():
    """Whether this copy was installed with the installer (so it can update itself)."""
    return os.path.exists(os.path.join(os.path.dirname(sys.executable), INSTALL_MARKER))


def _get(url):
    request = urllib.request.Request(url, headers={
        "User-Agent": f"GifMaker/{__version__}", "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(request, timeout=TIMEOUT)


def check():
    """The newest release if it's newer than this one, else None.

    Returns {"version", "page", "setup_url", "size"} (setup_url may be None).
    Raises OSError (or ValueError) if GitHub can't be reached or answers oddly.
    """
    with _get(LATEST_URL) as response:
        release = json.load(response)
    latest = parse_version(release.get("tag_name", ""))
    current = parse_version(__version__)
    if latest is None or current is None or latest <= current:
        return None
    setup = next((a for a in release.get("assets", []) if a.get("name") == SETUP_NAME), None)
    return {"version": release["tag_name"].lstrip("vV"), "page": release.get("html_url"),
            "setup_url": setup and setup.get("browser_download_url"),
            "size": setup and setup.get("size")}


def download(update, on_progress=None, should_cancel=None):
    """Download the update's installer to a temporary file and return its path.

    Returns None if cancelled; raises OSError if the download fails.
    """
    path = os.path.join(tempfile.mkdtemp(prefix="gifmaker-update-"), SETUP_NAME)
    with _get(update["setup_url"]) as response, open(path, "wb") as f:
        total = int(response.headers.get("Content-Length") or update.get("size") or 0)
        done = 0
        while True:
            if should_cancel and should_cancel():
                return None
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if on_progress and total:
                on_progress(min(1.0, done / total))
    if total and done < total:
        raise OSError("The download was cut short.")
    return path


def install(path):
    """Start the installer silently; the caller should then quit straight away.

    The installer waits for the app to close, replaces it, and starts it again.
    """
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                      "/CLOSEAPPLICATIONS"], creationflags=flags, close_fds=True)
