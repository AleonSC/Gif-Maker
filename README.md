# GifMaker

A small Windows desktop app for turning a section of a video into a GIF.

1. **Open video...** (mp4, mkv, mov, avi, webm, ...), drag a video from Explorer into the
   window, or drop one onto `GifMaker.exe`.
2. Press **Play** (or the spacebar) to watch it, or scrub with the slider / step buttons /
   ←→ arrow keys to find the part you want. Playback is video only, without sound.
   While you drag, the preview shows quick keyframe thumbnails; the exact frame appears
   as soon as you stop.
3. Click **Set to current** for Start and End (or press `[` and `]`), or type times like `12.5` or `1:02.25`.
4. Click **Play clip** to loop just that section and check it before making the GIF.
5. Choose FPS, width, speed, color count and looping, then click **Make GIF...**.
   GIFs can be up to 30 seconds long (after the speed change).

**Save snapshot...** saves the frame you're looking at as a full-resolution PNG or JPEG.

GIFs are made with ffmpeg's two-pass palette method, which gives much better colors than a
plain conversion. ffmpeg is bundled inside the exe, so there is nothing else to install.

## Getting the exe

GitHub Actions builds `GifMaker.exe` on every push (see `.github/workflows/build-windows.yml`):
open the **Actions** tab, pick the latest *Build Windows exe* run and download the
`GifMaker-windows` artifact. Pushing a tag like `v1.0` also attaches the exe to a GitHub release.

Windows SmartScreen may warn the first time you run it because the exe isn't code-signed:
click *More info → Run anyway*.

### Building it yourself

On Windows with Python 3.10+ installed, run `build.bat`. The exe ends up at `dist\GifMaker.exe`.

### Running from source

```
pip install -r requirements.txt
python run_gifmaker.py
```
