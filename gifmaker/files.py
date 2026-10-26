"""File chores: deleting to the Recycle Bin on Windows, and renaming in bulk."""

import json
import os
import re
import sys
import uuid


def where_it_goes():
    """A sentence saying where a deleted file ends up, for messages."""
    if sys.platform == "win32":
        return "It goes to the Recycle Bin, so you can get it back from there."
    return "It's deleted permanently."


def send_to_trash(path):
    """Move path to the Recycle Bin on Windows; elsewhere, delete it.

    Raises OSError if it couldn't be done (e.g. the file is still in use).
    """
    if sys.platform != "win32":
        os.remove(path)
        return
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = os.path.abspath(path) + "\0"  # the list ends with a second NUL
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted or os.path.exists(path):
        raise OSError(f"Windows couldn't move it to the Recycle Bin (code {result}).")


def reveal(paths):
    """Open the folder of paths in Explorer with those files selected (highlighted).

    Uses the shell's own SHOpenFolderAndSelectItems, which reuses a window already
    showing that folder; `explorer /select,` often just opens the folder. All the
    paths must be in the same folder. Returns False if it couldn't be done.
    """
    paths = [os.path.normpath(os.path.abspath(p)) for p in paths if p]
    if not paths:
        return False
    if sys.platform != "win32":
        import subprocess

        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", paths[0]])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(paths[0])])
            return True
        except OSError:
            return False
    import ctypes
    import threading

    def work():
        shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
        shell32.ILFindLastID.restype = ctypes.c_void_p
        shell32.ILFindLastID.argtypes = [ctypes.c_void_p]
        shell32.ILFree.argtypes = [ctypes.c_void_p]
        shell32.SHOpenFolderAndSelectItems.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong]
        ole32.CoInitialize(None)
        folder = shell32.ILCreateFromPathW(os.path.dirname(paths[0]))
        items = [shell32.ILCreateFromPathW(p) for p in paths]
        try:
            children = [shell32.ILFindLastID(i) for i in items if i]
            if folder and children:
                array = (ctypes.c_void_p * len(children))(*children)
                if shell32.SHOpenFolderAndSelectItems(folder, len(children), array, 0) == 0:
                    return
            # The shell call failed: fall back to Explorer's command line (one file).
            import subprocess

            subprocess.Popen(f'explorer /select,"{paths[0]}"')
        finally:
            for pidl in items + [folder]:
                if pidl:
                    shell32.ILFree(pidl)
            ole32.CoUninitialize()

    # In its own thread: the shell call can take a moment and needs COM set up.
    threading.Thread(target=work, daemon=True).start()
    return True


# ------------------------------------------------------------ renaming
SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
TEMP_PREFIX = ".gifmaker-rename-"
INVALID_NAME_CHARS = '<>:"/\\|?*'


def natural_key(name):
    """Sort key that puts "clip 2" before "clip 10", like Explorer."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name.lower())]


def _hidden(path, name):
    if name.startswith(".") or name.lower() in SKIP_NAMES:
        return True
    attributes = getattr(os.stat(path), "st_file_attributes", 0)
    return bool(attributes & 0x6)  # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM


def list_files(folder, extensions=None):
    """Names of the (non-hidden) files directly in folder, in natural order.

    extensions limits it to those (lower-case, with the dot), e.g. {".mp4"}.
    """
    names = []
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if not os.path.isfile(path) or _hidden(path, name):
            continue
        if extensions is None or os.path.splitext(name)[1].lower() in extensions:
            names.append(name)
    return sorted(names, key=natural_key)


def clean_prefix(text):
    """A prefix that's safe in a Windows file name."""
    text = "".join(c for c in text if c not in INVALID_NAME_CHARS and ord(c) >= 32)
    return text.strip().rstrip(".")


def numbered_names(names, prefix="", separator="-", start=1, digits=None):
    """New names for files in the given order: 'prefix-1.ext', or '1.ext' with no prefix.

    digits pads the numbers with zeros ("auto" pads to the widest number), which
    keeps the order in apps that don't sort numbers the way Explorer does.
    """
    prefix = clean_prefix(prefix)
    separator = clean_prefix(separator) if prefix else ""
    last = start + len(names) - 1
    width = len(str(last)) if digits == "auto" else (digits or 0)
    result = []
    for i, name in enumerate(names):
        number = str(start + i).zfill(width)
        result.append(f"{prefix}{separator}{number}{os.path.splitext(name)[1]}")
    return result


def rename_all(folder, plan):
    """Rename files in folder: plan is a list of (old name, new name).

    Everything is first moved to temporary names and then to the new ones, so
    names can swap around. If anything fails, what was done is put back and
    OSError is raised. Returns the (old, new) pairs that changed.
    """
    plan = [(old, new) for old, new in plan if old != new]
    sources = {os.path.normcase(old) for old, _ in plan}
    targets = [os.path.normcase(new) for _, new in plan]
    if len(set(targets)) != len(targets):
        raise OSError("Two files would get the same name.")
    for _, new in plan:
        if os.path.exists(os.path.join(folder, new)) and os.path.normcase(new) not in sources:
            raise FileExistsError(f"“{new}” already exists and isn't one of the files "
                                  "being renamed.")
    tag = uuid.uuid4().hex[:8]
    done = []  # (from, to) names, in the order they happened
    originals = {}  # temporary name -> the file's original name, for messages

    def move(src, dst):
        os.rename(os.path.join(folder, src), os.path.join(folder, dst))
        done.append((src, dst))

    try:
        temps = []
        for i, (old, new) in enumerate(plan):
            temp = f"{TEMP_PREFIX}{tag}-{i}{os.path.splitext(new)[1]}"
            originals[temp] = old
            move(old, temp)
            temps.append((temp, new))
        for temp, new in temps:
            move(temp, new)
    except OSError as e:
        for src, dst in reversed(done):
            try:
                os.rename(os.path.join(folder, dst), os.path.join(folder, src))
            except OSError:
                pass
        name = os.path.basename(getattr(e, "filename", None) or "")
        detail = f" ({originals.get(name, name)})" if name else ""
        raise OSError(f"{e.strerror or e}{detail}. Nothing was renamed.") from e
    return plan


def _undo_path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "GifMaker", "last_rename.json")


def save_undo(folder, renamed):
    """Remember a rename so it can be undone, even after restarting."""
    try:
        os.makedirs(os.path.dirname(_undo_path()), exist_ok=True)
        with open(_undo_path(), "w", encoding="utf-8") as f:
            json.dump({"folder": folder, "renamed": renamed}, f)
    except OSError:
        pass


def load_undo():
    """(folder, [(old, new), ...]) of the last rename, or None."""
    try:
        with open(_undo_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data["folder"], [tuple(pair) for pair in data["renamed"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def forget_undo():
    try:
        os.remove(_undo_path())
    except OSError:
        pass
