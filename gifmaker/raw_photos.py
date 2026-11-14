"""Camera RAW photos (CR2, NEF, ARW, DNG, ...), which ffmpeg can't open.

Every RAW file also holds a full-size (or near full-size) JPEG the camera made for its
own screen. That JPEG is pulled out once, kept in a cache folder, and shown in place of
the RAW file, turned the right way up.
"""

import hashlib
import os
import struct
import tempfile
import threading

RAW_EXTENSIONS = {".cr2", ".nef", ".nrw", ".arw", ".sr2", ".srf", ".dng", ".pef", ".srw",
                  ".orf", ".rw2", ".raf", ".erf", ".kdc", ".dcr", ".mos", ".3fr", ".mef"}
_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
_lock = threading.Lock()
_done = {}  # source key -> cached JPEG path


def is_raw(path):
    return os.path.splitext(path)[1].lower() in RAW_EXTENSIONS


def _cache_dir():
    return os.path.join(tempfile.gettempdir(), "gifmaker-raw")


def preview_path(path):
    """A JPEG of the RAW photo at path (cached), or None if it has none."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = f"{os.path.normcase(os.path.abspath(path))}|{st.st_size}|{st.st_mtime_ns}"
    with _lock:
        if key in _done and os.path.exists(_done[key]):
            return _done[key]
    out = os.path.join(_cache_dir(), hashlib.sha1(key.encode("utf-8")).hexdigest() + ".jpg")
    if not os.path.exists(out):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        jpeg = extract_jpeg(data)
        if not jpeg:
            return None
        try:
            os.makedirs(_cache_dir(), exist_ok=True)
            temp = f"{out}.{os.getpid()}.{threading.get_ident()}.part"
            with open(temp, "wb") as f:
                f.write(jpeg)
            os.replace(temp, out)
        except OSError:
            return None
    with _lock:
        _done[key] = out
    return out


# ------------------------------------------------------------- reading
def extract_jpeg(data):
    """The biggest JPEG picture inside a RAW file's bytes, turned the right way up
    (by an EXIF orientation added if it lacks one), or None."""
    if data[:16] == b"FUJIFILMCCD-RAW ":  # Fujifilm: a header, then a whole JPEG
        if len(data) < 92:
            return None
        offset, length = struct.unpack(">II", data[84:92])
        jpeg = data[offset:offset + length]
        return jpeg if _jpeg_size(jpeg) else None
    found, orientation = _tiff_jpegs(data)
    best = max(found, key=lambda j: j[0], default=None)
    if not best:
        return None
    jpeg = best[1]
    if orientation in range(2, 9) and not _has_exif(jpeg):
        jpeg = _with_orientation(jpeg, orientation)
    return jpeg


def _tiff_jpegs(data):
    """[(pixels, jpeg bytes)] of the ordinary JPEGs in a TIFF-like RAW, and the
    photo's orientation (1-8)."""
    order = {b"II": "<", b"MM": ">"}.get(data[:2])
    if not order or len(data) < 8:
        return [], 1
    first = struct.unpack(order + "I", data[4:8])[0]
    found, seen, orientation = [], set(), 1
    queue = [(first, True)]

    def values(entry):
        tag, kind, count = struct.unpack(order + "HHI", data[entry:entry + 8])
        size = _SIZES.get(kind, 1)
        at = entry + 8
        if size * count > 4:
            at = struct.unpack(order + "I", data[at:at + 4])[0]
        if kind in (3, 8):
            fmt = "H"
        elif kind in (4, 9, 13):
            fmt = "I"
        else:  # bytes: (offset of the bytes, how many)
            return tag, kind, count, at
        n = min(count, 4096)
        if at + n * size > len(data):
            return tag, kind, count, []
        return tag, kind, count, list(struct.unpack(order + fmt * n, data[at:at + n * size]))

    while queue and len(seen) < 64:
        ifd, is_first = queue.pop(0)
        if ifd in seen or not 8 <= ifd < len(data) - 2:
            continue
        seen.add(ifd)
        count = struct.unpack(order + "H", data[ifd:ifd + 2])[0]
        if ifd + 2 + count * 12 > len(data):
            continue
        tags = {}
        for k in range(min(count, 1000)):
            tag, kind, n, v = values(ifd + 2 + k * 12)
            tags[tag] = (kind, n, v)
        get = lambda t: tags.get(t, (0, 0, []))[2]  # noqa: E731
        if is_first and get(0x0112):
            orientation = get(0x0112)[0]
        offsets, counts = get(0x0111), get(0x0117)
        if isinstance(offsets, list) and len(offsets) == 1 and counts:
            _add(found, data, offsets[0], counts[0])
        start, length = get(0x0201), get(0x0202)
        if start and length:
            _add(found, data, start[0], length[0])
        if 0x002E in tags:  # Panasonic's JpgFromRaw: the JPEG is the tag's bytes
            _, n, at = tags[0x002E]
            if isinstance(at, int):
                _add(found, data, at, n)
        for sub in get(0x014A) or []:
            queue.append((sub, False))
        nxt = ifd + 2 + count * 12
        if nxt + 4 <= len(data):
            queue.append((struct.unpack(order + "I", data[nxt:nxt + 4])[0], False))
    return found, orientation


def _add(found, data, offset, length):
    if 0 < offset < len(data) and length > 100:
        jpeg = data[offset:offset + length]
        size = _jpeg_size(jpeg)
        if size:
            found.append((size[0] * size[1], jpeg))


def _segments(jpeg):
    """(marker, payload) of a JPEG's header segments, up to the picture data."""
    if jpeg[:2] != b"\xff\xd8":
        return
    i = 2
    while i + 4 <= len(jpeg):
        if jpeg[i] != 0xFF:
            return
        marker = jpeg[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        length = struct.unpack(">H", jpeg[i + 2:i + 4])[0]
        yield marker, jpeg[i + 4:i + 2 + length]
        if marker == 0xDA:  # start of the picture data
            return
        i += 2 + length


def _jpeg_size(jpeg):
    """(width, height) of an ordinary (baseline or progressive) JPEG, else None.
    Lossless JPEGs (the raw sensor data in some files) don't count."""
    for marker, payload in _segments(jpeg):
        if marker in (0xC0, 0xC1, 0xC2) and len(payload) >= 5:
            h, w = struct.unpack(">HH", payload[1:5])
            return (w, h) if w and h else None
        if marker in (0xC3, 0xC5, 0xC6, 0xC7, 0xCB, 0xCD, 0xCE, 0xCF):
            return None
    return None


def _has_exif(jpeg):
    return any(m == 0xE1 and p[:6] == b"Exif\0\0" for m, p in _segments(jpeg))


def _with_orientation(jpeg, orientation):
    """jpeg with a small EXIF block saying which way up it goes."""
    tiff = (b"MM\0\x2a" + struct.pack(">I", 8) + struct.pack(">H", 1)
            + struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0) + struct.pack(">I", 0))
    payload = b"Exif\0\0" + tiff
    return jpeg[:2] + b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + jpeg[2:]
