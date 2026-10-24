"""Deleting files the friendly way: to the Recycle Bin on Windows."""

import os
import sys


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
