"""Audio playback for the preview, which also serves as the playback clock."""

import threading
from array import array
import time

from . import ffmpeg_tools as ff

RATE = 48000
CHANNELS = 2
BYTES_PER_FRAME = 2 * CHANNELS  # s16le
PREBUFFER = 0.15  # seconds of audio decoded before the sound starts
MAX_BUFFER = 2.0  # seconds of audio to decode ahead at most
STALL_LIMIT = 0.5  # if the sound device stops pulling audio this long, keep time without it

try:
    import sounddevice
except Exception:  # missing package or no PortAudio library
    sounddevice = None


class AudioOutput:
    """Plays a video's audio from `start` and reports how far playback has got.

    clock() is the media time (seconds since `start`) currently coming out of
    the speakers, so video frames timed against it stay in sync with the sound.
    """

    def __init__(self, ffmpeg, path, start, get_gain, speed=1.0):
        self.get_gain = get_gain  # volume 0..1 (0 = muted), read on every audio block
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.eof = False
        self.closed = False
        self.close_lock = threading.Lock()
        self.frames_played = 0
        # (media seconds at the start of the last block, its DAC time), for clock()
        self.last_block = None
        self.wall_base = None  # once the audio runs out, keep time with the wall clock
        self.last_t = (0.0, None)  # (last clock value, when it last moved)
        # Open the device first so a failure here doesn't leave ffmpeg running.
        self.stream = sounddevice.RawOutputStream(
            samplerate=RATE, channels=CHANNELS, dtype="int16", callback=self._callback)
        self.proc = ff.open_audio_stream(ffmpeg, path, start, RATE, CHANNELS, speed)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while not self.closed:
            with self.lock:
                buffered = len(self.buf) / (RATE * BYTES_PER_FRAME)
            if buffered > MAX_BUFFER:
                time.sleep(0.05)
                continue
            chunk = self.proc.stdout.read(8192)
            if not chunk:
                break
            with self.lock:
                self.buf += chunk
        self.eof = True

    def _callback(self, outdata, frames, time_info, status):
        n = frames * BYTES_PER_FRAME
        with self.lock:
            chunk = bytes(self.buf[:n - n % BYTES_PER_FRAME])
            del self.buf[:len(chunk)]
            self.last_block = (self.frames_played / RATE, time_info.outputBufferDacTime)
            self.frames_played += len(chunk) // BYTES_PER_FRAME
        gain = self.get_gain()
        if gain <= 0.0:
            chunk = bytes(len(chunk))
        elif gain < 0.999:
            samples = array("h")
            samples.frombytes(chunk)
            chunk = array("h", [int(v * gain) for v in samples]).tobytes()
        outdata[:len(chunk)] = chunk
        if len(chunk) < n:
            outdata[len(chunk):] = bytes(n - len(chunk))

    def start(self):
        """Start the sound once a little audio is buffered (or the track has ended)."""
        deadline = time.monotonic() + 2.0
        while (not self.eof and not self.closed and time.monotonic() < deadline
               and len(self.buf) < PREBUFFER * RATE * BYTES_PER_FRAME):
            time.sleep(0.005)
        with self.close_lock:  # never start a stream that close() has shut (or is shutting)
            if not self.closed:
                self.stream.start()

    def clock(self):
        if self.closed:  # the device is gone; just report where we got to
            return self.last_t[0]
        if self.wall_base is not None:
            return self.wall_base[0] + time.monotonic() - self.wall_base[1]
        with self.lock:
            last_block, played = self.last_block, self.frames_played
            drained = self.eof and not self.buf
        if last_block is None:
            return 0.0
        media, dac_time = last_block
        now = self.stream.time
        if dac_time > 0 and now > 0:
            t = media + max(0.0, now - dac_time)
            t = min(t, played / RATE)
        else:  # host API without timing info
            t = max(0.0, played / RATE - self.stream.latency)
        mono = time.monotonic()
        if t > self.last_t[0] or self.last_t[1] is None:
            self.last_t = (t, mono)
        if (drained and now >= dac_time) or mono - self.last_t[1] > STALL_LIMIT:
            # The audio ran out (track shorter than the video) or the device stalled:
            # carry on with the wall clock so the video doesn't freeze.
            self.wall_base = (t, mono)
        return t

    def close(self):
        """Stop the sound. Safe to call more than once and from any thread."""
        with self.close_lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.stream.abort()
                self.stream.close()
            except Exception:
                pass
        self.proc.kill()
        self.proc.wait()


def open_audio(ffmpeg, path, start, get_gain, speed=1.0):
    """Return an AudioOutput (call start() when ready), or None if there's no sound."""
    if sounddevice is None:
        return None
    try:
        return AudioOutput(ffmpeg, path, start, get_gain, speed)
    except Exception:
        return None
