"""Picture fingerprints for sorting videos and photos by colour and spotting look-alikes.

Each video gets a fingerprint of three frames (20%, 50% and 80% through). For
each frame, after trimming black bars: a 4x4 grid of colours in OKLab (a colour
space where distances match what the eye sees) and a 64-bit difference hash of
its light and dark shapes. Files whose frames match closely, and whose lengths
agree, count as look-alikes: the same video, re-encoded, resized or renamed.
"""

import json
import math
import os

from . import ffmpeg_tools as ff

GRAB = 32  # frames are read at GRAB x GRAB pixels
CELLS = 4  # colour layout grid is CELLS x CELLS
POINTS = (0.2, 0.5, 0.8)
DARK = 0.12  # rows/columns darker than this (OKLab lightness) at the edges are bars
SAME_HASH = 12  # at most this many of 64 hash bits may differ for a matching frame
SAME_COLOR = 0.06  # and the colour grids at most this far apart on average,
SAME_CELL = 0.2  # with no single cell further apart than this (a different overlay, say)
SAME_LENGTH = 0.03  # videos must be within 3% (or 1 s) of each other's length
NEUTRAL_CHROMA = 0.035  # a main colour less colourful than this is a grey
PALETTE = 4  # colours found in each picture
VIVID = 0.12  # chroma at which a colour counts as fully vivid when picking the main colour
COLOR_CHROMA = 0.04  # a pixel at least this colourful (OKLab chroma) counts as coloured
VISIBLE = 0.3  # pixels darker than this (OKLab lightness) are dark background, not the subject
COLORFUL = 0.06  # a picture needs this share of coloured pixels to count as in colour
VERSION = 4  # bump when the fingerprint changes, so old saved ones are ignored


