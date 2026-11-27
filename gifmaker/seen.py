"""Seen / unseen marks on files, kept between sessions.

Saved in %APPDATA%\\GifMaker\\seen.json by the file's path, along with its size and
modified time. GifMaker's own renames and moves carry the mark along; if a file
was moved or renamed some other way (in Explorer), it's still found by its size
and modified time, which a move or rename doesn't change.
"""

import json
import os
import threading

SEEN, UNSEEN = "seen", "unseen"
COLORS = {SEEN: "#2ea44f", UNSEEN: "#d73a3a"}
DARKER = {SEEN: "#1a7f37", UNSEEN: "#b42323"}  # for text on light backgrounds

_marks = None  # {normcased path: [state, size, mtime_ns]}
_by_stamp = None  # {(size, mtime_ns): normcased path}
_lock = threading.RLock()  # renames can happen on a background thread


def _locked(fn):
    def wrapper(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "GifMaker", "seen.json")


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def _stamp(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def _load():
    global _marks, _by_stamp
    if _marks is not None:
        return
    _marks = {}
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        for key, value in data.items():
            if isinstance(value, list) and len(value) == 3 and value[0] in COLORS:
                _marks[key] = value
    except (OSError, ValueError, AttributeError):
        pass
    _by_stamp = {(size, mtime): key for key, (_, size, mtime) in _marks.items()}


def _save():
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(_marks, f)
        os.replace(temp, path)  # never a half-written file
    except OSError:
        pass


@_locked
def get(path, look_further=True):
    """SEEN, UNSEEN or None. look_further=False skips looking for a file moved
    outside GifMaker (which reads the file's size and date: slow for thousands)."""
    _load()
    if not _marks:
        return None
    record = _marks.get(_key(path))
    if record:
        return record[0]
    if not look_further:
        return None
    stamp = _stamp(path)
    old = _by_stamp.get(stamp) if stamp else None
    if old and old in _marks:
        # Moved or renamed outside GifMaker: same size and modified time.
        state = _marks[old][0]
        if not os.path.exists(old):
            del _marks[old]
        _marks[_key(path)] = [state, *stamp]
        _by_stamp[stamp] = _key(path)
        _save()
        return state
    return None


@_locked
def mark(path, state):
    """Mark path SEEN or UNSEEN, or clear its mark (None)."""
    _load()
    key = _key(path)
    old = _marks.pop(key, None)
    if old:
        _by_stamp.pop((old[1], old[2]), None)
    if state in COLORS:
        stamp = _stamp(path)
        _marks[key] = [state, *(stamp or (0, 0))]
        if stamp:
            _by_stamp[stamp] = key
    _save()


@_locked
def moved(src, dst):
    """src is now at dst (renamed or moved): it keeps its mark."""
    moved_all([(src, dst)])


@_locked
def moved_all(pairs):
    """moved() for each (src, dst), saving once."""
    _load()
    records = [(_marks.pop(_key(src), None), dst) for src, dst in pairs]
    changed = False
    for record, dst in records:  # (after taking them all out, so names can swap round)
        if record:
            _marks[_key(dst)] = record
            _by_stamp[(record[1], record[2])] = _key(dst)
            changed = True
    if changed:
        _save()


@_locked
def forget(path):
    """path was deleted."""
    _load()
    if _key(path) in _marks:
        mark(path, None)
