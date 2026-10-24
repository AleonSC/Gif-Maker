"""Light and dark colour schemes for the whole app.

Light mode keeps the native Windows look. Dark mode switches ttk to the "clam"
theme (the native one can't be recoloured) and paints it with the palette below.
Code that draws its own colours reads them from `colors`, and registers a
callback with on_change() to redraw when the mode is switched.
"""

import json
import os
import sys
import tkinter as tk
from tkinter import ttk

LIGHT = {
    "bg": "#f0f0f0",  # replaced by the native theme's own background in apply()
    "fg": "#000000", "muted": "#5f5f5f", "error": "#c00000",
    "field": "#ffffff", "select": "#0078d7", "select_fg": "#ffffff",
    "track": "#c8c8c8", "range": "#9cc3f5", "lane": "#e6e6e6", "playhead": "#222222",
    "tip": "#ffffe0", "tip_fg": "#000000",
}
DARK = {
    "bg": "#1e1e1e", "fg": "#e6e6e6", "muted": "#a3a3a3", "error": "#ff6b6b",
    "button": "#2d2d2d", "button_hover": "#3a3a3a", "button_pressed": "#454545",
    "border": "#4a4a4a", "field": "#2a2a2a", "select": "#2f6db3", "select_fg": "#ffffff",
    "trough": "#333333", "disabled": "#6f6f6f",
    "track": "#4a4a4a", "range": "#2f5f9e", "lane": "#2a2a2a", "playhead": "#f0f0f0",
    "tip": "#333333", "tip_fg": "#eeeeee",
}
LIGHT_THEME = "vista" if sys.platform == "win32" else "clam"

colors = dict(LIGHT)
dark = False
_listeners = []


def on_change(callback):
    """Call callback() whenever the colours change."""
    _listeners.append(callback)


def apply(root, use_dark):
    """Switch every window to the light or dark scheme."""
    global dark
    dark = use_dark
    style = ttk.Style(root)
    if use_dark:
        style.theme_use("clam")
        colors.clear()
        colors.update(DARK)
        _style_dark(style, root)
    else:
        try:
            style.theme_use(LIGHT_THEME)
        except tk.TclError:
            style.theme_use("clam")
        colors.clear()
        colors.update(LIGHT)
        if style.theme_use() == "clam":
            # clam keeps colours that were set on it, so put its defaults back.
            _style_clam_light(style)
        colors["bg"] = style.lookup("TFrame", "background") or "#f0f0f0"
    for name in ("Muted.TLabel", "Error.TLabel"):
        style.configure(name, foreground=colors["muted" if name.startswith("Muted") else "error"])
    root.configure(bg=colors["bg"])
    _combobox_lists(root)
    for window in _all_windows(root):
        dark_title_bar(window)
    for callback in list(_listeners):
        try:
            callback()
        except tk.TclError:  # its window has been closed
            _listeners.remove(callback)


def _style_dark(style, root):
    c = colors
    style.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                    bordercolor=c["border"], lightcolor=c["button"], darkcolor=c["button"],
                    troughcolor=c["trough"], selectbackground=c["select"],
                    selectforeground=c["select_fg"], insertcolor=c["fg"], focuscolor=c["select"],
                    arrowcolor=c["fg"])
    style.map(".", foreground=[("disabled", c["disabled"])],
              background=[("disabled", c["bg"])])
    style.configure("TButton", background=c["button"])
    style.map("TButton", background=[("pressed", c["button_pressed"]), ("active", c["button_hover"]),
                                     ("disabled", c["bg"])],
              lightcolor=[("pressed", c["button_pressed"])], darkcolor=[("pressed", c["button_pressed"])])
    for widget in ("TCheckbutton", "TRadiobutton"):
        style.configure(widget, indicatorbackground=c["field"], indicatorforeground=c["fg"])
        style.map(widget, background=[("active", c["bg"])],
                  indicatorbackground=[("pressed", c["button_pressed"]), ("disabled", c["bg"])])
    for widget in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(widget, fieldbackground=c["field"], foreground=c["fg"],
                        background=c["button"], arrowcolor=c["fg"])
        style.map(widget, fieldbackground=[("readonly", c["field"]), ("disabled", c["bg"])],
                  foreground=[("disabled", c["disabled"])],
                  selectbackground=[("readonly", c["field"])], selectforeground=[("readonly", c["fg"])],
                  background=[("active", c["button_hover"])])
    style.configure("TLabelframe", background=c["bg"], bordercolor=c["border"])
    style.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"])
    style.configure("Horizontal.TScale", background=c["button"], troughcolor=c["trough"])
    style.configure("Horizontal.TProgressbar", background=c["select"], troughcolor=c["trough"])
    style.configure("Nav.TButton", background=c["button"])


