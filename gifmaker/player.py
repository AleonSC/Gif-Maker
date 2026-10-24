"""Streams video frames (and optionally sound) from ffmpeg into a Tk window."""

import queue
import threading
import time

from . import audio as audio_out
from . import ffmpeg_tools as ff


class Player:
    """Plays a video in the background and hands frames to on_frame on the Tk thread.

    on_frame(t, ppm_bytes) gets each frame with its time in the video; on_end()
    runs when playback reaches `end` or the end of the file (not after stop()).
    With sound, frames are timed against the audio clock so they stay in sync.
    """

    def __init__(self, root, ffmpeg, on_frame, on_end):
        self.root = root
        self.ffmpeg = ffmpeg
        self.on_frame = on_frame
        self.on_end = on_end
        self.playing = False
        self.end = None  # video time to stop at; may be changed while playing
        self.muted = False  # read by the audio thread
        self.volume = 1.0  # 0..1, read by the audio thread
        self.boost = 1.0  # 1..4 multiplies the volume to make quiet videos louder
        self.no_sound_device = False  # set if sound was wanted but couldn't play
        self._token = 0
        self._proc = None
        self._audio = None
        self._queue = queue.Queue()
        self._thread = None
        self._closed = False
        self._poll()

    def close(self):
        """Stop for good; call before destroying the window the player draws into."""
        self.stop()
        self._closed = True
        # Let the playback thread finish closing its sound, so none is still being
        # set up when the app exits (which can crash the sound library).
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)

    def start(self, path, start, size, fps, end=None, pre_vf="", speed=1.0, sound=False):
        self.stop()
        self._token += 1
        self.playing = True
        self.end = end
        w, h = size
        self._proc = ff.open_raw_stream(self.ffmpeg, path, start, w, h, fps,
                                        pre_vf=pre_vf, speed=speed)
        self._audio = None
        if sound:
            self._audio = audio_out.open_audio(self.ffmpeg, path, start, self._gain, speed)
            self.no_sound_device = self._audio is None
        self._thread = threading.Thread(
            target=self._worker,
            args=(self._token, self._proc, self._audio, start, w, h, fps, speed),
            daemon=True)
        self._thread.start()

    def _gain(self):
        # Squared so the slider feels even to the ear (loudness isn't linear).
        return 0.0 if self.muted else self.volume ** 2 * self.boost

    def stop(self):
        if not self.playing:
            return
        self.playing = False
        self._token += 1
        if self._proc:
            self._proc.kill()
            self._proc = None
        if self._audio:
            self._audio.close()
            self._audio = None

    def _worker(self, token, proc, audio, start, w, h, fps, speed):
        frame_size = w * h * 3
        header = b"P6 %d %d 255\n" % (w, h)
        clock = None
        i = 0
        while token == self._token:
            buf = proc.stdout.read(frame_size)
            if len(buf) < frame_size:
                break
            t = start + i * speed / fps
            end = self.end
            if end is not None and t >= end:
                break
            if clock is None:  # start the clock once the first frame is ready
                clock = self._start_clock(audio)
            due = i / fps
            while token == self._token:
                wait = due - clock()
                if wait <= 0:
                    break
                time.sleep(min(wait, 0.01))
            if due - clock() > -0.1:  # drop frames we are too late for
                self._queue.put((token, t, header + buf))
            i += 1
        proc.kill()
        proc.wait()
        if audio:
            audio.close()
        self._queue.put((token, None, None))

    @staticmethod
    def _start_clock(audio):
        if audio:
            try:
                audio.start()
                return audio.clock
            except Exception:
                pass
        t0 = time.monotonic()
        return lambda: time.monotonic() - t0

    def _poll(self):
        if self._closed:
            return
        frame = None
        ended = False
        try:
            while True:
                token, t, data = self._queue.get_nowait()
                if token != self._token:
                    continue
                if data is None:
                    ended = True
                else:
                    frame = (t, data)
        except queue.Empty:
            pass
        if frame and self.playing:
            self.on_frame(*frame)
        if ended and self.playing:
            self.playing = False
            self._proc = self._audio = None
            self.on_end()
        self.root.after(10, self._poll)
