"""Reading a site's login (cookies) from Firefox, for downloading as the logged-in user.

yt-dlp can read Firefox's cookies itself, but it copies only cookies.sqlite: a login
made recently, while Firefox stays open, is still in cookies.sqlite-wal (Firefox
merges it in later), and logins without "remember me" are kept only in the session
file. Both are read here, and only the cookies for the one site are handed on.
"""

import glob
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.parse


def profile_dirs(roots=None):
    """Firefox profile folders (in roots, or where Firefox keeps them), the most
    recently used first."""
    if roots:
        pass
    elif sys.platform == "win32":
        roots = [os.path.expandvars(r"%APPDATA%\Mozilla\Firefox\Profiles"),
                 os.path.expandvars(r"%LOCALAPPDATA%\Packages\Mozilla.Firefox_n80bbvh6b1yt2"
                                    r"\LocalCache\Roaming\Mozilla\Firefox\Profiles")]
    else:
        roots = [os.path.expanduser("~/.mozilla/firefox"),
                 os.path.expanduser("~/.config/mozilla/firefox"),
                 os.path.expanduser("~/snap/firefox/common/.mozilla/firefox")]
    found = []
    for root in roots:
        for db in glob.glob(os.path.join(root, "*", "cookies.sqlite")):
            profile = os.path.dirname(db)
            stamps = [os.path.getmtime(p) for p in (db, db + "-wal") if os.path.exists(p)]
            found.append((max(stamps), profile))
    return [p for _, p in sorted(found, reverse=True)]


def _matches(host, site):
    """Whether a cookie for host is sent to site (a host name)."""
    host = host.lstrip(".").lower()
    return site == host or site.endswith("." + host)


def _from_database(profile, site):
    """(host, name, value, path, expires or 0, secure) for site from cookies.sqlite,
    including what's still only in its -wal file."""
    folder = tempfile.mkdtemp(prefix="gifmaker-ff-")
    try:
        copy = os.path.join(folder, "cookies.sqlite")
        shutil.copyfile(os.path.join(profile, "cookies.sqlite"), copy)
        wal = os.path.join(profile, "cookies.sqlite-wal")
        if os.path.exists(wal):  # sqlite applies it when it opens the copy
            shutil.copyfile(wal, copy + "-wal")
        conn = sqlite3.connect(copy)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            rows = conn.execute("SELECT host, name, value, path, expiry, isSecure "
                                "FROM moz_cookies").fetchall()
        finally:
            conn.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    out = []
    for host, name, value, path, expiry, secure in rows:
        if host and _matches(host, site):
            if version >= 16 and expiry:  # Firefox 142+ counts in milliseconds
                expiry //= 1000
            out.append((host, name, value, path or "/", int(expiry or 0), bool(secure)))
    return out


def _lz4_block(src, size):
    """Decompress one LZ4 block (Firefox's .jsonlz4 files are one block)."""
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        length = token >> 4
        if length == 15:
            while True:
                b = src[i]
                i += 1
                length += b
                if b != 255:
                    break
        dst += src[i:i + length]
        i += length
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        length = token & 15
        if length == 15:
            while True:
                b = src[i]
                i += 1
                length += b
                if b != 255:
                    break
        length += 4
        start = len(dst) - offset
        if offset >= length:
            dst += dst[start:start + length]
        else:  # the copy overlaps what it's making (a repeating run)
            for k in range(length):
                dst.append(dst[start + k])
    return bytes(dst[:size])


def _read_jsonlz4(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"mozLz40\0":
        raise ValueError("not a mozlz4 file")
    size = int.from_bytes(data[8:12], "little")
    return json.loads(_lz4_block(data[12:], size).decode("utf-8"))


def _from_session(profile, site):
    """Cookies for site that last only while Firefox is open ("don't remember me"):
    Firefox keeps those in its session file."""
    for name in ("sessionstore-backups/recovery.jsonlz4", "sessionstore.jsonlz4"):
        path = os.path.join(profile, *name.split("/"))
        if not os.path.exists(path):
            continue
        try:
            session = _read_jsonlz4(path)
        except (OSError, ValueError, IndexError, UnicodeDecodeError):
            continue
        out = []
        for c in session.get("cookies") or []:
            host = c.get("host") or ""
            if host and c.get("name") is not None and _matches(host, site):
                out.append((host, c["name"], c.get("value", ""), c.get("path") or "/", 0,
                            bool(c.get("secure"))))
        return out
    return []


def cookies_for(url, roots=None):
    """(cookies, profile) for url's site from the most recently used Firefox profile
    that has any; ([], profile or None) if none does. Raises OSError if Firefox's
    files can't be read."""
    site = (urllib.parse.urlsplit(url).hostname or "").lower()
    profiles = profile_dirs(roots)
    for profile in profiles:
        found = _from_database(profile, site)
        known = {(c[0], c[1], c[3]) for c in found}
        found += [c for c in _from_session(profile, site) if (c[0], c[1], c[3]) not in known]
        if found:
            return found, profile
    return [], (profiles[0] if profiles else None)


def write_netscape(cookies, path):
    """Save cookies as a cookies.txt file (the format yt-dlp's --cookies reads)."""
    far = int(time.time()) + 24 * 3600  # session cookies: valid for this download
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for host, name, value, cpath, expires, secure in cookies:
            f.write("\t".join((host, "TRUE" if host.startswith(".") else "FALSE", cpath,
                               "TRUE" if secure else "FALSE", str(expires or far), name,
                               value)) + "\n")