def _style_clam_light(style):
    # clam's own defaults
    bg = "#dcdad5"
    style.configure(".", background=bg, foreground="#000000", fieldbackground="#ffffff",
                    bordercolor="#9e9a91", lightcolor="#eeebe7", darkcolor="#cfcdc8",
                    troughcolor="#bab5ab", selectbackground="#4a6984", selectforeground="#ffffff",
                    insertcolor="#000000", focuscolor="#4a6984", arrowcolor="#000000")
    style.map(".", foreground=[("disabled", "#999999")], background=[("disabled", bg)])
    style.configure("TButton", background=bg)
    style.map("TButton", background=[("pressed", "#bab5ab"), ("active", "#eeebe7"), ("disabled", bg)],
              lightcolor=[("pressed", "#bab5ab")], darkcolor=[("pressed", "#bab5ab")])
    for widget in ("TCheckbutton", "TRadiobutton"):
        style.configure(widget, indicatorbackground="#ffffff", indicatorforeground="#000000")
        style.map(widget, background=[("active", "#eeebe7")],
                  indicatorbackground=[("pressed", bg), ("disabled", bg)])
    for widget in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(widget, fieldbackground="#ffffff", foreground="#000000", background=bg,
                        arrowcolor="#000000")
        style.map(widget, fieldbackground=[("readonly", "#ffffff"), ("disabled", bg)],
                  foreground=[("disabled", "#999999")],
                  selectbackground=[("readonly", "#ffffff")], selectforeground=[("readonly", "#000000")],
                  background=[("active", "#eeebe7")])
    style.configure("TLabelframe", background=bg, bordercolor="#9e9a91")
    style.configure("TLabelframe.Label", background=bg, foreground="#000000")
    style.configure("Horizontal.TScale", background=bg, troughcolor="#bab5ab")
    style.configure("Horizontal.TProgressbar", background="#4a6984", troughcolor="#bab5ab")
    style.configure("Nav.TButton", background=bg)


def _all_windows(root):
    windows = [root]
    for child in root.winfo_children():
        if isinstance(child, tk.Toplevel):
            windows += _all_windows(child)
    return windows


def _combobox_lists(root):
    """Colour the drop-down lists of comboboxes (they're separate windows)."""
    c = colors
    for option, value in (("background", c["field"]), ("foreground", c["fg"]),
                          ("selectBackground", c["select"]), ("selectForeground", c["select_fg"])):
        root.option_add(f"*TCombobox*Listbox.{option}", value)

    def walk(widget):
        for child in widget.winfo_children():
            if isinstance(child, ttk.Combobox):
                try:  # lists that already exist don't pick up the new options
                    popdown = root.tk.call("ttk::combobox::PopdownWindow", child)
                    root.tk.call(f"{popdown}.f.l", "configure", "-background", c["field"],
                                 "-foreground", c["fg"], "-selectbackground", c["select"],
                                 "-selectforeground", c["select_fg"])
                except tk.TclError:
                    pass
            walk(child)

    walk(root)


def dark_title_bar(window):
    """Match the Windows title bar to the scheme (Windows 10 2004+ and 11)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (19 on older builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


def style_window(window):
    """Call for each new Toplevel so it matches the current scheme."""
    window.configure(bg=colors["bg"])
    dark_title_bar(window)


# ------------------------------------------------------------- settings
def _settings_path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "GifMaker", "settings.json")


def system_prefers_dark():
    """Whether Windows is set to dark mode for apps."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def load_dark_setting():
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            return bool(json.load(f)["dark"])
    except (OSError, ValueError, KeyError, TypeError):
        return system_prefers_dark()


def save_dark_setting(use_dark):
    path = _settings_path()
    try:
        settings = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                settings = json.load(f)
        settings["dark"] = use_dark
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f)
    except (OSError, ValueError):
        pass
