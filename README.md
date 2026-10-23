# GifMaker

A small Windows desktop app for turning a section of a video into a GIF.

1. **Open video...** (mp4, mkv, mov, avi, webm, ...), drag a video from Explorer into the
   window, or drop one onto `GifMaker.exe`.
2. Press **Play** (or the spacebar) to watch it with sound (untick **Sound** to mute), or
   click and drag on the timeline / use the step buttons / ←→ arrow keys to find the part
   you want. While you drag, the preview shows quick keyframe thumbnails; the exact frame
   appears as soon as you stop.
3. Set the clip by dragging the green (start) and red (end) markers on the timeline, with
   **Set to current** (or `[` and `]`), or by typing times like `12.5` or `1:02.25`.
4. Click **Play clip** to loop just that section. You can drag the markers while it loops
   to fine-tune it.
5. Choose FPS, width, speed, color count and looping, then click **Make GIF...**.
   GIFs can be up to 30 seconds long (after the speed change).
6. A preview window plays the GIF's frames on a loop. Rotate, flip or crop it (drag on
   the picture; pick a crop shape like 1:1 or 16:9 to lock it), then click **Save GIF...**.

**Save snapshot...** saves the frame you're looking at as a full-resolution PNG or JPEG,
with the same preview window for rotating and cropping first.

**Save video...** saves the section between the markers as a new MP4 (with its sound),
cut exactly at the markers, after the same rotate/crop preview. To keep several parts of
a video, set the markers and click **Add section** for each part: the sections appear in
the orange strip under the timeline (click one to change it with the markers, then
**Done editing**), and **Save video...** joins them in time order and cuts out the rest.

**Clear** closes the current video so you can start over, and **Show folder after
saving** opens Explorer on each file you save.

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
