"""Stress test for the Rename tool's colour order with thousands of files.

    python tools/stress_rename.py FOLDER COUNT [MAX_SWATCHES]

Fills FOLDER with COUNT small files, gives each a made-up fingerprint with its own
palette (so nothing needs reading), opens the Rename tool in colour order and
scrolls through the whole list. Prints "passed" if the window survives. Used by
the Windows build: thousands of distinct swatch pictures once crashed it there.
"""

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gifmaker import app as A  # noqa: E402
from gifmaker import ffmpeg_tools as ff  # noqa: E402
from gifmaker import rename_tool as R  # noqa: E402
from gifmaker import similarity as sim  # noqa: E402
from gifmaker import theme  # noqa: E402


def main():
    folder, count = sys.argv[1], int(sys.argv[2])
    if len(sys.argv) > 3:
        R.MAX_SWATCHES = int(sys.argv[3])
    os.makedirs(folder, exist_ok=True)
    rng = random.Random(1)
    names = [f"pic-{i:05d}.jpg" for i in range(count)]
    for n in names:
        path = os.path.join(folder, n)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(n.encode())
    probe = type("Probe", (), {"folder": folder})()
    cache = {}

    def color():
        return (rng.uniform(0.1, 0.95), rng.uniform(-0.2, 0.2), rng.uniform(-0.2, 0.2))

    for n in names:
        cells = [color() for _ in range(16)]
        mean = tuple(sum(c[i] for c in cells) / 16 for i in range(3))
        palette = sorted(((rng.random(), color()) for _ in range(4)), reverse=True)
        cache[R.RenameTool._color_key(probe, n)] = {
            "duration": 0.0, "frames": [(cells, rng.getrandbits(64), mean)], "mean": mean,
            "palette": palette, "main": palette[0][1], "colorful": rng.random()}
    sim.save_cache(cache)
    R._sigs = None

    root = A.TkinterDnD.Tk() if A.TkinterDnD else A.tk.Tk()
    theme.apply(root, False)
    app = A.GifMakerApp(root, ff.find_ffmpeg())
    started = time.time()

    def go():
        app.open_rename_tool()
        tool = app.rename_window
        tool.load(folder)
        tool.order.set("Colour")
        tool._order_changed()
        wait(tool)

    def wait(tool):
        if tool.reading_colors or tool.sorting or tool.fill_job:
            root.after(200, lambda: wait(tool))
            return
        rows = len(tool.tree.get_children())
        for step in range(101):  # scroll through everything, drawing as we go
            tool.tree.yview_moveto(step / 100)
            root.update()
        print(f"passed: {rows} rows, {len(tool.swatches)} swatch pictures, "
              f"{time.time() - started:.1f} s", flush=True)
        app.close()

    root.after(300, go)
    root.mainloop()


if __name__ == "__main__":
    main()
