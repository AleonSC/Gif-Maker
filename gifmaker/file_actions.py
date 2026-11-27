"""What can be done to a file from the library or its right-click menu: mark it seen or
unseen, rename, move or copy it, show it in Explorer, copy its path, delete it.

Each takes the file's path. Anything reading the file (the editor, the library's
preview, the grid player...) lets go of it first (app.let_go), and app.files_changed()
is called afterwards so every list of the folder is refreshed.
"""

import os
import shutil
import sys
import threading
from tkinter import filedialog, messagebox

from . import files
from . import seen
from . import theme
from . import ui_thread


def same(a, b):
    return bool(a and b) and os.path.normcase(os.path.abspath(a)) == \
        os.path.normcase(os.path.abspath(b))


class FileActions:
    def __init__(self, app):
        self.app = app

    def _status(self, text):
        self.app.status.set(text)

    def usable(self, path):
        """path, if it can be changed now (else None, with the reason shown)."""
        if not path:
            return None
        if self.app.busy:
            self._status("Wait for the save to finish (or cancel it) first.")
            return None
        if not os.path.exists(path):
            self._status(f"{os.path.basename(path)} isn't there any more.")
            return None
        return path

    # ------------------------------------------------------------- marks
    def mark(self, path, state):
        """Mark path seen or unseen (None clears it); kept between sessions."""
        if not path:
            return
        seen.mark(path, state)
        self.app.marks_changed(path)
        name = os.path.basename(path)
        self._status(f"Marked {name} as {state}." if state else f"Cleared the mark on {name}.")

    # ------------------------------------------------------------ simple
    def reveal(self, path):
        if self.usable(path):
            files.reveal([path])

    def copy_path(self, path):
        if path:
            root = self.app.root
            root.clipboard_clear()
            root.clipboard_append(os.path.abspath(path))
            self._status(f"Copied the path of {os.path.basename(path)}.")

    # ------------------------------------------------------------ delete
    def delete(self, path, ask=True):
        if not self.usable(path):
            return
        if ask and not messagebox.askyesno(
                "Delete file", f"Delete “{os.path.basename(path)}”?"
                f"\n\n{files.where_it_goes()}", icon="warning", parent=self.app.root):
            return
        if same(path, self.app.video_path):
            self.app.sections = []  # already confirmed
        self.app.let_go(path)
        self._delete_when_free(path, tries=15)

    def _delete_when_free(self, path, tries):
        try:
            files.send_to_trash(path)
        except OSError as e:
            if tries > 0 and os.path.exists(path):
                self.app.root.after(150, lambda: self._delete_when_free(path, tries - 1))
                return
            messagebox.showerror("Couldn't delete", f"{os.path.basename(path)} wasn't deleted:"
                                 f"\n{e}", parent=self.app.root)
            return
        self.app.files_changed()
        recycled = " (it's in the Recycle Bin)" if sys.platform == "win32" else ""
        self._status(f"Deleted {os.path.basename(path)}{recycled}.")

    # ------------------------------------------------------ move and copy
    def move_or_copy(self, path, copy):
        """Ask for a folder, then move (or copy) path there."""
        if not self.usable(path):
            return
        last = theme.load_setting("quick_look_folder", "") or os.path.dirname(path)
        folder = filedialog.askdirectory(parent=self.app.root, initialdir=last,
                                         title=("Copy" if copy else "Move") + " to this folder")
        if not folder:
            return
        theme.save_setting("quick_look_folder", folder)
        (self.copy_to if copy else self.move_to)(path, folder)

    def move_to(self, path, folder):
        if not self.usable(path):
            return
        if not os.path.isdir(folder):
            self._status(f"That folder isn't there any more: {folder}")
            return
        if same(os.path.dirname(os.path.abspath(path)), folder):
            self._status("It's already in that folder.")
            return
        is_open = same(path, self.app.video_path)
        if is_open and self.app.sections and not messagebox.askyesno(
                "Move file", f"Moving it clears your {len(self.app.sections)} section(s). "
                "Move it anyway?", parent=self.app.root):
            return
        if is_open:
            self.app.sections = []
        dst = files.free_name(folder, os.path.basename(path))
        where = os.path.basename(os.path.normpath(folder)) or folder
        self.app.let_go(path)
        self._status(f"Moving {os.path.basename(path)}...")

        def work():
            try:
                files.move_file(path, dst)
                error = None
            except OSError as e:
                error = e
            ui_thread.call(lambda: self._moved(path, dst, where, error))

        threading.Thread(target=work, daemon=True).start()

    def _moved(self, src, dst, where, error):
        self.app.files_changed()
        if error:
            messagebox.showerror("Couldn't move", f"{os.path.basename(src)} wasn't moved:"
                                 f"\n{error}", parent=self.app.root)
            return
        self.app.last_sort = (src, dst)  # Ctrl+Z puts it back
        self._status(f"Moved {os.path.basename(src)} to {where}  (Ctrl+Z puts it back).")

    def copy_to(self, path, folder):
        if not self.usable(path):
            return
        dst = files.free_name(folder, os.path.basename(path))
        self._status(f"Copying {os.path.basename(path)}...")

        def work():
            try:
                shutil.copy2(path, dst)
                error = None
            except OSError as e:
                error = e
            ui_thread.call(lambda: self._copied(path, dst, error))

        threading.Thread(target=work, daemon=True).start()

    def _copied(self, src, dst, error):
        if error:
            messagebox.showerror("Couldn't copy", f"{os.path.basename(src)} wasn't copied:"
                                 f"\n{error}", parent=self.app.root)
            return
        self.app.files_changed()
        self._status(f"Copied {os.path.basename(src)} to {os.path.dirname(dst)}.")

    # ------------------------------------------------------------ rename
    def rename(self, path, name, on_done=None):
        """Rename path to name (without the extension, which stays). on_done(new path)
        runs once it's renamed."""
        if not self.usable(path):
            return
        name = name.strip().rstrip(".")
        folder = os.path.dirname(os.path.abspath(path))
        old = os.path.basename(path)
        stem, ext = os.path.splitext(old)
        if not name or name == stem:
            return
        if any(c in files.INVALID_NAME_CHARS or ord(c) < 32 for c in name):
            messagebox.showerror("Can't rename", "A file name can't contain any of these: "
                                 + " ".join(files.INVALID_NAME_CHARS), parent=self.app.root)
            return
        new = name + ext
        if os.path.exists(os.path.join(folder, new)) and \
                os.path.normcase(new) != os.path.normcase(old):
            messagebox.showerror("Can't rename", f"“{new}” already exists in this "
                                 "folder.", parent=self.app.root)
            return
        reopen = self.app.let_go(path)
        self._rename_when_free(folder, old, new, reopen, on_done, tries=15)

    def _rename_when_free(self, folder, old, new, reopen, on_done, tries):
        try:
            files.rename_all(folder, [(old, new)])
        except OSError as e:
            if tries > 0 and not isinstance(e, FileExistsError):
                self.app.root.after(150, lambda: self._rename_when_free(
                    folder, old, new, reopen, on_done, tries - 1))
                return
            reopen({})
            messagebox.showerror("Couldn't rename", str(e), parent=self.app.root)
            return
        new_path = os.path.join(folder, new)
        reopen({os.path.normcase(os.path.join(folder, old)): new_path})
        self.app.files_changed()
        self._status(f"Renamed to {new}")
        if on_done:
            on_done(new_path)
