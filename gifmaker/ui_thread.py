"""Handing work from background threads to the Tk thread, without ever blocking.

In Tkinter, a background thread that calls widget.after() waits for the main
thread to take the call. Once the window has closed nothing takes it, the
thread hangs, and GifMaker.exe stays running (which also blocks updates).
Background work puts its callables here instead: the window runs them while
it's open, and after it closes they're simply never run.
"""

import queue
import sys
import tkinter as tk

POLL_MS = 15
_calls = queue.SimpleQueue()


def call(fn):
    """Run fn() on the Tk thread soon. Safe from any thread; never waits."""
    _calls.put(fn)


def start(root):
    """Start running queued calls (once, from the Tk thread)."""
    def poll():
        while True:
            try:
                fn = _calls.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except tk.TclError:
                pass  # what it was for has been closed
            except Exception:
                root.report_callback_exception(*sys.exc_info())
        try:
            root.after(POLL_MS, poll)
        except tk.TclError:
            pass  # the app has closed

    poll()
