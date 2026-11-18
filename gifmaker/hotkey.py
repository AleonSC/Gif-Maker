"""A hotkey that works anywhere, even while GifMaker is minimized (Windows only).

Registered with RegisterHotKey on a background thread that runs its own message
loop; presses are handed to the Tk thread through ui_thread.
"""

import sys
import threading

from . import ui_thread

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012

# Tk keysym -> virtual-key code, for keys that aren't a single letter or digit.
_NAMED = {"Print": 0x2C, "Pause": 0x13, "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24,
          "End": 0x23, "Prior": 0x21, "Next": 0x22, "space": 0x20, "Return": 0x0D,
          "Tab": 0x09, "BackSpace": 0x08, "Left": 0x25, "Up": 0x26, "Right": 0x27,
          "Down": 0x28, "Scroll_Lock": 0x91}
_NAMED.update({f"F{n}": 0x6F + n for n in range(1, 25)})
_LABELS = {"Prior": "Page Up", "Next": "Page Down", "Print": "Print Screen",
           "BackSpace": "Backspace", "space": "Space", "Return": "Enter"}


def supported():
    return sys.platform == "win32"


def vk_of(keysym):
    """Virtual-key code for a Tk keysym, or None."""
    if keysym in _NAMED:
        return _NAMED[keysym]
    if len(keysym) == 1 and keysym.isalnum() and keysym.isascii():
        return ord(keysym.upper())
    return None


def from_event(event):
    """The hotkey (a dict {"mods": int, "vk": int}) a Tk key event describes, or None
    while only modifier keys are down or the key can't be used."""
    vk = vk_of(event.keysym)
    if vk is None:
        return None
    mods = 0
    if event.state & 0x4:
        mods |= MOD_CONTROL
    if event.state & 0x20000 or event.state & 0x8:  # Alt (Windows reports it as 0x20000)
        mods |= MOD_ALT
    if event.state & 0x1:
        mods |= MOD_SHIFT
    is_function = event.keysym.startswith("F") and event.keysym[1:].isdigit()
    if not mods and not is_function and event.keysym not in ("Print", "Pause", "Scroll_Lock"):
        return None  # a bare letter would stop you typing it anywhere
    return {"mods": mods, "vk": vk}


def describe(hotkey):
    """Text like "Ctrl+Shift+S" for a hotkey dict."""
    if not hotkey:
        return "None"
    names = {v: k for k, v in _NAMED.items()}
    key = names.get(hotkey["vk"])
    key = _LABELS.get(key, key) if key else chr(hotkey["vk"])
    parts = [n for bit, n in ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"))
             if hotkey["mods"] & bit]
    return "+".join(parts + [key])


def valid(value):
    return (isinstance(value, dict) and isinstance(value.get("mods"), int)
            and isinstance(value.get("vk"), int))


class GlobalHotkey:
    """One system-wide hotkey. set(hotkey) (re)binds it, set(None) removes it;
    on_press runs on the Tk thread."""

    def __init__(self, on_press):
        self.on_press = on_press
        self._thread = None
        self._thread_id = None
        self._lock = threading.Lock()

    def set(self, hotkey):
        """Bind hotkey; returns None, or a sentence saying why it couldn't be."""
        self.clear()
        if not hotkey:
            return None
        if not supported():
            return "Global hotkeys only work on Windows."
        result = {}
        ready = threading.Event()
        thread = threading.Thread(target=self._run, args=(hotkey, result, ready), daemon=True)
        thread.start()
        ready.wait(2)
        if result.get("error"):
            return result["error"]
        with self._lock:
            self._thread, self._thread_id = thread, result.get("tid")
        return None

    def clear(self):
        with self._lock:
            thread, tid = self._thread, self._thread_id
            self._thread = self._thread_id = None
        if thread and tid and supported():
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            thread.join(1)

    def _run(self, hotkey, result, ready):
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        result["tid"] = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, hotkey["mods"] | MOD_NOREPEAT, hotkey["vk"]):
            result["error"] = (f"{describe(hotkey)} is already used by another program. "
                               "Pick a different one.")
            ready.set()
            return
        ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    ui_thread.call(self.on_press)
        finally:
            user32.UnregisterHotKey(None, 1)