# ------------------------------------------------------------- colour
def _linear(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def oklab(r, g, b):
    """sRGB 0..255 -> OKLab (L 0..1, a and b roughly -0.4..0.4)."""
    r, g, b = _linear(r), _linear(g), _linear(b)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


# ------------------------------------------------------------- frames
def _frame_fingerprint(rgb):
    """rgb: GRAB*GRAB*3 bytes. Returns (cells, hash, mean, samples) or None if it's all
    black; samples are (weight, colour) pixels for finding the picture's main colours."""
    lab = [oklab(rgb[i], rgb[i + 1], rgb[i + 2]) for i in range(0, len(rgb), 3)]
    lightness = [p[0] for p in lab]
    rows = [max(lightness[y * GRAB:(y + 1) * GRAB]) for y in range(GRAB)]
    cols = [max(lightness[x::GRAB]) for x in range(GRAB)]
    # Trim black bars (letterboxing), but never more than half the picture.
    top = next((y for y in range(GRAB // 4) if rows[y] > DARK), GRAB // 4)
    bottom = next((y for y in range(GRAB - 1, GRAB * 3 // 4, -1) if rows[y] > DARK), GRAB * 3 // 4)
    left = next((x for x in range(GRAB // 4) if cols[x] > DARK), GRAB // 4)
    right = next((x for x in range(GRAB - 1, GRAB * 3 // 4, -1) if cols[x] > DARK), GRAB * 3 // 4)
    if max(rows) <= DARK:  # a black frame says nothing about the video
        return None

    def area(x0, y0, x1, y1):
        """Average OKLab colour of the trimmed picture's region, in fractions 0..1."""
        w, h = right - left + 1, bottom - top + 1
        xa, xb = left + int(x0 * w), left + max(int(x0 * w) + 1, int(x1 * w))
        ya, yb = top + int(y0 * h), top + max(int(y0 * h) + 1, int(y1 * h))
        pixels = [lab[y * GRAB + x] for y in range(ya, yb) for x in range(xa, xb)]
        return tuple(sum(p[c] for p in pixels) / len(pixels) for c in range(3))

    cells = [area(cx / CELLS, cy / CELLS, (cx + 1) / CELLS, (cy + 1) / CELLS)
             for cy in range(CELLS) for cx in range(CELLS)]
    # Difference hash: 9x8 lightness, each bit says whether a cell is brighter than the next.
    grid = [[area(x / 9, y / 8, (x + 1) / 9, (y + 1) / 8)[0] for x in range(9)] for y in range(8)]
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (grid[y][x] > grid[y][x + 1])
    mean = tuple(sum(c[i] for c in cells) / len(cells) for i in range(3))
    # Pixels for the palette: 2x2 blocks of the trimmed picture, weighted towards the
    # middle, where the subject usually is.
    samples = []
    w, h = right - left + 1, bottom - top + 1
    for y in range(0, h - 1, 2):
        for x in range(0, w - 1, 2):
            block = [lab[(top + y + dy) * GRAB + left + x + dx] for dy in (0, 1) for dx in (0, 1)]
            color = tuple(sum(p[c] for p in block) / 4 for c in range(3))
            off_centre = math.hypot((x + 1) / w - 0.5, (y + 1) / h - 0.5) / 0.7071
            samples.append((2.0 - off_centre, color))
    return cells, bits, mean, samples


def palette(samples, k=PALETTE, rounds=8):
    """The k main colours of weighted (weight, colour) samples, as [(share, colour)],
    biggest first. Plain k-means, started from colours far apart (so it's repeatable)."""
    total = sum(w for w, _ in samples)
    mean = tuple(sum(w * c[i] for w, c in samples) / total for i in range(3))
    centres = [min((c for _, c in samples), key=lambda c: math.dist(c, mean))]
    while len(centres) < k:
        far = max(samples, key=lambda s: min(math.dist(s[1], c) for c in centres))[1]
        if min(math.dist(far, c) for c in centres) < 0.02:
            break  # the picture doesn't have k different colours
        centres.append(far)
    for _ in range(rounds):
        sums = [[0.0, 0.0, 0.0, 0.0] for _ in centres]
        for w, c in samples:
            i = min(range(len(centres)), key=lambda j: math.dist(c, centres[j]))
            acc = sums[i]
            acc[0] += w
            acc[1] += w * c[0]
            acc[2] += w * c[1]
            acc[3] += w * c[2]
        centres = [(a[1] / a[0], a[2] / a[0], a[3] / a[0]) if a[0] else centres[i]
                   for i, a in enumerate(sums)]
    shares = [a[0] / total for a in sums]
    return sorted(((s, c) for s, c in zip(shares, centres) if s > 0), reverse=True)


def tone(samples):
    """(colourful, colour) of a picture's weighted (weight, colour) samples.

    colourful is the share of the visible (not black) picture that's actually in
    colour, so a coloured figure on a black background counts fully, while a black
    and white picture with a tiny coloured detail doesn't. colour is the average of
    the coloured parts (weighted by how vivid they are) for a colour picture, or
    the average grey for a black and white one.
    """
    visible = [(w, c) for w, c in samples if c[0] > VISIBLE]
    total = sum(w for w, _ in samples)
    base = visible if sum(w for w, _ in visible) > 0.02 * total else samples
    base_total = sum(w for w, _ in base) or 1.0
    colored = [(w * math.hypot(c[1], c[2]), w, c) for w, c in base
               if math.hypot(c[1], c[2]) >= COLOR_CHROMA]
    colorful = sum(w for _, w, _ in colored) / base_total
    if colorful >= COLORFUL:
        weight = sum(v for v, _, _ in colored)
        color = tuple(sum(v * c[i] for v, _, c in colored) / weight for i in range(3))
    else:
        color = tuple(sum(w * c[i] for w, c in base) / base_total for i in range(3))
    return colorful, color


def fingerprint(ffmpeg, path):
    """A picture fingerprint for a video or image file, or None if it can't be read."""
    try:
        duration = ff.probe(ffmpeg, path).duration
    except RuntimeError:
        duration = 0.0  # still images
    times = [duration * p for p in POINTS] if duration > 1 else [0.0]
    frames = []
    for t in times:
        data = ff.grab_frame(ffmpeg, path, t, GRAB, GRAB,
                             pre_vf=f"scale={GRAB}:{GRAB}:flags=area")
        if not data:
            continue
        pixels = data.split(b"\n", 3)[-1]
        if len(pixels) == GRAB * GRAB * 3:
            frame = _frame_fingerprint(pixels)
            if frame:
                frames.append(frame)
    if not frames:
        return None
    mean = tuple(sum(f[2][i] for f in frames) / len(frames) for i in range(3))
    samples = [s for f in frames for s in f[3]]
    colors = palette(samples)
    colorful, main = tone(samples)
    return {"duration": duration, "frames": [f[:3] for f in frames], "mean": mean,
            "palette": [(share, color) for share, color in colors],
            "main": main, "colorful": colorful}


def swatch_rgb(sig):
    """sRGB for a fingerprint's main colour."""
    return to_rgb(sig["main"])


def to_rgb(color):
    """OKLab -> sRGB 0..255."""
    L, a, b = color
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    rgb = (4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
           -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
           -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_)

    def encode(c):
        c = min(1.0, max(0.0, c))
        return round(255 * (12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055))

    return tuple(encode(c) for c in rgb)


# ------------------------------------------------------- comparing
def _frames_match(f1, f2):
    differing = bin(f1[1] ^ f2[1]).count("1")
    cells = [math.dist(c1, c2) for c1, c2 in zip(f1[0], f2[0])]
    return (differing <= SAME_HASH and sum(cells) / len(cells) <= SAME_COLOR
            and max(cells) <= SAME_CELL)


def look_alike(s1, s2):
    """Whether two fingerprints look like the same video (or the same picture)."""
    d1, d2 = s1["duration"], s2["duration"]
    if (d1 > 1) != (d2 > 1):
        return False  # a video and a still image
    if d1 > 1 and abs(d1 - d2) > max(1.0, SAME_LENGTH * max(d1, d2)):
        return False
    pairs = list(zip(s1["frames"], s2["frames"]))
    if not pairs:
        return False
    matches = sum(_frames_match(a, b) for a, b in pairs)
    return matches >= max(1, len(pairs) - 1)  # all but one frame (e.g. a different intro)


def color_key(main, colorful=1.0):
    """Groups colour pictures by hue (reds, oranges, yellows, greens, blues, purples),
    then the black and white ones dark to light. Within a hue group lightness runs dark
    to light, and the other way in the next, so neighbouring groups meet at similar
    shades (no jumps). colorful is tone()'s share of the picture that's in colour."""
    L, a, b = main
    if colorful < COLORFUL or math.hypot(a, b) < NEUTRAL_CHROMA:
        return (1, 0, L)
    hue = (math.degrees(math.atan2(b, a)) + 360 - 15) % 360  # start just before red
    group = int(hue // 30)
    return (0, group, L if group % 2 == 0 else -L)


def order(names, sigs, keep_together=True):
    """Sort names by colour. With keep_together, look-alikes are grouped next to each
    other. Returns (ordered names, {name: group number} for groups of 2 or more)."""
    readable = [n for n in names if sigs.get(n)]
    unreadable = [n for n in names if not sigs.get(n)]
    parent = {n: n for n in readable}

    def root(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    if keep_together:
        # Only videos of about the same length can match, so compare neighbours by length.
        by_length = sorted(readable, key=lambda n: sigs[n]["duration"])
        for i, a in enumerate(by_length):
            da = sigs[a]["duration"]
            for b in by_length[i + 1:]:
                db = sigs[b]["duration"]
                if da > 1 and db - da > max(1.0, SAME_LENGTH * db):
                    break
                if look_alike(sigs[a], sigs[b]):
                    parent[root(b)] = root(a)
    groups = {}
    for n in readable:
        groups.setdefault(root(n), []).append(n)

    def group_key(members):
        return color_key(tuple(sum(sigs[n]["main"][i] for n in members) / len(members)
                               for i in range(3)),
                         sum(sigs[n].get("colorful", 1.0) for n in members) / len(members))

    ordered, labels = [], {}
    for members in sorted(groups.values(), key=group_key):
        if len(members) > 1:
            number = len(set(labels.values())) + 1
            labels.update({n: number for n in members})
        ordered += sorted(members, key=lambda n: (-sigs[n]["duration"], n))
    return ordered + unreadable, labels


def group_label(number):
    """1 -> A, 26 -> Z, 27 -> AA ..."""
    label = ""
    while number:
        number, rest = divmod(number - 1, 26)
        label = chr(65 + rest) + label
    return label


# --------------------------------------------------- saved fingerprints
def _cache_path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "GifMaker", "fingerprints.json")


def load_cache():
    """{file identity string: fingerprint} saved from earlier scans."""
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != VERSION:
            return {}
        cache = {}
        for key, sig in data["files"].items():
            if sig:
                sig["frames"] = [(cells, bits, tuple(mean)) for cells, bits, mean in sig["frames"]]
                sig["mean"], sig["main"] = tuple(sig["mean"]), tuple(sig["main"])
                sig["palette"] = [(share, tuple(color)) for share, color in sig["palette"]]
            cache[key] = sig
        return cache
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def save_cache(cache, limit=20000):
    """Save fingerprints (the newest `limit` of them) so re-scans are instant."""
    try:
        items = list(cache.items())[-limit:]
        os.makedirs(os.path.dirname(_cache_path()), exist_ok=True)
        tmp = _cache_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": VERSION, "files": dict(items)}, f)
        os.replace(tmp, _cache_path())
    except OSError:
        pass
