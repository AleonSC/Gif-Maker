# GifMaker

A small Windows desktop app for turning a section of a video into a GIF.

1. **Open...** (Ctrl+O; mp4, mkv, mov, avi, webm, ...), drag a video from Explorer into the
   window, or drop one onto `GifMaker.exe`.
2. Press **Play** (or the spacebar) to watch it with sound (untick **Sound** to mute; tick
   **Loop** to have it start over at the end), or
   click and drag on the timeline / use the step buttons / Shift+←→ to find the part
   you want. While you drag, the preview shows quick keyframe thumbnails; the exact frame
   appears as soon as you stop.
3. Set the clip by dragging the green (start) and red (end) markers on the timeline, with
   the **[** and **]** buttons on the play bar (or the `[` and `]` keys), which also work
   while the video plays, or by typing times like `12.5` or `1:02.25` into **Start** and
   **End** (Enter jumps there). **+ Section** on the play bar keeps the clip as a video
   section (see below).
4. Click **Play clip** to loop just that section. You can drag the markers while it loops
   to fine-tune it.
5. Click **Make GIF...** (right end of the play bar). Animations can be up to 30 seconds
   long (after the speed change; a boomerang counts twice).
6. The Make GIF window plays a live preview on a loop. Set the **Format** (GIF, WebP or
   animated PNG: the last two keep full colour and are often smaller), **Direction**
   (forward, reverse, or boomerang: forward then back, which loops smoothly), **Speed**,
   **FPS**, **Width**, **Colours** and looping there: the preview follows each change, so
   you see the boomerang, the frame rate, the pixel size and the colours straight away.
   They're remembered for next time. You can also rotate, flip or crop it (drag on
   the picture; pick a crop shape like 1:1 or 16:9 to lock it). **Preview result** makes the
   real file and plays it, with its true colours and file size; saving then just keeps it.
   If you've made sections, you can save each one as its own file instead.

