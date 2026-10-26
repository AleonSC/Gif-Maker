"""A tool for renaming every file in a folder with a numbered pattern, in a chosen order
(including grouped by colour, so similar-looking videos end up next to each other)."""

import os
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, ttk

from . import files
from . import similarity as sim
from . import theme
from . import ui_thread

ORDERS = ("Name", "Date modified", "Size", "Colour")
DIGITS = {"None (1, 2 ... 10)": None, "Auto (01 ... 10)": "auto", "2 (01)": 2, "3 (001)": 3,
          "4 (0001)": 4}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_sigs = None  # file identity -> picture fingerprint; loaded from disk when first needed


def _fingerprints():
    global _sigs
    if _sigs is None:
        _sigs = sim.load_cache()
    return _sigs


class RenameTool(tk.Toplevel):
    def __init__(self, app, video_extensions, folder=None):
        super().__init__(app.root)
        self.app = app
        self.media = set(video_extensions) | IMAGE_EXTENSIONS
        self.folder = None
        self.names = []
        self.swatches = {}
        self.color_token = 0
        self.reading_colors = False
        self.labels = {}  # name -> look-alike group number
        self.order_cache = None  # (what it depends on, ordered names, labels)
        self.refresh_job = None
        self.title("Rename files")
        self.geometry("860x640")
        self.minsize(640, 420)
        theme.style_window(self)

        self.all_files = tk.BooleanVar(value=False)
        self.order = tk.StringVar(value="Name")
        self.reverse = tk.BooleanVar(value=False)
        self.together = tk.BooleanVar(value=True)
        self.prefix = tk.StringVar(value="")
        self.separator = tk.StringVar(value="-")
        self.start = tk.StringVar(value="1")
        self.digits = tk.StringVar(value=list(DIGITS)[0])
        self.status = tk.StringVar()
        self._build()
        for var in (self.reverse, self.together, self.digits):
            var.trace_add("write", lambda *_: self.refresh())
        for var in (self.prefix, self.separator, self.start):  # typed: wait for a pause
            var.trace_add("write", lambda *_: self._refresh_soon())
        self.protocol("WM_DELETE_WINDOW", self.close)
        if folder:
            self.load(folder)
        self._update_undo()

    # ---------------------------------------------------------------- UI
    def _build(self):
        pad = {"padx": 6, "pady": 3}
        main = ttk.Frame(self, padding=10)
        main.pack(fill="both", expand=True)

        top = ttk.Frame(main)
        top.pack(fill="x")
        ttk.Button(top, text="Choose folder...", command=self.choose_folder).pack(side="left")
        self.folder_label = ttk.Label(top, text="No folder chosen", style="Muted.TLabel")
        self.folder_label.pack(side="left", padx=10)
        include = ttk.Frame(main)
        include.pack(fill="x", pady=(6, 0))
        ttk.Label(include, text="Rename:").pack(side="left")
        ttk.Radiobutton(include, text="Videos and images", value=False, variable=self.all_files,
                        command=self.reload).pack(side="left", padx=(8, 0))
        ttk.Radiobutton(include, text="All files", value=True, variable=self.all_files,
                        command=self.reload).pack(side="left", padx=(8, 0))
        ttk.Label(include, style="Muted.TLabel",
                  text="(only files directly in the folder; hidden and system files are left alone)"
                  ).pack(side="left", padx=(10, 0))

        options = ttk.Frame(main)
        options.pack(fill="x", pady=(8, 0))
        order = ttk.LabelFrame(options, text="Order", padding=6)
        order.pack(side="left", fill="y")
        box = ttk.Combobox(order, textvariable=self.order, values=ORDERS, width=14, state="readonly")
        box.grid(row=0, column=0, **pad)
        box.bind("<<ComboboxSelected>>", lambda e: self._order_changed())
        ttk.Checkbutton(order, text="Reverse", variable=self.reverse).grid(row=0, column=1, **pad)
        ttk.Checkbutton(order, text="Keep look-alikes together", variable=self.together).grid(
            row=1, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(order, style="Muted.TLabel", wraplength=250, justify="left",
                  text="Colour: by each picture's main colour (the swatch's left block): "
                       "reds, oranges, yellows, greens, blues, purples, then greys. "
                       "Look-alikes (the same video or photo re-encoded, resized or renamed) "
                       "sit together, marked A, B, C..."
                  ).grid(row=2, column=0, columnspan=2, sticky="w", **pad)

        naming = ttk.LabelFrame(options, text="New names", padding=6)
        naming.pack(side="left", fill="both", expand=True, padx=(10, 0))
        ttk.Label(naming, text="Prefix").grid(row=0, column=0, sticky="e", **pad)
        ttk.Entry(naming, textvariable=self.prefix, width=18).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(naming, text="Separator").grid(row=0, column=2, sticky="e", **pad)
        ttk.Entry(naming, textvariable=self.separator, width=4).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(naming, text="Start at").grid(row=1, column=0, sticky="e", **pad)
        ttk.Spinbox(naming, from_=0, to=999999, textvariable=self.start, width=8).grid(
            row=1, column=1, sticky="w", **pad)
        ttk.Label(naming, text="Zero-pad").grid(row=1, column=2, sticky="e", **pad)
        ttk.Combobox(naming, textvariable=self.digits, values=list(DIGITS), width=16,
                     state="readonly").grid(row=1, column=3, sticky="w", **pad)
        self.example = ttk.Label(naming, style="Muted.TLabel")
        self.example.grid(row=2, column=0, columnspan=4, sticky="w", **pad)

        table = ttk.Frame(main)
        table.pack(fill="both", expand=True, pady=(10, 0))
        self.tree = ttk.Treeview(table, columns=("alike", "old", "new"), selectmode="none")
        self.tree.heading("#0", text="#")
        self.tree.heading("alike", text="Look-alike")
        self.tree.heading("old", text="Current name")
        self.tree.heading("new", text="New name")
        self.tree.column("#0", width=86, stretch=False)
        self.tree.column("alike", width=95, stretch=False, anchor="center")
        self.tree.column("old", width=320)
        self.tree.column("new", width=320)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        bottom = ttk.Frame(main)
        bottom.pack(fill="x", pady=(10, 0))
        self.rename_btn = ttk.Button(bottom, text="Rename", command=self.apply, state="disabled")
        self.rename_btn.pack(side="right")
        self.undo_btn = ttk.Button(bottom, text="Undo last rename", command=self.undo)
        self.undo_btn.pack(side="right", padx=6)
        self.progress = ttk.Progressbar(bottom, maximum=1.0, length=160)
        self.progress.pack(side="right", padx=6)
        ttk.Label(bottom, textvariable=self.status).pack(side="left")

    # ------------------------------------------------------------ folder
    def choose_folder(self):
        folder = filedialog.askdirectory(parent=self, title="Choose a folder to rename",
                                         initialdir=self.folder)
        if folder:
            self.load(folder)

    def load(self, folder):
        self._set_folder(os.path.abspath(folder))
        self.reload()

    def _set_folder(self, folder):
        self.folder = folder
        shown = folder if len(folder) <= 70 else "\u2026" + folder[-69:]  # keep the end visible
        self.folder_label.config(text=shown, style="TLabel")

    def reload(self):
        if not self.folder:
            return
        try:
            self.names = files.list_files(self.folder, None if self.all_files.get() else self.media)
        except OSError as e:
            messagebox.showerror("Couldn't read the folder", str(e), parent=self)
            self.names = []
        self.refresh()
        if self.order.get() == "Colour":
            self._read_colors()

    def _order_changed(self):
        if self.order.get() == "Colour":
            self._read_colors()
        self.refresh()

    # ------------------------------------------------------------ colour
    def _color_key(self, name):
        """Identifies a file's contents but not its name, so fingerprints survive renaming."""
        try:
            stat = os.stat(os.path.join(self.folder, name))
        except OSError:
            return None
        # Some drives (FAT, network) have no file IDs: fall back to the path then.
        file_id = stat.st_ino or os.path.normcase(os.path.join(self.folder, name))
        return f"{stat.st_dev}:{file_id}:{stat.st_size}:{stat.st_mtime_ns}"

    def _sig(self, name):
        return _fingerprints().get(self._color_key(name))

    def _read_colors(self):
        """Fingerprint each file's pictures in the background (saved for next time)."""
        cache = _fingerprints()
        todo = [n for n in self.names if self._color_key(n) not in cache]
        if not todo:
            self.refresh()
            return
        self.color_token += 1
        token, folder, ffmpeg = self.color_token, self.folder, self.app.ffmpeg
        self.reading_colors = True
        self.rename_btn.config(state="disabled")
        done = [0]

        def one(name):
            if token != self.color_token:
                return  # the window closed (or the folder changed): skip the rest
            key = self._color_key(name)
            sig = sim.fingerprint(ffmpeg, os.path.join(folder, name))
            if key:
                cache[key] = sig
            done[0] += 1
            self._from_thread(lambda: token == self.color_token and self._color_progress(done[0], len(todo)))

        def work():
            with ThreadPoolExecutor(max_workers=max(2, min(6, (os.cpu_count() or 4) - 1))) as pool:
                for name in todo:
                    if token != self.color_token:
                        break
                    pool.submit(one, name)
            sim.save_cache(cache)
            self._from_thread(lambda: token == self.color_token and self._colors_done())

        threading.Thread(target=work, daemon=True).start()

    def _from_thread(self, fn):
        try:
            ui_thread.call(fn)
        except (RuntimeError, tk.TclError):
            pass

    def _color_progress(self, done, total):
        self.progress.config(value=done / total)
        self.status.set(f"Reading pictures... {done} of {total}")

    def _colors_done(self):
        self.reading_colors = False
        self.progress.config(value=0)
        self.refresh()

    def _swatch(self, sig):
        """The main colour on the left, then the picture's palette in proportion."""
        if not sig:
            key = None
        else:
            key = (sim.swatch_rgb(sig),
                   tuple((round(share, 2), sim.to_rgb(color)) for share, color in sig["palette"]))
        if key not in self.swatches:
            image = tk.PhotoImage(master=self, width=34, height=12)
            if key:
                hex_color = lambda rgb: "#%02x%02x%02x" % rgb
                image.put(hex_color(key[0]), to=(0, 0, 10, 12))
                x, total = 12, sum(share for share, _ in key[1]) or 1
                for i, (share, rgb) in enumerate(key[1]):
                    x2 = 34 if i == len(key[1]) - 1 else min(34, x + max(1, round(22 * share / total)))
                    if x2 > x:
                        image.put(hex_color(rgb), to=(x, 0, x2, 12))
                    x = x2
            self.swatches[key] = image
        return self.swatches[key]

    # ----------------------------------------------------------- preview
    def _refresh_soon(self):
        if self.refresh_job:
            self.after_cancel(self.refresh_job)
        self.refresh_job = self.after(200, self.refresh)

    def _ordered(self):
        """The files in the chosen order (remembered: working out a colour order with
        look-alikes takes a while for thousands of files, and the names don't change it)."""
        order, together, reverse = self.order.get(), self.together.get(), self.reverse.get()
        depends = (self.folder, tuple(self.names), order, together, reverse,
                   len(_fingerprints()) if order == "Colour" else 0)
        if self.order_cache and self.order_cache[0] == depends:
            self.labels = self.order_cache[2]
            return list(self.order_cache[1])
        names = self._compute_order()
        self.order_cache = (depends, list(names), self.labels)
        return names

    def _compute_order(self):
        order, folder = self.order.get(), self.folder
        names = list(self.names)
        if order == "Date modified":
            names.sort(key=lambda n: os.path.getmtime(os.path.join(folder, n)))
        elif order == "Size":
            names.sort(key=lambda n: os.path.getsize(os.path.join(folder, n)))
        self.labels = {}
        if order == "Colour":
            sigs = {n: self._sig(n) for n in names}
            names, self.labels = sim.order(names, sigs, self.together.get())
        if self.reverse.get():
            names.reverse()
        return names

    def _settings(self):
        try:
            start = int(self.start.get())
        except ValueError:
            start = 1
        return self.prefix.get(), self.separator.get(), max(0, start), DIGITS.get(self.digits.get())

    def plan(self):
        names = self._ordered()
        return list(zip(names, files.numbered_names(names, *self._settings())))

    def refresh(self):
        if self.refresh_job:
            self.after_cancel(self.refresh_job)
            self.refresh_job = None
        prefix, separator, start, digits = self._settings()
        example = files.numbered_names(["clip.mp4"] * 2, prefix, separator, start, digits)
        self.example.config(text=f"For example: {example[0]}, {example[1]} ...")
        self.tree.delete(*self.tree.get_children())
        if not self.folder:
            self.status.set("Choose a folder to start.")
            return
        plan = self.plan() if not (self.order.get() == "Colour" and self.reading_colors) else \
            [(n, "") for n in self.names]
        show_color = self.order.get() == "Colour" and not self.reading_colors
        for i, (old, new) in enumerate(plan, 1):
            image = self._swatch(self._sig(old)) if show_color else ""
            alike = sim.group_label(self.labels[old]) if old in self.labels else ""
            self.tree.insert("", "end", text=f" {i}", image=image, values=(alike, old, new))
        changes = sum(1 for old, new in plan if new and old != new)
        if not self.reading_colors:
            what = "file" if len(plan) == 1 else "files"
            groups = len(set(self.labels.values()))
            alike = (f" {groups} group{'s' if groups > 1 else ''} of look-alikes."
                     if groups else "")
            self.status.set(f"{len(plan)} {what}; {changes} will be renamed.{alike}" if plan
                            else "No files to rename here.")
        self.rename_btn.config(state="normal" if changes and not self.reading_colors else "disabled")

    # ------------------------------------------------------------- apply
    def apply(self):
        plan = [(old, new) for old, new in self.plan() if old != new]
        if not plan:
            return
        if not messagebox.askyesno(
                "Rename files",
                f"Rename {len(plan)} files in {self.folder}?\n\nYou can put them back with "
                "Undo last rename.", parent=self):
            return
        self._run(plan, "Renamed", save_undo=True)

    def undo(self):
        last = files.load_undo()
        if not last:
            return
        folder, renamed = last
        if not messagebox.askyesno(
                "Undo last rename",
                f"Put the {len(renamed)} files renamed in {folder} back to their old names?",
                parent=self):
            return
        self._set_folder(folder)
        self._run([(new, old) for old, new in renamed], "Put back", save_undo=False)

    def _run(self, plan, done_word, save_undo):
        reopen = self.app.release_folder(self.folder)  # nothing may be reading these files
        self._rename_when_free(plan, done_word, save_undo, reopen, tries=15)

    def _rename_when_free(self, plan, done_word, save_undo, reopen, tries):
        # Video players take a moment to let go of their files, and Windows won't
        # rename a file that's still open, so retry for a couple of seconds.
        try:
            renamed = files.rename_all(self.folder, plan)
        except FileExistsError as e:
            messagebox.showerror("Can't rename", str(e), parent=self)
            return
        except OSError as e:
            if tries > 0:
                self.after(150, lambda: self._rename_when_free(plan, done_word, save_undo, reopen,
                                                               tries - 1))
                return
            messagebox.showerror("Couldn't rename", str(e), parent=self)
            return
        if save_undo:
            files.save_undo(self.folder, renamed)
        else:
            files.forget_undo()
        reopen({os.path.normcase(os.path.join(self.folder, old)): os.path.join(self.folder, new)
                for old, new in renamed})
        self._update_undo()
        self.reload()
        self.status.set(f"{done_word} {len(renamed)} files.")

    def _update_undo(self):
        self.undo_btn.config(state="normal" if files.load_undo() else "disabled")

    def close(self):
        self.color_token += 1  # stops reading colours
        self.app.rename_window = None
        self.destroy()
