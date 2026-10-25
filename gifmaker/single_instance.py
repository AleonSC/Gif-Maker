"""Lets a running GifMaker take videos opened from Explorer, instead of a new window.

The running app listens on a local port and writes the port (and a random key,
so nothing else can talk to it by accident) to a file. A new copy started with
a video reads the file, hands the video over and quits.
"""

import os
import secrets
import socket
import threading

TIMEOUT = 2.0  # seconds to wait for the running copy


def _path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "GifMaker", "instance.txt")


def hand_over(video):
    """Give video to a running GifMaker. Returns True if it took it."""
    try:
        with open(_path(), encoding="utf-8") as f:
            port, key = f.read().split()
        with socket.create_connection(("127.0.0.1", int(port)), timeout=TIMEOUT) as s:
            s.sendall(f"{key}\n{os.path.abspath(video)}".encode("utf-8"))
            s.shutdown(socket.SHUT_WR)
            return s.recv(2) == b"ok"
    except (OSError, ValueError):
        return False  # none running (or a stale file): open normally


class Listener:
    """Accepts videos from new copies; on_video(path) is called on a background thread."""

    def __init__(self, on_video):
        self.on_video = on_video
        self.key = secrets.token_hex(16)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
        with open(_path(), "w", encoding="utf-8") as f:
            f.write(f"{self.port} {self.key}")
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return  # closed
            with conn:
                try:
                    conn.settimeout(TIMEOUT)
                    data = b""
                    while len(data) < 65536:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    key, _, path = data.decode("utf-8").partition("\n")
                    if key == self.key and os.path.isfile(path):
                        conn.sendall(b"ok")
                        self.on_video(path)
                except (OSError, UnicodeDecodeError):
                    pass

    def close(self):
        try:
            self.sock.close()
            with open(_path(), encoding="utf-8") as f:
                mine = f.read().split()[:1] == [str(self.port)]
            if mine:  # a newer copy may have taken over the file
                os.remove(_path())
        except OSError:
            pass