**Open link...** (Ctrl+L) downloads a video from YouTube or most other video sites and
opens it, ready to clip: paste the page's link, pick the quality and the folder, and click
**Download and open**. You can also copy a link and press Ctrl+V in the window, or drag a
link from the browser onto it. It uses [yt-dlp](https://github.com/yt-dlp/yt-dlp), which
GifMaker fetches the first time (about 18 MB) and keeps up to date by itself, since sites
change often. RedGifs works with any of its links, including the file links
(media.redgifs.com/Name.mp4) its download button gives. Copy-protected videos (DRM) and ones that need you to sign in can't be
downloaded.

**Snapshot...** saves the frame you're looking at as a full-resolution PNG or JPEG,
with the same preview window for rotating and cropping first.

**Save video...** saves the section between the markers as a new MP4 (with its sound),
cut exactly at the markers, after the same rotate/crop preview. To keep several parts of
a video, set the markers and click **+ Section** for each part: the sections appear in
the orange strip under the timeline (click one to change it with the markers, then
**✓ Done**), and **Save video...** joins them in time order and cuts out the rest, or
saves each section as its own numbered video.

The speed box, **Sound** switch and volume slider on the play bar set the playback speed (the sound keeps its
pitch) and volume; the second, smaller slider (**Boost**) makes quiet videos up to 4x louder (very loud parts may distort). **Delete...** (or the Delete key) deletes the open video, after
asking, to the Recycle Bin, and opens the next one in the folder.

**Files** (top right, or F3) shows the videos in the current folder as thumbnails in a
panel beside the player; click one to open it. The slider at the top of the panel (or
Ctrl + mouse wheel over it) makes the thumbnails bigger or smaller, and the rows refit to
however many fit across; drag the divider between the video and the panel to make it wider
or narrower. Only the thumbnails in view are drawn and made, so folders of thousands of files open
at once and don't keep the computer busy; thumbnails are remembered, so they show at
once next time.

Pictures open too (JPG, PNG, BMP, WebP, TIFF): the timeline and video controls step aside,
the picture fills the window, and **Save copy...** rotates, flips or crops it and saves a
copy, or **Replace original** saves the edit over the file itself (the original goes to the
Recycle Bin, so you can get it back). **‹ ›**, the Files panel, Delete and renaming work with pictures and videos alike.

**Slideshow** (top right, or F5) goes through the folder's files by itself: pick how long
each picture shows (2 seconds to a minute), whether videos play to the end, shuffle, and
whether it starts over at the end. The arrows still work while it runs; F5 or Esc stops it.
Pair it with fullscreen (F11).

**Sort keys** give folders a key each: click *Sort keys...*, add a folder and press a
letter, a number or a spare F key for it. Then, while viewing a file, pressing that key moves
it into that folder and opens the next one (the Files panel updates too). A file with the
same name already there is kept, and the moved one gets " (2)" added. Ctrl+Z puts the last
sorted file back.

Scroll the mouse wheel over the video (or GIF, or picture) to zoom in around the pointer;
drag to move around while zoomed, and middle-click (or zoom back out) to reset. Once
zoomed in, the picture fills the whole video area whatever its shape. Each step shows at
once and sharpens a moment later. It works while playing and in fullscreen.

The **‹** and **›** buttons beside the video (or the ← → arrow keys, or Page Up / Page
Down) open the previous or
next video in the same folder; tick **Autoplay** (top right) to have each one start playing
as soon as it opens. With **One window** ticked (the default), videos you open from
Explorer load into the GifMaker that's already open instead of starting another. Resize the window to make the video bigger or smaller; the picture
scales to fit. **⛶** next to the time (or F11, or double-clicking the video) shows the video
fullscreen with the timeline and play bar underneath; Esc goes back.

**Grid player** (top right) opens a window for playing several videos (and showing pictures) at once. Add them
with **Add...** or by dragging them in from Explorer; drag a video to move it, drag
its corner to resize it (the picture re-sharpens to the new size), click it to play or
pause, and use its green and red markers to loop part of it. **Save layout...** remembers where the panels are and how big (not what's in them,
and as a share of the window, so it fits any window size); pick it from **Layout** to put
the panels back in those spots, and videos you add after that fill its empty spots.
**Snap to other panels** makes edges snap together (side by side, or lined up) when you drag
or resize a panel close to another one or to the window's edge. Pictures can go on the grid too (they get just a title bar, and re-sharpen when resized). **Arrange** tiles them neatly,
and each one has its own sound switch (off to begin with).

**Rename files** (top right) renames everything in a folder with a numbered pattern,
like `1, 2, 3` or `videogames-1, videogames-2` (with optional zero-padding), in name, date,
size or **colour** order. Colour order first splits the pictures and videos into colour and black-and-white: it
judges only the lit subject (dark backgrounds don't count), so a coloured figure on a black
background is in colour, while a black-and-white picture with a tiny coloured detail (a
cigarette's glow, say) stays with the black-and-whites. Colour ones are sorted by the hue of
their coloured parts (reds, oranges, yellows, greens, blues, purples), the black-and-white
ones dark to light after them; each colour group runs dark-to-light then light-to-dark so
neighbours blend. It reads three frames of each video, with black bars ignored. Look-alikes (the same video re-encoded, resized, trimmed
slightly or renamed) are kept next to each other and marked A, B, C... so duplicates are
easy to spot. What it reads is saved, so sorting the same folder again is instant. It shows the old and new names before changing
anything, and **Undo last rename** puts them back.

**Dark** (top right) switches the whole app to dark colours, title bars included. It's
remembered, and the first time it follows your Windows setting.

Double-click the video's name at the top (or press F2) to rename its file; Enter
saves, Esc cancels, and your markers and sections stay put. **Close** closes the current video so you can start over, and **Show in folder after
saving** opens Explorer on each file you save. Hover over any control for a hint.

GIFs are made with ffmpeg's two-pass palette method, which gives much better colors than a
plain conversion. ffmpeg is bundled with the app, so there is nothing else to install.

## Installing and updating

Download **GifMaker-Setup.exe** from the latest release
(https://github.com/AleonSC/Gif-Maker/releases/latest) and run it. It installs for your
user only (no admin password), adds GifMaker to the Start menu, and registers it with
Windows as a video player. Windows only asks whether you trust it this once.

**Making it the default:** tick *Make GifMaker the default for videos* on the installer's
last page (or open *Settings → Apps → Default apps → GifMaker*) and pick the file types you
want, e.g. `.mp4` and `.mkv`. You can also right-click any video, choose *Open with →
Choose another app*, pick GifMaker and tick *Always*. Windows doesn't let apps make
themselves the default, so this one step is yours.

**Updates:** when a newer version is out, an **Update to 1.0.x** button appears at the bottom
right. Click it and GifMaker downloads the new installer, updates itself and reopens, with
your settings kept. Updates it downloads itself don't trigger the "do you trust this" warning.

Every push to the default branch builds a numbered release on GitHub Actions (see
`.github/workflows/build-windows.yml`); other branches' builds are on the **Actions** tab
as artifacts. **GifMaker-portable.zip** runs without installing (unzip it and run
`GifMaker.exe`), but it can't update itself: its update button opens the download page.

### Building it yourself

On Windows with Python 3.10+ installed, run `build.bat`. The app ends up in
`dist\GifMaker\`, and if Inno Setup 6 is installed, the installer at `dist\GifMaker-Setup.exe`.

### Running from source

```
pip install -r requirements.txt
python run_gifmaker.py
```
