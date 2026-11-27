"""Quick look: right-click a thumbnail in Files to see it big, with file actions beside it.

The picture (or the video, playing silently on a loop) fills most of the window,
and a panel docked on its right deletes, moves, copies or renames the file, shows
it in Explorer, and so on. ‹ › (or the arrow keys) step through the folder, so a
folder can be gone through and tidied without leaving the window.
"""

import os
import shutil
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import ffmpeg_tools as ff
from . import files
from . import sort_keys
from . import theme
from . import ui_thread
from .player import Player

PANEL_W = 220
MAX_FPS = 30


def _human_size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


class QuickLook(tk.Toplevel):
    """One window, reused: show() points it at another file."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.paths = []
        self.path = None
        self.info = None
        self.token = 0  # bumped on each file: stale frame grabs are dropped
        self.photo = None
        self.render_job = None
        self.renaming = None
        self.player = Player(self, app.ffmpeg, self._on_frame, self._on_end)
        self.title("Quick look")
        theme.style_window(self)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = int(sw * 0.8), int(sh * 0.8)
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 20)}")
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self._escape())
        self.bind("<Left>", lambda e: self._key(e, lambda: self.step(-1)))
        self.bind("<Right>", lambda e: self._key(e, lambda: self.step(1)))
        self.bind("<Delete>", lambda e: self._key(e, self.delete))
        self.bind("<F2>", lambda e: self._key(e, self.start_rename))
        self.bind("<Return>", lambda e: self._key(e, self.open_in_main))
        self.bind("<Control-e>", lambda e: self._key(e, self.reveal))

    # ------------------------------------------------------------ layout
    def _build(self):
        main = ttk.Frame(self)
        main.pack(fill="both", expand=True)

        panel = ttk.Frame(main, padding=10, width=PANEL_W)
        panel.pack(side="right", fill="y")
        panel.pack_propagate(False)
        self.name_label = ttk.Label(panel, style="Head.TLabel", wraplength=PANEL_W - 20,
                                    justify="left")
        self.name_label.pack(anchor="w", fill="x")
        self.rename_holder = ttk.Frame(panel)
        self.rename_holder.pack(fill="x")
        self.details = ttk.Label(panel, style="Muted.TLabel", wraplength=PANEL_W - 20,
                                 justify="left")
        self.details.pack(anchor="w", fill="x", pady=(4, 10))

        def button(text, command):
            b = ttk.Button(panel, text=text, command=command)
            b.pack(fill="x", pady=2)
            return b

        button("Open in GifMaker  (Enter)", self.open_in_main)
        ttk.Separator(panel).pack(fill="x", pady=6)
        button("Rename...  (F2)", self.start_rename)
        button("Move to folder...", lambda: self.move_or_copy(copy=False))
        button("Copy to folder...", lambda: self.move_or_copy(copy=True))
        self.sort_frame = ttk.Frame(panel)
        self.sort_frame.pack(fill="x")
        button("Show in folder  (Ctrl+E)", self.reveal)
        button("Copy file path", self.copy_path)
        ttk.Separator(panel).pack(fill="x", pady=6)
        button("Delete  (Del)", self.delete)
        self.status = ttk.Label(panel, style="Muted.TLabel", wraplength=PANEL_W - 20,
                                justify="left")
        self.status.pack(side="bottom", anchor="w", fill="x")
        nav = ttk.Frame(panel)
        nav.pack(side="bottom", fill="x", pady=(10, 6))
        self.back_btn = ttk.Button(nav, text="‹ Back", command=lambda: self.step(-1))
        self.back_btn.pack(side="left", expand=True, fill="x", padx=(0, 2))
        self.next_btn = ttk.Button(nav, text="Next ›", command=lambda: self.step(1))
        self.next_btn.pack(side="left", expand=True, fill="x", padx=(2, 0))
        self.count_label = ttk.Label(panel, style="Muted.TLabel", anchor="center")
        self.count_label.pack(side="bottom", fill="x")

        self.canvas = tk.Canvas(main, bg="#111", highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas_img = self.canvas.create_image(0, 0, anchor="center")
        self.canvas_text = self.canvas.create_text(0, 0, fill="#888", anchor="center")
        self.canvas.bind("<Configure>", lambda e: self._render_soon())
        self.canvas.bind("<Double-Button-1>", lambda e: self.open_in_main())

    def _fill_sort_buttons(self):
        for child in self.sort_frame.winfo_children():
            child.destroy()
        keys = getattr(self.app, "sort_keys", {}) or {}
        for key, folder in sort_keys._ordered(keys):
            name = os.path.basename(os.path.normpath(folder)) or folder
            ttk.Button(self.sort_frame, text=f"Move to {name}  ({sort_keys.label(key)})",
                       command=lambda f=folder: self.move_to(f)).pack(fill="x", pady=2)

    # ----------------------------------------------------------- showing
    def show(self, path, paths):
        """Show path; paths is the folder's list, for ‹ ›."""
        self.paths = list(paths) if path in paths else [path]
        self._fill_sort_buttons()
        self._load(path)
        self.deiconify()
        self.lift()
        self.focus_force()

    def _load(self, path):
        self._cancel_rename()
        self.player.stop()
        self.token += 1
        self.path = path
        self.info = None
        self.photo = None
        self.canvas.itemconfig(self.canvas_img, image="")
        self.canvas.itemconfig(self.canvas_text, text="Loading...")
        self.status.config(text="")
        self._update_labels()
        token = self.token

        def work():
            try:
                info = ff.probe(self.app.ffmpeg, path)
            except Exception as e:  # noqa: BLE001 - any failure: say so in the window
                info = e
            ui_thread.call(lambda: self._probed(token, info))

        threading.Thread(target=work, daemon=True).start()

    def _probed(self, token, info):
        if token != self.token or not self.winfo_exists():
            return
        if isinstance(info, Exception):
            self.canvas.itemconfig(self.canvas_text, text=f"Can't show this file.\n\n{info}"[:400])
            return
        self.info = info
        self._update_labels()
        self._render()

    def _update_labels(self):
        path = self.path
        self.title(f"{os.path.basename(path)} - Quick look" if path else "Quick look")
        self.name_label.config(text=os.path.basename(path) if path else "")
        lines = []
        try:
            st = os.stat(path)
            lines.append(_human_size(st.st_size))
        except (OSError, TypeError):
            pass
        info = self.info
        if info:
            if info.width:
                lines.append(f"{info.width} × {info.height} px")
            if info.duration and not ff.is_image(path):
                lines.append(ff.format_time(info.duration).split(".")[0])
        if path:
            lines.append(os.path.dirname(path))
        self.details.config(text="\n".join(lines))
        i = self._index()
        n = len(self.paths)
        self.count_label.config(text=f"{i + 1} of {n}" if i is not None and n > 1 else "")
        self.back_btn.config(state="normal" if i else "disabled")
        self.next_btn.config(state="normal" if i is not None and i + 1 < n else "disabled")

    def _view_size(self):
        return max(2, self.canvas.winfo_width()), max(2, self.canvas.winfo_height())

    def _render_soon(self):
        cw, ch = self._view_size()
        self.canvas.coords(self.canvas_img, cw // 2, ch // 2)
        self.canvas.coords(self.canvas_text, cw // 2, ch // 2)
        self.canvas.itemconfig(self.canvas_text, width=max(100, cw - 40))
        if self.render_job:
            self.after_cancel(self.render_job)
        self.render_job = self.after(150, self._render)

    def _render(self):
        self.render_job = None
        info, path = self.info, self.path
        if not info or not path:
            return
        cw, ch = self._view_size()
        w, h = ff.fit_size(info.width, info.height, cw, ch, upscale=False)
        if info.duration > 0 and not ff.is_image(path):
            fps = min(info.fps or 25.0, MAX_FPS)
            self.player.muted = True
            self.player.start(path, 0.0, (w, h), fps)
            return
        token = self.token

        def work():
            data = ff.grab_frame(self.app.ffmpeg, path, 0.0, w, h)
            ui_thread.call(lambda: self._show_still(token, data))

        threading.Thread(target=work, daemon=True).start()

    def _show_still(self, token, data):
        if token != self.token or not self.winfo_exists():
            return
        if not data:
            self.canvas.itemconfig(self.canvas_text, text="Can't show this file.")
            return
        self.photo = tk.PhotoImage(master=self, data=data, format="ppm")
        self.canvas.itemconfig(self.canvas_img, image=self.photo)
        self.canvas.itemconfig(self.canvas_text, text="")

    def _on_frame(self, _t, data):
        self.photo = tk.PhotoImage(master=self, data=data, format="ppm")
        self.canvas.itemconfig(self.canvas_img, image=self.photo)
        self.canvas.itemconfig(self.canvas_text, text="")

    def _on_end(self):
        if self.path and self.winfo_exists():
            self.after(10, self._render)  # loop

    # -------------------------------------------------------- navigation
    def _index(self):
        try:
            return self.paths.index(self.path)
        except ValueError:
            return None

    def step(self, step):
        i = self._index()
        if i is None:
            return
        j = i + step
        if 0 <= j < len(self.paths):
            self._load(self.paths[j])

    def _gone(self, new=None):
        """The current file was moved away or deleted: show the next one (or close)."""
        i = self._index()
        if new is not None and i is not None:
            self.paths[i] = new
            self._load(new)
            return
        if i is not None:
            self.paths.pop(i)
        if not self.paths:
            self.close()
            return
        self._load(self.paths[min(i or 0, len(self.paths) - 1)])

    def _key(self, event, action):
        if isinstance(event.widget, (tk.Entry, ttk.Entry)):
            return None
        action()
        return "break"

    def _escape(self):
        if self.renaming is not None:
            self._cancel_rename()
        else:
            self.close()

    # ----------------------------------------------------------- actions
    def _can_change(self):
        if not self.path:
            return False
        if self.app.busy:
            self.status.config(text="Wait for the save to finish (or cancel it) first.")
            return False
        if not os.path.exists(self.path):
            self.status.config(text="It isn't there any more.")
            return False
        return True

    def _let_go(self, path):
        """Stop everything reading path (here and in the main window). Returns the
        main window's reopen(mapping) (see App._release)."""
        self.player.stop()
        self.token += 1
        key = os.path.normcase(os.path.abspath(path))
        return self.app._release(lambda p: os.path.normcase(os.path.abspath(p)) == key)

    def _folder_changed(self):
        self.app.listing = None
        self.app._update_nav()
        if getattr(self.app, "rename_window", None) is not None:
            try:
                self.app.rename_window.reload()
            except Exception:  # noqa: BLE001 - the rename tool refreshing is a nicety
                pass

    def open_in_main(self):
        if not self.path:
            return
        path = self.path
        self.close()
        self.app._browser_open(path)

    def reveal(self):
        if self.path and os.path.exists(self.path):
            files.reveal([self.path])

    def copy_path(self):
        if self.path:
            self.clipboard_clear()
            self.clipboard_append(os.path.abspath(self.path))
            self.status.config(text="Path copied.")

    def delete(self):
        if not self._can_change():
            return
        path = self.path
        if not messagebox.askyesno("Delete file", f"Delete “{os.path.basename(path)}”?"
                                   f"\n\n{files.where_it_goes()}", icon="warning", parent=self):
            return
        if os.path.normcase(os.path.abspath(path)) == \
                os.path.normcase(os.path.abspath(self.app.video_path or "")):
            self.app.sections = []  # already confirmed
        self._let_go(path)
        self._delete_when_free(path, tries=15)

    def _delete_when_free(self, path, tries):
        try:
            files.send_to_trash(path)
        except OSError as e:
            if tries > 0 and os.path.exists(path):
                self.after(150, lambda: self._delete_when_free(path, tries - 1))
                return
            messagebox.showerror("Couldn't delete", f"{os.path.basename(path)} wasn't deleted:"
                                 f"\n{e}", parent=self)
            self._load(path)
            return
        self._folder_changed()
        recycled = " (it's in the Recycle Bin)" if sys.platform == "win32" else ""
        self.app.status.set(f"Deleted {os.path.basename(path)}{recycled}.")
        if self.path == path:
            self._gone()
        self.status.config(text=f"Deleted {os.path.basename(path)}{recycled}.")

    def move_or_copy(self, copy):
        if not self._can_change():
            return
        last = theme.load_setting("quick_look_folder", "") or os.path.dirname(self.path)
        folder = filedialog.askdirectory(parent=self, initialdir=last,
                                         title=("Copy" if copy else "Move") + " to this folder")
        if not folder:
            return
        theme.save_setting("quick_look_folder", folder)
        if copy:
            self.copy_to(folder)
        else:
            self.move_to(folder)

    def move_to(self, folder):
        if not self._can_change():
            return
        path = self.path
        if not os.path.isdir(folder):
            self.status.config(text=f"That folder isn't there any more: {folder}")
            return
        if os.path.normcase(os.path.dirname(os.path.abspath(path))) == \
                os.path.normcase(os.path.abspath(folder)):
            self.status.config(text="It's already in that folder.")
            return
        dst = files.free_name(folder, os.path.basename(path))
        is_open = os.path.normcase(os.path.abspath(path)) == \
            os.path.normcase(os.path.abspath(self.app.video_path or ""))
        if is_open and self.app.sections and not messagebox.askyesno(
                "Move file", f"Moving it clears your {len(self.app.sections)} section(s) "
                "in the main window. Move it anyway?", parent=self):
            return
        if is_open:
            self.app.sections = []
        self._let_go(path)
        self._gone()  # on to the next one while it moves
        self.status.config(text=f"Moving {os.path.basename(path)}...")
        where = os.path.basename(os.path.normpath(folder)) or folder

        def work():
            try:
                files.move_file(path, dst)
                error = None
            except OSError as e:
                error = e
            ui_thread.call(lambda: self._moved(path, dst, where, error))

        threading.Thread(target=work, daemon=True).start()

    def _moved(self, src, dst, where, error):
        self._folder_changed()
        if error:
            messagebox.showerror("Couldn't move", f"{os.path.basename(src)} wasn't moved:"
                                 f"\n{error}", parent=self if self.winfo_exists() else None)
            return
        self.app.last_sort = (src, dst)  # Ctrl+Z in the main window puts it back
        text = f"Moved {os.path.basename(src)} to {where}."
        self.app.status.set(text + "  (Ctrl+Z puts it back)")
        if self.winfo_exists():
            self.status.config(text=text)

    def copy_to(self, folder):
        path = self.path
        dst = files.free_name(folder, os.path.basename(path))
        self.status.config(text=f"Copying {os.path.basename(path)}...")

        def work():
            try:
                shutil.copy2(path, dst)
                error = None
            except OSError as e:
                error = e
            ui_thread.call(lambda: self._copied(path, dst, error))

        threading.Thread(target=work, daemon=True).start()

    def _copied(self, src, dst, error):
        if not self.winfo_exists():
            return
        if error:
            messagebox.showerror("Couldn't copy", f"{os.path.basename(src)} wasn't copied:"
                                 f"\n{error}", parent=self)
            return
        self.status.config(text=f"Copied to {os.path.dirname(dst)}.")
        self._folder_changed()

    # ------------------------------------------------------------ rename
    def start_rename(self):
        if not self._can_change() or self.renaming is not None:
            return
        stem = os.path.splitext(os.path.basename(self.path))[0]
        entry = self.renaming = ttk.Entry(self.rename_holder)
        entry.insert(0, stem)
        entry.select_range(0, "end")
        entry.pack(fill="x", pady=(4, 0))
        entry.focus_set()
        entry.bind("<Return>", lambda e: self._finish_rename() or "break")
        entry.bind("<Escape>", lambda e: self._cancel_rename() or "break")

    def _cancel_rename(self):
        entry, self.renaming = self.renaming, None
        if entry is not None:
            entry.destroy()
            self.focus_set()

    def _finish_rename(self):
        entry = self.renaming
        if entry is None:
            return
        name = entry.get().strip().rstrip(".")
        self._cancel_rename()
        if not self._can_change():
            return
        path = self.path
        folder = os.path.dirname(os.path.abspath(path))
        old = os.path.basename(path)
        stem, ext = os.path.splitext(old)
        if not name or name == stem:
            return
        if any(c in files.INVALID_NAME_CHARS or ord(c) < 32 for c in name):
            messagebox.showerror("Can't rename", "A file name can't contain any of these: "
                                 + " ".join(files.INVALID_NAME_CHARS), parent=self)
            return
        new = name + ext
        if os.path.exists(os.path.join(folder, new)) and \
                os.path.normcase(new) != os.path.normcase(old):
            messagebox.showerror("Can't rename", f"“{new}” already exists in this "
                                 "folder.", parent=self)
            return
        reopen = self._let_go(path)
        self._rename_when_free(folder, old, new, reopen, tries=15)

    def _rename_when_free(self, folder, old, new, reopen, tries):
        try:
            files.rename_all(folder, [(old, new)])
        except OSError as e:
            if tries > 0 and not isinstance(e, FileExistsError):
                self.after(150, lambda: self._rename_when_free(folder, old, new, reopen,
                                                               tries - 1))
                return
            reopen({})
            messagebox.showerror("Couldn't rename", str(e), parent=self)
            self._load(os.path.join(folder, old))
            return
        old_path, new_path = os.path.join(folder, old), os.path.join(folder, new)
        reopen({os.path.normcase(old_path): new_path})
        self._folder_changed()
        self.app.status.set(f"Renamed to {new}")
        if self.path and os.path.normcase(os.path.abspath(self.path)) == \
                os.path.normcase(old_path):
            self._gone(new=new_path)
        self.status.config(text=f"Renamed to {new}")

    # ------------------------------------------------------------- close
    def close(self):
        self._cancel_rename()
        self.player.stop()
        self.token += 1
        self.path = None
        self.photo = None
        self.canvas.itemconfig(self.canvas_img, image="")
        self.withdraw()  # kept for next time: opens instantly
