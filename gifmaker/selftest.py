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

    def gif():
        d = tempfile.mkdtemp()
        src, dst = os.path.join(d, "in.mp4"), os.path.join(d, "out.gif")
        subprocess.run([exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=320x180:rate=30", "-t", "2", "-pix_fmt", "yuv420p", src],
                       check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        vf = ff.transform_filters(90, True, (0.1, 0.1, 0.5, 0.5))
        err = ff.run_with_progress(ff.build_gif_command(exe, src, dst, 0.5, 1.5, 10, 0, pre_vf=vf), 1.0)
        assert err is None, err
        return f"{os.path.getsize(dst)} bytes"

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

    check("ffmpeg", ffmpeg)
    check("gif", gif)
    check("sound", sound)
    check("drag and drop", drag_and_drop)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(results) + "\n")
    return 0 if all(r.startswith("ok") for r in results) else 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1]))
