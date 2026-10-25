"""A tool for renaming every file in a folder with a numbered pattern, in a chosen order
(including grouped by colour, so similar-looking videos end up next to each other)."""

import os
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, ttk

from . import ffmpeg_tools as ff
from . import files
from . import theme

ORDERS = ("Name", "Date modified", "Size", "Colour")
DIGITS = {"None (1, 2 ... 10)": None, "Auto (01 ... 10)": "auto", "2 (01)": 2, "3 (001)": 3,
          "4 (0001)": 4}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_colors = {}  # file identity -> average colour, kept while the app runs


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
        self.title("Rename files")
        self.geometry("860x640")
        self.minsize(640, 420)
        theme.style_window(self)

        self.all_files = tk.BooleanVar(value=False)
        self.order = tk.StringVar(value="Name")
        self.reverse = tk.BooleanVar(value=False)
        self.prefix = tk.StringVar(value="")
        self.separator = tk.StringVar(value="-")
        self.start = tk.StringVar(value="1")
        self.digits = tk.StringVar(value=list(DIGITS)[0])
        self.status = tk.StringVar()
        self._build()
        for var in (self.reverse, self.prefix, self.separator, self.start, self.digits):
            var.trace_add("write", lambda *_: self.refresh())
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
        ttk.Label(order, style="Muted.TLabel", wraplength=230, justify="left",
                  text="Colour groups videos by the average colour of their pictures: "
                       "reds, oranges, yellows, greens, blues, purples, then greys."
                  ).grid(row=1, column=0, columnspan=2, sticky="w", **pad)

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
        self.tree = ttk.Treeview(table, columns=("old", "new"), selectmode="none")
        self.tree.heading("#0", text="#")
        self.tree.heading("old", text="Current name")
        self.tree.heading("new", text="New name")
        self.tree.column("#0", width=70, stretch=False)
        self.tree.column("old", width=340)
        self.tree.column("new", width=340)
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
        """Identifies a file's contents, but not its name, so colours survive renaming."""
        try:
            stat = os.stat(os.path.join(self.folder, name))
        except OSError:
            return None
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _read_colors(self):
        """Work out the average colour of each file in the background."""
        todo = [n for n in self.names if self._color_key(n) not in _colors]
        if not todo:
            self.refresh()
            return
        self.color_token += 1
        token, folder, ffmpeg = self.color_token, self.folder, self.app.ffmpeg
        self.reading_colors = True
        self.rename_btn.config(state="disabled")
        done = [0]

        def one(name):
            key = self._color_key(name)
            rgb = ff.average_color(ffmpeg, os.path.join(folder, name))
            if key:
                _colors[key] = rgb
            done[0] += 1
            self._from_thread(lambda: token == self.color_token and self._color_progress(done[0], len(todo)))

        def work():
            with ThreadPoolExecutor(max_workers=max(2, min(6, (os.cpu_count() or 4) - 1))) as pool:
                for name in todo:
                    if token != self.color_token:
                        break
                    pool.submit(one, name)
            self._from_thread(lambda: token == self.color_token and self._colors_done())

        threading.Thread(target=work, daemon=True).start()

    def _from_thread(self, fn):
        try:
            self.after(0, fn)
        except (RuntimeError, tk.TclError):
            pass

    def _color_progress(self, done, total):
        self.progress.config(value=done / total)
        self.status.set(f"Reading colours... {done} of {total}")

    def _colors_done(self):
        self.reading_colors = False
        self.progress.config(value=0)
        self.refresh()

    def _swatch(self, rgb):
        color = "#%02x%02x%02x" % rgb if rgb else None
        if color not in self.swatches:
            image = tk.PhotoImage(master=self, width=22, height=12)
            if color:
                image.put(color, to=(0, 0, 22, 12))
            self.swatches[color] = image
        return self.swatches[color]

    # ----------------------------------------------------------- preview
    def _ordered(self):
        order, folder = self.order.get(), self.folder
        names = list(self.names)
        if order == "Date modified":
            names.sort(key=lambda n: os.path.getmtime(os.path.join(folder, n)))
        elif order == "Size":
            names.sort(key=lambda n: os.path.getsize(os.path.join(folder, n)))
        elif order == "Colour":
            names.sort(key=lambda n: ff.color_sort_key(_colors.get(self._color_key(n))))
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
            image = self._swatch(_colors.get(self._color_key(old))) if show_color else ""
            self.tree.insert("", "end", text=f" {i}", image=image, values=(old, new))
        changes = sum(1 for old, new in plan if new and old != new)
        if not self.reading_colors:
            what = "file" if len(plan) == 1 else "files"
            self.status.set(f"{len(plan)} {what}; {changes} will be renamed." if plan
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
