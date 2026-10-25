"""`GifMaker.exe --selftest REPORT` checks the packaged app can load everything it needs.

Used by the build to catch missing DLLs or data files in the .exe. Writes one line
per check to REPORT (a windowed .exe has no console) and exits non-zero on failure.
"""

import os
import subprocess
import sys
import tempfile
import traceback


def run(report_path):
    results = []

    def check(name, fn):
        try:
            results.append(f"ok   {name}: {fn()}")
        except Exception:
            results.append(f"FAIL {name}: {traceback.format_exc(limit=2).strip()}")

    from . import ffmpeg_tools as ff

    exe = None

    def ffmpeg():
        nonlocal exe
        exe = ff.find_ffmpeg()
        return exe

    src = None

    def source():
        nonlocal src
        src = os.path.join(tempfile.mkdtemp(), "in.mp4")
        subprocess.run([exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=320x180:rate=30", "-f", "lavfi", "-i", "sine",
                        "-t", "2", "-pix_fmt", "yuv420p", src],
                       check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return ff.probe(exe, src)

    def gif():
        dst = os.path.join(os.path.dirname(src), "out.gif")
        vf = ff.transform_filters(90, True, (0.1, 0.1, 0.5, 0.5))
        err = ff.run_with_progress(ff.build_animation_command(exe, src, dst, 0.5, 1.5, 10, 0,
                                                              pre_vf=vf), 1.0)
        assert err is None, err
        return f"{os.path.getsize(dst)} bytes"

    def animations():
        made = []
        for fmt, (ext, _) in ff.ANIMATION_FORMATS.items():
            if fmt not in ff.available_formats(exe):
                raise AssertionError(f"this ffmpeg can't write {fmt}")
            dst = os.path.join(os.path.dirname(src), "boomerang" + ext)
            err = ff.run_with_progress(ff.build_animation_command(
                exe, src, dst, 0.5, 1.0, 10, 160, fmt=fmt, direction="Boomerang"), 1.0)
            assert err is None, f"{fmt}: {err}"
            made.append(f"{fmt} {os.path.getsize(dst)} bytes")
        return ", ".join(made)

    def stream_file():
        # .ts files (common for downloaded streams) crashed some older ffmpeg builds.
        ts = os.path.join(os.path.dirname(src), "stream.ts")
        subprocess.run([exe, "-y", "-loglevel", "error", "-i", src, "-c:v", "libx264",
                        "-c:a", "aac", "-f", "mpegts", ts],
                       check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        dst = os.path.join(os.path.dirname(src), "from_ts.mp4")
        err = ff.run_with_progress(ff.build_clip_command(exe, ts, dst, [(0.2, 1.2)], True), 1.0)
        assert err is None, err
        return str(ff.probe(exe, ts))

    def recycle_bin():
        from . import files
        path = os.path.join(tempfile.mkdtemp(), "delete me.txt")
        with open(path, "w") as f:
            f.write("GifMaker self-test")
        files.send_to_trash(path)
        assert not os.path.exists(path)
        return files.where_it_goes()

    def video():
        dst = os.path.join(os.path.dirname(src), "clip.mp4")
        err = ff.run_with_progress(ff.build_clip_command(exe, src, dst, [(0.2, 0.7), (1.0, 1.5)], True,
                                                      ff.transform_filters(90)), 1.0)
        assert err is None, err
        info = ff.probe(exe, dst)
        assert info.has_audio and abs(info.duration - 1.0) < 0.1, info
        return f"{os.path.getsize(dst)} bytes, {info}"

    def sound():
        import sounddevice
        return f"PortAudio {sounddevice.get_portaudio_version()[1]}"

    def drag_and_drop():
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
        try:
            return "tkdnd " + root.tk.call("package", "require", "tkdnd")
        finally:
            root.destroy()

    def dark_mode():
        import tkinter as tk
        from tkinter import ttk
        from . import theme
        root = tk.Tk()
        try:
            theme.apply(root, True)
            dark = ttk.Style(root).theme_use()
            theme.apply(root, False)
            return (f"dark uses {dark}, light uses {ttk.Style(root).theme_use()}; "
                    f"Windows prefers dark: {theme.system_prefers_dark()}")
        finally:
            root.destroy()

    check("ffmpeg", ffmpeg)
    check("source video", source)
    check("gif", gif)
    check("animations", animations)
    check("video clip", video)
    check("stream (.ts) file", stream_file)
    check("delete to the Recycle Bin", recycle_bin)
    check("sound", sound)
    check("drag and drop", drag_and_drop)
    check("dark mode", dark_mode)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(results) + "\n")
    return 0 if all(r.startswith("ok") for r in results) else 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1]))
