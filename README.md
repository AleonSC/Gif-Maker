# GifMaker

A small Windows desktop app for turning a section of a video into a GIF.

1. **Open video...** (mp4, mkv, mov, avi, webm, ...), drag a video from Explorer into the
   window, or drop one onto `GifMaker.exe`.
2. Press **Play** (or the spacebar) to watch it with sound (untick **Sound** to mute), or
   click and drag on the timeline / use the step buttons / ←→ arrow keys to find the part
   you want. While you drag, the preview shows quick keyframe thumbnails; the exact frame
   appears as soon as you stop.
3. Set the clip by dragging the green (start) and red (end) markers on the timeline, with
   the **[** and **]** buttons on the play bar (or the `[` and `]` keys), which also work
   while the video plays, or by typing times like `12.5` or `1:02.25`. **+ Section** on the
   play bar keeps the current loop as a video section (see below).
4. Click **Play clip** to loop just that section. You can drag the markers while it loops
   to fine-tune it.
5. Choose FPS, width, speed, color count and looping, then click **Make GIF...**.
   Animations can be up to 30 seconds long (after the speed change; a boomerang counts twice).
6. A preview window plays the GIF's frames on a loop. Pick the **Format** (GIF, WebP or
   animated PNG: the last two keep full colour and are often smaller) and **Direction**
   (forward, reverse, or boomerang: forward then back, which loops smoothly) there, and
   rotate, flip or crop it (drag on
   the picture; pick a crop shape like 1:1 or 16:9 to lock it). **Preview result** makes the
   real file and plays it, with its true colours and file size; saving then just keeps it.
   If you've made sections, you can save each one as its own file instead.

**Save snapshot...** saves the frame you're looking at as a full-resolution PNG or JPEG,
with the same preview window for rotating and cropping first.

**Save video...** saves the section between the markers as a new MP4 (with its sound),
cut exactly at the markers, after the same rotate/crop preview. To keep several parts of
a video, set the markers and click **Add section** for each part: the sections appear in
the orange strip under the timeline (click one to change it with the markers, then
**Done editing**), and **Save video...** joins them in time order and cuts out the rest, or
saves each section as its own numbered video.

The speed box and **Vol** slider next to the time set the playback speed (the sound keeps its
pitch) and volume. **Delete file...** (or the Delete key) deletes the open video, after
asking, to the Recycle Bin, and opens the next one in the folder.

The **‹** and **›** buttons beside the video (or Page Up / Page Down) open the previous or
next video in the same folder. **⛶** on the play bar (or F11, or double-clicking the video)
shows the video fullscreen with the timeline and play bar underneath; Esc goes back.

**Grid player** (top right) opens a window for playing several videos at once. Add them
with **Add videos...** or by dragging them in from Explorer; drag a video to move it, drag
its corner to resize it (the picture re-sharpens to the new size), click it to play or
pause, and use its green and red markers to loop part of it. **Arrange** tiles them neatly,
and each one has its own sound switch (off to begin with).

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
