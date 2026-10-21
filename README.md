# GifMaker

A small Windows desktop app for turning a section of a video into a GIF.

1. **Open video...** (mp4, mkv, mov, avi, webm, ...) or drop a video onto `GifMaker.exe`.
2. Scrub with the slider (or the step buttons / ←→ arrow keys) to find the part you want.
3. Click **Set to current** for Start and End (or press `[` and `]`), or type times like `12.5` or `1:02.25`.
4. Choose FPS, width, speed, color count and looping, then click **Make GIF...**.

GIFs are made with ffmpeg's two-pass palette method, which gives much better colors than a
plain conversion. ffmpeg is bundled inside the exe, so there is nothing else to install.

## Getting the exe

GitHub Actions builds `GifMaker.exe` on every push (see `.github/workflows/build-windows.yml`):
open the **Actions** tab, pick the latest *Build Windows exe* run and download the
`GifMaker-windows` artifact. Pushing a tag like `v1.0` also attaches the exe to a GitHub release.

Windows SmartScreen may warn the first time you run it because the exe isn't code-signed:
click *More info → Run anyway*.

### Building it yourself

On Windows with Python 3.9+ installed, run `build.bat`. The exe ends up at `dist\GifMaker.exe`.

### Running from source

```
pip install -r requirements.txt
python run_gifmaker.py
```
