"""ptkit desktop GUI (Tkinter, standard library only).

Workflow: load a .pkt/.pka -> read instructions/metadata -> pick a target
(one device, or Batch / Multi-Device) -> Apply & Compile. Each apply runs on
a clone of the working document; only if editing, the consistency check,
encoding and the read-back verification all succeed does the clone become
the new working copy. Successive applies therefore accumulate, and a failed
apply never leaves partial edits behind. "Load" discards the working copy and
re-reads the input file.

Long-running steps (decode/encode) run on a worker thread; results and log
lines are marshalled back to the Tk thread through a queue.
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__, ops, xmlbundle, reference, solver
from .codec import CodecError
from .htmltext import html_to_text, strip_html_comments  # noqa: F401 (re-exported)
from .model import Device, PTFile
from .xmlio import XmlFidelityError

BATCH_LABEL = "⧉  Batch / Multi-Device"
FILETYPES = [("Packet Tracer files", "*.pkt *.pka"), ("Network (.pkt)", "*.pkt"),
             ("Activity (.pka)", "*.pka"), ("All files", "*.*")]

# Errors reported as one log line; anything else also gets a traceback.
EXPECTED_ERRORS = (ValueError, KeyError, OSError, CodecError, XmlFidelityError)

LOG_COLORS = {"info": None, "ok": "#1a7f37", "warn": "#9a6700", "error": "#cf222e",
              "step": "#0969da"}


# --- helpers -------------------------------------------------------------------

def documents_dir() -> Path:
    """The user's Documents folder (honours OneDrive/folder redirection)."""
    if sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:  # CSIDL_PERSONAL
                return Path(buf.value)
        except Exception:  # noqa: BLE001
            pass
    docs = Path.home() / "Documents"
    return docs if docs.is_dir() else Path.home()


def fallback_output_dir() -> Path:
    return documents_dir() / "ptkit"


def is_writable_dir(d: Path) -> bool:
    """True if a file can actually be created in ``d``.

    One exclusive-create attempt, deliberately not ``tempfile``: on Windows
    tempfile treats PermissionError as a name collision and retries up to
    TMP_MAX (~2**31) times whenever os.access() claims the folder is
    writable, which it does for Program Files, so it never returns there.
    """
    probe = Path(d) / f".ptkit-write-test-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    os.close(fd)
    try:
        os.unlink(probe)
    except OSError:
        pass
    return True


def default_output_path(src: Path) -> Path:
    """``<name>-ptkit.<ext>`` beside the input, or in Documents\\ptkit when the
    input's folder is read-only (e.g. the samples under Program Files)."""
    name = f"{src.stem}-ptkit{src.suffix}"
    if is_writable_dir(src.parent):
        return src.with_name(name)
    return fallback_output_dir() / name


def log_file_path() -> Path:
    if os.environ.get("PTKIT_LOG_FILE"):
        return Path(os.environ["PTKIT_LOG_FILE"])
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return base / "ptkit" / "ptkit-gui.log"


class _FileLog:
    """Best-effort mirror of the execution log on disk, so problems in the
    windowed app (no console) can still be diagnosed afterwards."""

    def __init__(self, path: Path):
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 1_000_000:
                path.replace(path.with_suffix(".old.log"))
            self._fh = open(path, "a", encoding="utf-8")
            self.write("info", f"--- session start (ptkit {__version__}, "
                               f"{'frozen exe' if getattr(sys, 'frozen', False) else sys.executable})")
        except OSError:
            self._fh = None

    def write(self, level: str, msg: str) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}\n")
            self._fh.flush()
        except (OSError, ValueError):
            pass


# --- application -----------------------------------------------------------------

class PtkitApp:
    def __init__(self, root: tk.Tk, *, synchronous: bool = False):
        self.root = root
        # synchronous=True runs tasks inline (used by tests / headless checks)
        self.synchronous = synchronous
        self.working: PTFile | None = None
        self.loaded_path: Path | None = None
        self.applied_sets = 0
        self._busy = False
        self._auto_output: str | None = None
        self._queue: queue.Queue = queue.Queue()
        self._device_cache: dict[str, Device] = {}
        self.reference_states: dict = {}
        self._file_log = _FileLog(log_file_path())
        # Without a console, uncaught errors in Tk callbacks would vanish:
        # surface them in the execution log (and the log file) instead.
        root.report_callback_exception = self._on_tk_error

        root.title(f"ptkit — Packet Tracer Config Workbench  v{__version__}")
        self._size_window()
        self._init_style()
        self._build()
        self._set_target_mode(None)
        self.log("ptkit GUI ready. Choose a .pkt/.pka file and press Load.", "step")
        if not synchronous:
            root.after(50, self._drain_queue)

    # -- layout ---------------------------------------------------------------------
    def _size_window(self) -> None:
        """Size relative to the display's DPI (Tk 'scaling' is pixels per
        point; 96 DPI = 1.333) and never larger than 92% of the screen."""
        scale = max(1.0, float(self.root.tk.call("tk", "scaling")) / (96 / 72))
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = min(int(1320 * scale), int(sw * 0.92))
        h = min(int(860 * scale), int(sh * 0.88))
        self.root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        self.root.minsize(min(int(960 * scale), w), min(int(640 * scale), h))

    def _init_style(self) -> None:
        style = ttk.Style(self.root)
        for theme in ("vista", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 6))
        style.configure("Header.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Muted.TLabel", foreground="#57606a")
        self.mono = ("Consolas", 10) if sys.platform == "win32" else ("TkFixedFont", 10)

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        self._build_files(outer).grid(row=0, column=0, sticky="ew")

        vpane = ttk.PanedWindow(outer, orient="vertical")
        vpane.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        hpane = ttk.PanedWindow(vpane, orient="horizontal")
        vpane.add(hpane, weight=4)
        hpane.add(self._build_info(hpane), weight=2)
        hpane.add(self._build_editor(hpane), weight=3)
        vpane.add(self._build_log(vpane), weight=1)

        self.status = tk.StringVar(value="No file loaded")
        bar = ttk.Frame(outer)
        bar.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(bar, textvariable=self.status, style="Muted.TLabel").pack(side="left")
        ttk.Button(bar, text="License & use", command=self.show_license).pack(side="right", padx=6)
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=160)

    def show_license(self):
        window = tk.Toplevel(self.root)
        window.title("PTKit — MIT License & responsible use")
        window.transient(self.root)
        text = self._readonly_text(window, wrap="word", font=("Segoe UI", 10), width=85, height=25)
        text.master_frame.pack(fill="both", expand=True, padx=10, pady=10)
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
        sections = []
        for name in ("RESPONSIBLE_USE.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
            try:
                sections.append((base / name).read_text(encoding="utf-8"))
            except OSError:
                continue
        self._set_text(text, "\n\n".join(sections) or
            "PTKit is published by Sponge and contributors under the MIT License. "
            "Use at your own risk. You are responsible for authorization, applicable "
            "rules and independent validation of outputs. See the LICENSE and "
            "RESPONSIBLE_USE.md files supplied with the distribution.")
        ttk.Button(window, text="Close", command=window.destroy).pack(pady=(0, 10))
        return text

    def _build_files(self, parent) -> ttk.LabelFrame:
        box = ttk.LabelFrame(parent, text=" 1 · Files ", padding=8)
        box.columnconfigure(1, weight=1)
        self.in_var = tk.StringVar()
        self.out_var = tk.StringVar()
        ttk.Label(box, text="Input .pkt/.pka").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(box, textvariable=self.in_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(box, text="Browse…", command=self._browse_input).grid(row=0, column=2, padx=4)
        self.load_btn = ttk.Button(box, text="Load", command=self.load_clicked)
        self.load_btn.grid(row=0, column=3)
        ttk.Label(box, text="Output file").grid(row=1, column=0, sticky="w", padx=(0, 8),
                                                pady=(6, 0))
        ttk.Entry(box, textvariable=self.out_var).grid(row=1, column=1, sticky="ew", pady=(6, 0))
        ttk.Button(box, text="Browse…", command=self._browse_output).grid(
            row=1, column=2, padx=4, pady=(6, 0))

        ttk.Label(box, text="XML data").grid(row=2, column=0, sticky="w", padx=(0, 8),
                                             pady=(6, 0))
        xml_row = ttk.Frame(box)
        xml_row.grid(row=2, column=1, columnspan=3, sticky="ew", pady=(6, 0))
        self.export_btn = ttk.Button(xml_row, text="Export XML Data…",
                                     command=self.export_xml_clicked)
        self.export_btn.pack(side="left")
        self.import_btn = ttk.Menubutton(xml_row, text="Import XML & Re-encode ▾")
        menu = tk.Menu(self.import_btn, tearoff=False)
        menu.add_command(label="From export folder…",
                         command=lambda: self.import_xml_clicked(kind="folder"))
        menu.add_command(label="From single XML file…",
                         command=lambda: self.import_xml_clicked(kind="file"))
        self.import_btn["menu"] = menu
        self.import_btn.pack(side="left", padx=6)
        self.solve_btn = ttk.Button(xml_row, text="Solve PKA…", state="disabled",
                                    command=self.solve_clicked)
        self.solve_btn.pack(side="left", padx=(0, 6))
        ttk.Label(xml_row, style="Muted.TLabel",
                  text="Unpack the loaded file to editable XML, or validate edited XML and "
                       "compile it back into a .pkt/.pka").pack(side="left", padx=6)
        return box

    def _build_info(self, parent) -> ttk.LabelFrame:
        box = ttk.LabelFrame(parent, text=" 2 · Instructions & metadata ", padding=6)
        nb = ttk.Notebook(box)
        nb.pack(fill="both", expand=True)
        self.info_nb = nb

        self.instr_text = self._readonly_text(nb, wrap="word", font=("Segoe UI", 10),
                                              width=36, height=8)
        nb.add(self.instr_text.master_frame, text="Instructions")

        meta = ttk.Frame(nb)
        self.meta_tree = ttk.Treeview(meta, columns=("value",), show="tree headings", height=12)
        self.meta_tree.heading("#0", text="Property")
        self.meta_tree.heading("value", text="Value")
        self.meta_tree.column("#0", width=150, stretch=False)
        self.meta_tree.column("value", width=200)
        self._with_scroll(meta, self.meta_tree)
        nb.add(meta, text="Metadata")

        assess = ttk.Frame(nb)
        self.check_note = ttk.Label(assess, style="Muted.TLabel", justify="left", text="")
        self.check_note.pack(side="top", anchor="w", pady=(0, 4))
        self.check_note.bind("<Configure>",
                             lambda e: self.check_note.configure(wraplength=e.width - 4))
        tree_frame = ttk.Frame(assess)
        tree_frame.pack(fill="both", expand=True)
        self.assess_tree = ttk.Treeview(tree_frame, columns=("points", "components"),
                                        show="tree headings")
        self.assess_tree.heading("#0", text="Graded item (what is checked)")
        self.assess_tree.heading("points", text="Points")
        self.assess_tree.heading("components", text="Component")
        self.assess_tree.column("#0", width=260)
        self.assess_tree.column("points", width=55, anchor="center", stretch=False)
        self.assess_tree.column("components", width=90, stretch=False)
        self._with_scroll(tree_frame, self.assess_tree)
        nb.add(assess, text="Grading checklist")
        self._build_reference_tab(nb)
        for tree in (self.meta_tree, self.assess_tree):
            tree.bind("<Control-c>", lambda e, t=tree: self._copy_tree(t, selected_only=True))

        bar = ttk.Frame(box)
        bar.pack(fill="x", pady=(6, 0))
        ttk.Button(bar, text="Copy", command=self.copy_current_tab).pack(side="right")
        ttk.Label(bar, style="Muted.TLabel",
                  text="Select text and press Ctrl+C, or copy the whole tab:").pack(side="right",
                                                                                     padx=6)
        return box

    def _build_reference_tab(self, nb) -> None:
        panel = ttk.Frame(nb)
        nb.add(panel, text="Target & Reference Values")
        ttk.Label(panel, text="Reference Topology Parameters", style="Header.TLabel").pack(
            anchor="w", pady=(4, 6))
        self.reference_var = tk.StringVar()
        self.reference_combo = ttk.Combobox(panel, textvariable=self.reference_var,
                                            state="disabled")
        self.reference_combo.pack(fill="x")
        self.reference_combo.bind("<<ComboboxSelected>>", lambda e: self._show_reference())
        self.reference_note = ttk.Label(panel, text="Load a file to inspect reference values.",
                                         justify="left", style="Muted.TLabel")
        self.reference_note.pack(fill="x", pady=6)
        self.reference_note.bind("<Configure>",
            lambda e: self.reference_note.configure(wraplength=max(1, e.width - 4)))
        frame = ttk.Frame(panel)
        frame.pack(fill="both", expand=True)
        self.reference_tree = ttk.Treeview(frame, columns=("value",), show="tree headings", height=6)
        self.reference_tree.heading("#0", text="Device / parameter")
        self.reference_tree.heading("value", text="Reference value")
        self.reference_tree.column("#0", width=170)
        self.reference_tree.column("value", width=180)
        self._with_scroll(frame, self.reference_tree)
        self.reference_tree.bind("<Control-c>",
                                lambda e: self._copy_tree(self.reference_tree, selected_only=True))
        self.reference_copy_btn = ttk.Button(panel,
            text="Copy Reference Parameters to Batch JSON", state="disabled",
            command=self.copy_reference_batch)
        # Reserve the action row before the expanding tree, including on small screens.
        self.reference_copy_btn.pack(side="bottom", fill="x", pady=(6, 2), before=frame)

    def _show_reference(self) -> None:
        tree = self.reference_tree
        tree.delete(*tree.get_children())
        role = self.reference_var.get()
        data = self.reference_states.get(role, {})

        def add(parent, values):
            entries = values.items() if isinstance(values, dict) else enumerate(values)
            for key, value in entries:
                nested = isinstance(value, (dict, list))
                node = tree.insert(parent, "end", text=str(key), open=not parent,
                    values=("" if nested else ("(empty)" if value is None else str(value)),))
                if nested:
                    add(node, value)
        add("", data)
        explicit = any(word in role.lower() for word in ("answer", "reference", "target"))
        kind = "Explicit reference topology" if explicit else "Baseline topology captured when opened"
        self.reference_note.configure(text=(f"{kind}: {role} · {len(data)} device(s). "
            "Copy targets the selected editor network; review the batch before applying."
            if data else "No device parameters were found in this topology."))
        self.reference_copy_btn.configure(state="normal" if data else "disabled")

    def copy_reference_batch(self) -> None:
        if self._busy or self.working is None:
            return
        role = self.reference_var.get()
        try:
            target = self._network()
            if target is None:
                raise ValueError("Select a target network first.")
            plan, notes = reference.batch_plan(self.reference_states.get(role, {}), target, role)
        except (ValueError, KeyError) as e:
            self.log(f"Cannot copy reference parameters: {e}", "warn")
            return
        text = json.dumps(plan, indent=2, ensure_ascii=False)
        self._to_clipboard(text, "reference batch JSON")
        for note in notes:
            self.log(note, "warn")
        self.log(f"Copied {len(plan['operations'])} reference operation(s) for {target.role}. "
                 "Paste into Batch mode to review and apply. Other XML fields are retained "
                 "as reference_parameters metadata.")

    def _build_editor(self, parent) -> ttk.LabelFrame:
        box = ttk.LabelFrame(parent, text=" 3 · Configuration target ", padding=8)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(1, weight=1)

        sel = ttk.Frame(box)
        sel.grid(row=0, column=0, sticky="ew")
        ttk.Label(sel, text="Network").pack(side="left")
        self.net_var = tk.StringVar()
        self.net_combo = ttk.Combobox(sel, textvariable=self.net_var, state="disabled", width=10)
        self.net_combo.pack(side="left", padx=(6, 16))
        self.net_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_targets())
        ttk.Label(sel, text="Target").pack(side="left")
        self.target_var = tk.StringVar()
        self.target_combo = ttk.Combobox(sel, textvariable=self.target_var, state="disabled",
                                         width=44)
        self.target_combo.pack(side="left", padx=6, fill="x", expand=True)
        self.target_combo.bind("<<ComboboxSelected>>", lambda e: self._target_changed())

        self.mode_host = ttk.Frame(box)
        self.mode_host.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.mode_host.columnconfigure(0, weight=1)
        self.mode_host.rowconfigure(0, weight=1)
        self.placeholder = ttk.Label(self.mode_host, style="Muted.TLabel", anchor="center",
                                     text="Load a file to list its devices.")
        self.device_panel = self._build_device_panel(self.mode_host)
        self.batch_panel = self._build_batch_panel(self.mode_host)

        actions = ttk.Frame(box)
        actions.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.apply_btn = ttk.Button(actions, text="▶  Apply & Compile", style="Accent.TButton",
                                    command=self.apply_clicked)
        self.apply_btn.pack(side="right")
        self.check_btn = ttk.Button(actions, text="Check consistency", command=self.check_clicked)
        self.check_btn.pack(side="right", padx=8)
        self.mode_label = ttk.Label(actions, style="Muted.TLabel", text="")
        self.mode_label.pack(side="left")
        return box

    def _build_device_panel(self, parent) -> ttk.Frame:
        f = ttk.Frame(parent)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        self.dev_summary = ttk.Label(f, text="", justify="left")
        self.dev_summary.bind("<Configure>",
                              lambda e: self.dev_summary.configure(wraplength=e.width - 4))
        self.dev_summary.grid(row=0, column=0, sticky="ew")
        pane = ttk.PanedWindow(f, orient="horizontal")
        pane.grid(row=1, column=0, sticky="nsew", pady=(6, 0))

        left = ttk.Frame(pane)
        ttk.Label(left, text="Current running-config", style="Header.TLabel").pack(anchor="w")
        self.cfg_view = self._readonly_text(left, wrap="none", font=self.mono, width=40,
                                            height=8)
        self.cfg_view.master_frame.pack(fill="both", expand=True)
        pane.add(left, weight=1)

        right = ttk.Frame(pane)
        ttk.Label(right, text="CLI commands to apply (config mode)",
                  style="Header.TLabel").pack(anchor="w")
        # pack gives space in packing order: anchor the options row to the
        # bottom *before* the expanding text box, or it gets squeezed out.
        opts = ttk.Frame(right)
        opts.pack(side="bottom", fill="x", pady=(4, 0))
        self.cmd_text = self._editable_text(right)
        self.cmd_text.master_frame.pack(fill="both", expand=True)
        self.write_mem = tk.BooleanVar(value=False)
        self.write_mem_check = ttk.Checkbutton(
            opts, text="Also copy to startup-config (write memory)", variable=self.write_mem)
        self.write_mem_check.pack(side="left")
        ttk.Button(opts, text="Clear", command=lambda: self.cmd_text.delete("1.0", "end")
                   ).pack(side="right")
        pane.add(right, weight=1)
        return f

    def _build_batch_panel(self, parent) -> ttk.Frame:
        f = ttk.Frame(parent)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        ttk.Label(f, justify="left", style="Muted.TLabel", text=(
            'JSON plan: {"network": optional, "operations": [ {"device": ..., "op": ...}, ... ]}'
            "\nops: merge(config) · replace(config) · hostname(name) · "
            "set_ip(port, ip, mask[, gateway, no_shutdown]) · write_memory · rename(name)"),
        ).grid(row=0, column=0, sticky="w")
        self.batch_text = self._editable_text(f)
        self.batch_text.master_frame.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        row = ttk.Frame(f)
        row.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(row, text="Insert example for this file",
                   command=self._insert_example_plan).pack(side="left")
        ttk.Button(row, text="Validate JSON", command=self.validate_clicked).pack(side="left",
                                                                                 padx=6)
        ttk.Button(row, text="Format", command=self._format_plan).pack(side="left")
        return f

    def _build_log(self, parent) -> ttk.LabelFrame:
        box = ttk.LabelFrame(parent, text=" 4 · Execution log ", padding=6)
        self.log_text = self._readonly_text(box, wrap="word", font=self.mono, height=9)
        self.log_text.master_frame.pack(fill="both", expand=True)
        for level, color in LOG_COLORS.items():
            if color:
                self.log_text.tag_configure(level, foreground=color)
        self.log_text.tag_configure("ts", foreground="#8c959f")
        ttk.Button(box, text="Clear log", command=self._clear_log).pack(anchor="e", pady=(4, 0))
        return box

    # widget factories
    def _with_scroll(self, frame, widget) -> None:
        sb = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
        widget.configure(yscrollcommand=sb.set)
        widget.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _readonly_text(self, parent, **kw) -> ScrolledText:
        """Read-only, but selectable and copyable: click focuses it (a disabled
        Text otherwise doesn't take focus, so Ctrl+C would go elsewhere),
        Ctrl+A selects all, and right-click offers Copy / Select all."""
        frame = ttk.Frame(parent)
        t = ScrolledText(frame, state="disabled", relief="flat", borderwidth=1,
                         background="#f6f8fa", padx=6, pady=4, exportselection=True, **kw)
        t.pack(fill="both", expand=True)
        t.master_frame = frame
        t.bind("<Button-1>", lambda e: t.focus_set(), add="+")
        t.bind("<Control-a>", lambda e: self._select_all(t))
        t.bind("<Control-A>", lambda e: self._select_all(t))
        menu = tk.Menu(t, tearoff=False)
        menu.add_command(label="Copy", command=lambda: self._copy_text(t, selection=True))
        menu.add_command(label="Select all", command=lambda: self._select_all(t))
        menu.add_separator()
        menu.add_command(label="Copy all", command=lambda: self._copy_text(t, selection=False))
        t.bind("<Button-3>", lambda e: (t.focus_set(), menu.tk_popup(e.x_root, e.y_root)))
        return t

    # -- clipboard ------------------------------------------------------------------
    @staticmethod
    def _select_all(t) -> str:
        t.tag_add("sel", "1.0", "end-1c")
        t.mark_set("insert", "1.0")
        return "break"

    def _to_clipboard(self, text: str, what: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()  # keep it on the clipboard after focus changes
        self.status.set(f"Copied {what} ({len(text):,} characters)")

    def _copy_text(self, t, selection: bool) -> None:
        if selection and t.tag_ranges("sel"):
            text = t.get("sel.first", "sel.last")
        else:
            text = t.get("1.0", "end-1c")
        self._to_clipboard(text, "selection" if selection and t.tag_ranges("sel") else "text")

    @staticmethod
    def tree_to_text(tree, items=None, depth: int = 0) -> str:
        """Indented plain text of a Treeview (``items`` defaults to all roots)."""
        lines = []
        cols = tree["columns"]
        for iid in tree.get_children("") if items is None else items:
            vals = [str(v) for v in tree.item(iid, "values") if str(v)]
            label = tree.item(iid, "text")
            extra = ""
            if vals and cols == ("value",):
                extra = f": {vals[0]}"
            elif vals:
                extra = "  [" + ", ".join(
                    f"{v} pts" if c == "points" else v
                    for c, v in zip(cols, tree.item(iid, "values")) if str(v)) + "]"
            lines.append("  " * depth + label + extra)
            kids = tree.get_children(iid)
            if kids:
                lines.append(PtkitApp.tree_to_text(tree, kids, depth + 1))
        return "\n".join(l for l in lines if l)

    def _copy_tree(self, tree, selected_only: bool) -> str:
        sel = tree.selection() if selected_only else ()
        self._to_clipboard(self.tree_to_text(tree, sel or None),
                           "selected rows" if sel else "table")
        return "break"

    def copy_current_tab(self) -> None:
        """Copy the visible information or reference inspector tab."""
        current = self.info_nb.index(self.info_nb.select())
        if current == 0:
            self._copy_text(self.instr_text, selection=False)
        elif current == 1:
            self._copy_tree(self.meta_tree, selected_only=False)
        elif current == 2:
            text = self.check_note.cget("text") + "\n\n" + self.tree_to_text(self.assess_tree)
            self._to_clipboard(text.strip(), "grading checklist")
        else:
            self._copy_tree(self.reference_tree, selected_only=False)

    def _editable_text(self, parent) -> ScrolledText:
        frame = ttk.Frame(parent)
        t = ScrolledText(frame, wrap="none", font=self.mono, undo=True, relief="solid", height=8,
                         borderwidth=1, padx=6, pady=4, width=40)
        t.pack(fill="both", expand=True)
        t.master_frame = frame
        return t

    @staticmethod
    def _set_text(widget, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    # -- logging & task plumbing --------------------------------------------------
    def log(self, msg: str, level: str = "info") -> None:
        """Thread-safe: worker threads enqueue, the Tk thread writes."""
        if threading.current_thread() is not threading.main_thread():
            self._queue.put(("log", msg, level))
            return
        self._file_log.write(level, msg)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", time.strftime("%H:%M:%S "), "ts")
        self.log_text.insert("end", msg + "\n", level)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_tk_error(self, exc_type, exc, tb) -> None:
        self._set_busy(False)
        self.log("Unexpected error (details below; also saved to "
                 f"{self._file_log.path}):", "error")
        self.log("".join(traceback.format_exception(exc_type, exc, tb)).rstrip(), "error")

    def _clear_log(self) -> None:
        self._set_text(self.log_text, "")

    def _run_task(self, label: str, work, done) -> None:
        """Run ``work()`` off the Tk thread, then ``done(result, error)`` on it."""
        if self._busy:
            self.log("Another operation is still running.", "warn")
            return
        self._set_busy(True, label)
        if self.synchronous:
            try:
                result, err = work(), None
            except Exception as e:  # noqa: BLE001 - surfaced in the log
                result, err = None, e
            self._finish(done, result, err)
            return

        def runner():
            try:
                self._queue.put(("done", done, work(), None))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("done", done, None, e))

        threading.Thread(target=runner, daemon=True).start()

    def _drain_queue(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                if item[0] == "log":
                    self.log(item[1], item[2])
                else:
                    self._finish(*item[1:])
        except queue.Empty:
            pass
        self.root.after(50, self._drain_queue)

    def _finish(self, done, result, err) -> None:
        self._set_busy(False)
        if err is not None and not isinstance(err, EXPECTED_ERRORS):
            self.log("".join(traceback.format_exception(err)).rstrip(), "error")
        done(result, err)

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.apply_btn, self.check_btn, self.load_btn, self.export_btn,
                  self.import_btn):
            b.configure(state=state)
        self.solve_btn.configure(state="normal" if not busy and solver.available(self.working)
                                 else "disabled")
        if busy:
            self.status.set(label + "…")
            self.progress.pack(side="right")
            self.progress.start(12)
            self.root.configure(cursor="watch")
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.root.configure(cursor="")
            self._update_status()

    def _update_status(self) -> None:
        if not self.working:
            self.status.set("No file loaded")
            return
        kind = ".pka activity" if self.working.is_activity else ".pkt network"
        extra = f" · {self.applied_sets} change set(s) applied" if self.applied_sets else ""
        self.status.set(f"{self.loaded_path.name} · {kind} · "
                        f"{self.working.decoded.format.value} container{extra}")

    # -- file handling ------------------------------------------------------------
    def _initial_browse_dir(self) -> str:
        """Folder of the current input, else the newest Packet Tracer user
        saves folder (~/Cisco Packet Tracer <ver>/saves), else Documents."""
        cur = self.in_var.get().strip().strip('"')
        if cur and Path(cur).parent.is_dir():
            return str(Path(cur).parent)
        saves = [p for p in Path.home().glob("Cisco Packet Tracer*/saves") if p.is_dir()]
        if saves:
            return str(max(saves, key=lambda p: p.stat().st_mtime))
        return str(documents_dir())

    def _browse_input(self) -> None:
        path = filedialog.askopenfilename(title="Open Packet Tracer file", filetypes=FILETYPES,
                                          initialdir=self._initial_browse_dir())
        if path:
            self.in_var.set(path)
            self.load_clicked()

    def _browse_output(self) -> None:
        cur = Path(self.out_var.get() or self.in_var.get() or "output.pkt")
        path = filedialog.asksaveasfilename(
            title="Save compiled file as", initialdir=str(cur.parent), initialfile=cur.name,
            defaultextension=cur.suffix or ".pkt", filetypes=FILETYPES)
        if path:
            self.out_var.set(path)
            self._auto_output = None

    def load_clicked(self) -> None:
        raw = self.in_var.get().strip().strip('"')
        if not raw:
            self.log("Choose an input file first.", "warn")
            return
        path = Path(raw)
        if not path.is_file():
            self.log(f"Input file not found: {path}", "error")
            return
        if self.working is not None and self.applied_sets:
            if not self._confirm("Reload input?", "Reloading discards the in-memory working "
                                 "copy (already-compiled output files are kept). Continue?"):
                return
        self.log(f"Loading {path} …", "step")

        def work():
            t0 = time.perf_counter()
            pt = PTFile.load(path)
            return pt, time.perf_counter() - t0

        self._run_task("Decoding", work, lambda res, err: self._loaded(path, res, err))

    def _loaded(self, path: Path, res, err) -> None:
        if err is not None:
            self.log(f"Could not load {path.name}: {err}", "error")
            return
        pt, secs = res
        self.working, self.loaded_path, self.applied_sets = pt, path, 0
        self.reference_states = pt.extract_topologies()
        roles = list(self.reference_states)
        self.reference_combo.configure(values=roles, state="readonly" if roles else "disabled")
        self.reference_var.set(reference.preferred_state(self.reference_states))
        self._show_reference()
        d = pt.decoded
        self.log(f"Decoded {d.format.value} container in {secs:.2f}s: "
                 f"{d.original_size:,} bytes → {len(d.xml):,} bytes XML "
                 f"(PT {pt.version}).", "ok")
        if d.zlib_params is not None:
            self.log("Container parameters detected: unmodified re-encode is byte-identical.")
        if pt.is_protected:
            self.log("This activity has an Activity Wizard password. The configuration editor "
                     "targets the user network; XML export includes all network states and "
                     "grading data.", "warn")
        if not self.out_var.get() or self.out_var.get() == self._auto_output:
            self._auto_output = str(default_output_path(path))
            self.out_var.set(self._auto_output)
        self._populate_info()
        nets = [n.role for n in pt.networks()]
        self.net_combo.configure(values=nets, state="readonly" if len(nets) > 1 else "disabled")
        self.net_var.set(nets[0])
        self._refresh_targets()
        self._update_status()
        self.solve_btn.configure(state="normal" if solver.available(pt) else "disabled")

    # -- info panel ---------------------------------------------------------------
    def _populate_info(self) -> None:
        pt = self.working
        act = pt.activity
        if act is not None:
            pages = [html_to_text(p) for p in act.instructions()]
            text = "\n\n──────────\n\n".join(p for p in pages if p) or "(no instruction pages)"
        else:
            notes = [html_to_text(n) for n in pt.notes()]
            text = ("\n\n──────────\n\n".join(notes) if notes else
                    "(this .pkt file has no embedded instructions or notes)")
        self._set_text(self.instr_text, text)

        t = self.meta_tree
        t.delete(*t.get_children())
        rows = [("File", self.loaded_path.name),
                ("Kind", "Activity (.pka)" if pt.is_activity else "Network (.pkt)"),
                ("Container format", pt.decoded.format.value),
                ("Packet Tracer version", pt.version or "?"),
                ("Container size", f"{pt.decoded.original_size:,} bytes"),
                ("XML size", f"{len(pt.decoded.xml):,} bytes")]
        if pt.is_protected:
            rows.append(("Locked by author", "yes; complete XML export is available"))
        for k, v in rows:
            t.insert("", "end", text=k, values=(v,))
        for net in pt.networks():
            devs = net.devices()
            node = t.insert("", "end", text=f"Network: {net.role}",
                            values=(f"{len(devs)} devices, {len(net.links())} links",), open=True)
            for d in devs:
                addr = ", ".join(f"{p.name or p.index}={p.ip}" for p in d.ports() if p.ip)
                t.insert(node, "end", text=d.name, values=(
                    f"{d.type}/{d.model}" + (f"  [{addr}]" if addr else ""),))
        def add_parameters(parent, values):
            if isinstance(values, dict):
                entries = values.items()
            elif isinstance(values, list):
                entries = enumerate(values)
            else:
                return
            for key, value in entries:
                nested = isinstance(value, (dict, list))
                child = t.insert(parent, "end", text=str(key),
                                 values=("" if nested or value is None else str(value),))
                if nested:
                    add_parameters(child, value)

        node = t.insert("", "end", text="Embedded topology parameters")
        add_parameters(node, pt.extract_topologies())
        if act is not None:
            s = act.summary()
            node = t.insert("", "end", text="Activity", open=True,
                            values=(f"{s['graded_items']} graded items, "
                                    f"{s['total_points']} pts",))
            t.insert(node, "end", text="Instruction pages", values=(s["instruction_pages"],))

        a = self.assess_tree
        a.delete(*a.get_children())
        if act is None:
            self.check_note.configure(text="Not an activity file: there is no grading checklist.")
            return
        items = act.checklist()
        self.check_note.configure(text=(
            f"{len(items)} graded item(s): what Packet Tracer checks (as under Check Results "
            "› Assessment Items). Expected values are not shown; work them out from the "
            "instructions."))
        groups: dict[str, str] = {}
        for item in items:
            parts = list(item.parts)
            if len(parts) > 1 and parts[0] == "Network":  # common root of every item
                parts = parts[1:]
            group, rest = parts[0], parts[1:]
            if group not in groups:
                groups[group] = a.insert("", "end", text=group, open=True, values=("", ""))
            a.insert(groups[group], "end", text=" > ".join(rest) or group,
                     values=(item.points or "", item.components or ""))

    # -- target selection -------------------------------------------------------------
    def _network(self):
        return self.working.network(self.net_var.get() or None) if self.working else None

    def _refresh_targets(self) -> None:
        net = self._network()
        if net is None:
            return
        self._device_cache = {}
        labels = [BATCH_LABEL]
        for d in net.devices():
            kind = "IOS" if d.is_ios else "end device"
            label = f"{d.name}   —   {d.type} {d.model} ({kind})"
            self._device_cache[label] = d
            labels.append(label)
        self.target_combo.configure(values=labels, state="readonly")
        prev = self.target_var.get()
        if prev not in labels:
            # default to the first router, else the first IOS device
            ios = sorted((l for l, d in self._device_cache.items() if d.is_ios),
                         key=lambda l: self._device_cache[l].type != "Router")
            prev = ios[0] if ios else BATCH_LABEL
        self.target_var.set(prev)
        self._target_changed()
        if not self.batch_text.get("1.0", "end").strip():
            self._insert_example_plan()

    def _target_changed(self) -> None:
        label = self.target_var.get()
        if label == BATCH_LABEL:
            self._set_target_mode("batch")
        elif label in self._device_cache:
            self._set_target_mode("device")
            self._show_device(self._device_cache[label])

    def _set_target_mode(self, mode: str | None) -> None:
        for w in (self.placeholder, self.device_panel, self.batch_panel):
            w.grid_forget()
        if mode == "device":
            self.device_panel.grid(row=0, column=0, sticky="nsew")
            self.mode_label.configure(text="Device mode · ptkit apply")
        elif mode == "batch":
            self.batch_panel.grid(row=0, column=0, sticky="nsew")
            self.mode_label.configure(text="Batch mode · ptkit batch")
        else:
            self.placeholder.grid(row=0, column=0, sticky="nsew")
            self.mode_label.configure(text="")
        enabled = "normal" if mode and not self._busy else "disabled"
        self.apply_btn.configure(state=enabled)
        self.check_btn.configure(state=enabled)

    def _show_device(self, d: Device) -> None:
        ports = d.ports()
        up = [p for p in ports if p.ip]
        summary = (f"{d.name}  ·  {d.type} {d.model}  ·  hostname {d.hostname or '(n/a)'}  ·  "
                   f"{len(ports)} ports, {len(up)} addressed")
        if up:
            summary += "\n" + "   ".join(f"{p.name or 'port' + str(p.index)}: {p.ip}/{p.subnet}"
                                         for p in up[:6])
        self.dev_summary.configure(text=summary)
        if d.is_ios:
            self._set_text(self.cfg_view, "\n".join(d.running_config) or "(empty)")
            self.cmd_text.configure(state="normal", background="white")
        else:
            self._set_text(self.cfg_view, f"{d.name} is an end device ({d.type}) and has no IOS "
                           "CLI configuration.\n\nTo address it, use Batch mode, e.g.\n"
                           f'{{"device": "{d.name}", "op": "set_ip", "port": 0,\n'
                           ' "ip": "10.0.0.10", "mask": "255.255.255.0",\n'
                           ' "gateway": "10.0.0.1"}')
            self.cmd_text.configure(state="disabled", background="#eaeef2")

    def _insert_example_plan(self) -> None:
        self.batch_text.delete("1.0", "end")
        self.batch_text.insert("1.0", ops.example_plan(self._network()))

    def _format_plan(self) -> None:
        try:
            plan = ops.parse_plan(self.batch_text.get("1.0", "end"))
        except ValueError as e:
            self.log(f"Cannot format: {e}", "error")
            return
        self.batch_text.delete("1.0", "end")
        self.batch_text.insert("1.0", json.dumps(plan, indent=2))

    def validate_clicked(self) -> bool:
        try:
            plan = ops.parse_plan(self.batch_text.get("1.0", "end"))
        except ValueError as e:
            self.log(f"Batch plan invalid: {e}", "error")
            return False
        net = self._network()
        unknown = []
        if net is not None:
            try:
                target = self.working.network(plan.get("network", net.role))
                names = {d.name for d in target.devices()} | {d.hostname for d in target.devices()}
                unknown = sorted({op["device"] for op in plan["operations"]} - names)
            except KeyError as e:
                self.log(f"Batch plan invalid: {e.args[0]}", "error")
                return False
        if unknown:
            self.log(f"Batch plan refers to unknown device(s): {', '.join(unknown)}", "error")
            return False
        self.log(f"Batch plan valid: {len(plan['operations'])} operation(s) on "
                 f"{len({op['device'] for op in plan['operations']})} device(s).", "ok")
        return True

    # -- actions ------------------------------------------------------------------
    def _confirm(self, title: str, msg: str) -> bool:
        if self.synchronous:
            return True
        return messagebox.askyesno(title, msg, parent=self.root)

    def apply_clicked(self) -> None:
        if self.working is None:
            self.log("Load a file first.", "warn")
            return
        out_raw = self.out_var.get().strip().strip('"')
        if not out_raw:
            self.log("Set an output path first.", "warn")
            return
        out = Path(out_raw)
        if out.suffix.lower() not in (".pkt", ".pka"):
            self.log("Output file should end in .pkt or .pka.", "warn")
            return
        if not out.parent.is_dir():
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                self.log(f"Created output folder {out.parent}")
            except OSError as e:
                self.log(f"Cannot create output folder {out.parent}: {e}", "error")
                return
        if not is_writable_dir(out.parent):
            self.log(f"Cannot write to {out.parent} — it is read-only for normal programs "
                     "(e.g. under Program Files). Choose another output location with "
                     f"Browse…, for example {fallback_output_dir()}", "error")
            return
        if self.loaded_path and out.resolve() == self.loaded_path.resolve():
            if not self._confirm("Overwrite input?", f"Output is the input file:\n{out}\n\n"
                                 "Overwrite it?"):
                return

        mode = "batch" if self.target_var.get() == BATCH_LABEL else "device"
        net_role = self.net_var.get() or None
        if mode == "device":
            dev = self._device_cache.get(self.target_var.get())
            snippet = self.cmd_text.get("1.0", "end")
            if dev is None or not dev.is_ios:
                self.log("Select an IOS device (end devices are configured via Batch mode).",
                         "warn")
                return
            if not snippet.strip():
                self.log("Enter CLI commands to apply.", "warn")
                return
            dev_index, write_mem = dev.index, self.write_mem.get()
            self.log(f"Apply → {dev.name} [{net_role}]: "
                     f"{len(snippet.strip().splitlines())} command line(s)"
                     + (" + write memory" if write_mem else ""), "step")
        else:
            try:
                plan = ops.parse_plan(self.batch_text.get("1.0", "end"))
            except ValueError as e:
                self.log(f"Batch plan invalid: {e}", "error")
                return
            self.log(f"Batch → {len(plan['operations'])} operation(s)"
                     f" [{plan.get('network', net_role)}]", "step")

        base = self.working

        def work():
            candidate = ops.clone(base)
            if mode == "device":
                net = candidate.network(net_role)
                target = net.devices()[dev_index]
                changes = [f"{target.name}: {c}" for c in
                           ops.apply_snippet(target, snippet, write_memory=write_mem)]
            else:
                net, changes = ops.apply_plan(candidate, plan, net_role)
            for c in changes:
                self.log(f"  synced {c}")
            if not changes:
                self.log("  no structured fields needed syncing (config text updated)")
            self.log("Running port/config consistency checks…", "step")
            issues = []
            for n in candidate.networks():
                issues += [f"[{n.role}] {m}" for m in n.check()]
            for i in issues:
                self.log(f"  warning: {i}", "warn")
            if not issues:
                self.log("  consistent: config text and structured state agree", "ok")
            self.log(f"Encoding {candidate.decoded.format.value} container → {out}", "step")
            t0 = time.perf_counter()
            candidate.save(out)  # verifies decode(encode(xml)) == xml before replacing
            secs = time.perf_counter() - t0
            reread = PTFile.load(out)
            if reread.to_xml() != candidate.to_xml():
                raise RuntimeError("read-back verification failed: output differs")
            post = sum(len(n.check()) for n in reread.networks())
            return {"candidate": candidate, "changes": changes, "issues": issues,
                    "secs": secs, "size": out.stat().st_size, "post": post}

        self._run_task("Applying & compiling", work, lambda r, e: self._applied(out, r, e))

    def _applied(self, out: Path, res, err) -> None:
        if err is not None:
            msg = err.args[0] if isinstance(err, KeyError) and err.args else err
            if isinstance(err, PermissionError):
                msg = (f"permission denied writing {out}. Is the file open in another program, "
                       "or the folder read-only? Choose another output location.")
            self.log(f"Apply failed — nothing written, working copy unchanged: {msg}", "error")
            return
        self.working = res["candidate"]
        self.applied_sets += 1
        self.log(f"Wrote {out} ({res['size']:,} bytes, encoded+verified in {res['secs']:.2f}s). "
                 f"Read-back OK, {res['post']} consistency issue(s).", "ok")
        self._refresh_targets()
        self._populate_info()
        self._update_status()

    # -- XML export / import ------------------------------------------------------
    def solve_clicked(self, out: Path | None = None) -> None:
        if self._busy:
            return
        pt = self.working
        if pt is None:
            self.log("Load a .pka activity first.", "warn")
            return
        if not solver.available(pt):
            self.log("Solve PKA needs a user network plus an answer/reference topology "
                     "or explicit grading requirements.", "warn")
            return
        if out is None:
            picked = filedialog.asksaveasfilename(
                title="Save solution activity", defaultextension=".pka",
                initialdir=str(default_output_path(self.loaded_path).parent),
                initialfile=f"{self.loaded_path.stem}-solved.pka",
                filetypes=[("Packet Tracer activity", "*.pka")])
            if not picked:
                return
            out = Path(picked)
        out = Path(out)
        if out.suffix.lower() != ".pka":
            self.log("Solve PKA requires a .pka output filename.", "error")
            return
        self.log(f"Preparing solution and reconciling explicit grading requirements → {out}", "step")
        self._run_task("Solving PKA", lambda: solver.write_solution(
            pt, out, source=self.loaded_path), self._solved)

    def _solved(self, res, err) -> None:
        if err is not None:
            self.log(f"Solve PKA failed: {err}", "error")
            return
        self.log(f"Created {res.path} ({res.size:,} bytes): {res.devices} device(s). "
                 "Read-back verified. Original file, reference topology and checked criteria preserved.", "ok")
        if res.compatibility_repairs:
            self.log(f"Repaired {res.compatibility_repairs} legacy assessment value(s) using the "
                     "reference topology. Details are recorded in the solution report.")
        self.log(f"Assessment report: {res.matched} check(s) matched locally, "
                 f"{res.unresolved} unresolved locally. Report: {res.report_path}",
                 "warn" if res.unresolved else "info")
        self.log("Open the solution in Packet Tracer to run Check Results; custom activity "
                 "scripts and completion scores are evaluated there.")

    def export_xml_clicked(self, folder: Path | None = None) -> None:
        """Unpack the loaded document (including edits applied in this session)
        into ``<chosen folder>/<name>-xml/``."""
        if self.working is None:
            self.log("Load a file first, then export its XML.", "warn")
            return
        if folder is None:
            chosen = filedialog.askdirectory(
                title="Choose where to save the XML export",
                initialdir=str(fallback_output_dir() if fallback_output_dir().is_dir()
                               else documents_dir()))
            if not chosen:
                return
            folder = Path(chosen) / f"{self.loaded_path.stem}-xml"
        folder = Path(folder)
        if folder.exists() and any(folder.iterdir()):
            if not self._confirm("Replace export?", f"{folder}\nalready contains files. "
                                 "Overwrite the exported files in it?"):
                return
        pt, source = self.working, self.loaded_path
        self.log(f"Exporting XML → {folder}", "step")
        self._run_task("Exporting XML", lambda: xmlbundle.export_bundle(pt, folder, source),
                       self._exported)

    def _exported(self, res, err) -> None:
        if err is not None:
            self.log(f"Export failed: {err}", "error")
            return
        for name, size in res.files.items():
            self.log(f"  wrote {name} ({size:,} bytes)")
        if res.locked:
            self.log("  complete activity XML exported, including user, initial and answer "
                     "networks and grading data in document.xml")
        if self.applied_sets:
            self.log(f"  includes the {self.applied_sets} change set(s) applied in this session")
        self.log(f"Exported {len(res.files)} file(s) to {res.folder}", "ok")

    def import_xml_clicked(self, kind: str = "folder", source: Path | None = None,
                           out: Path | None = None) -> None:
        """Validate an export folder or XML file, then (after choosing where to
        save) re-encode it, verify it and load the result."""
        if source is None:
            start = str(fallback_output_dir() if fallback_output_dir().is_dir()
                        else documents_dir())
            picked = (filedialog.askdirectory(title="Choose an XML export folder",
                                              initialdir=start) if kind == "folder" else
                      filedialog.askopenfilename(title="Choose an XML file", initialdir=start,
                                                 filetypes=[("XML", "*.xml"),
                                                            ("All files", "*.*")]))
            if not picked:
                return
            source = Path(picked)
        source = Path(source)
        self.log(f"Validating {source} …", "step")
        self._run_task("Validating XML", lambda: xmlbundle.import_path(source),
                       lambda res, err: self._import_validated(source, res, err, out))

    def _import_validated(self, source: Path, res, err, out: Path | None) -> None:
        if err is not None:
            self.log(f"Import failed — validation error: {err}", "error")
            return
        for m in res.messages:
            self.log(f"  {m}", "warn" if m.startswith("warning:") else "info")
        if out is None:
            stem = source.name if source.is_dir() else source.stem
            stem = stem[:-4] if stem.endswith("-xml") else stem
            picked = filedialog.asksaveasfilename(
                title="Save re-encoded file as", defaultextension=res.suffix,
                initialdir=str(source.parent), initialfile=f"{stem}-reencoded{res.suffix}",
                filetypes=FILETYPES)
            if not picked:
                self.log("Import cancelled: no output file chosen.", "warn")
                return
            out = Path(picked)
        out = Path(out)
        if out.suffix.lower() != res.suffix:
            self.log(f"Note: saving as {res.suffix} (the document is a "
                     f"{'n activity' if res.suffix == '.pka' else ' network'}).", "warn")
            out = out.with_suffix(res.suffix)
        if not out.parent.is_dir() or not is_writable_dir(out.parent):
            self.log(f"Cannot write to {out.parent}; choose another location.", "error")
            return
        self.log(f"Encoding {res.fmt.value} container → {out}", "step")
        self._run_task("Re-encoding", lambda: xmlbundle.write_container(res, out),
                       lambda r, e: self._reencoded(out, e))

    def _reencoded(self, out: Path, err) -> None:
        if err is not None:
            self.log(f"Re-encode failed — nothing written: {err}", "error")
            return
        self.log(f"Re-encoded {out} ({out.stat().st_size:,} bytes): decoded again and "
                 "verified identical to the imported XML.", "ok")
        self.log("Loading the new file…", "step")
        self.in_var.set(str(out))
        self.out_var.set("")
        self.working = None  # it's a fresh document: no reload confirmation needed
        self.load_clicked()

    def check_clicked(self) -> None:
        if self.working is None:
            self.log("Load a file first.", "warn")
            return
        total = 0
        for n in self.working.networks():
            issues = n.check()
            total += len(issues)
            for i in issues:
                self.log(f"[{n.role}] {i}", "warn")
        if total:
            self.log(f"Consistency check: {total} issue(s).", "warn")
        else:
            self.log("Consistency check passed: config text and structured port state agree "
                     f"in all {len(self.working.networks())} network(s).", "ok")


def selftest() -> list[str]:
    """Exercise every layer the app depends on (crypto, codec, lossless XML,
    model edit + sync, Tk window). Returns failures; empty means healthy.
    Used by the build script to check the packaged exe, which has no console."""
    from . import codec
    from .twofish import Twofish

    failures = []

    def step(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failures.append(f"{name}: {type(e).__name__}: {e}")

    def crypto():
        ct = Twofish(bytes(16)).encrypt_block(bytes(16)).hex().upper()
        assert ct == "9F589F5CF6122C32B6BFEC2F2AE8C35A", ct

    xml = ("<PACKETTRACER5>\n <VERSION>9.0.0.0810</VERSION>\n <NETWORK>\n  <DEVICES>\n"
           "   <DEVICE>\n    <ENGINE>\n     <TYPE model=\"1941\" >Router</TYPE>\n"
           "     <NAME>R0</NAME>\n     <MODULE>\n      <PORT>\n"
           "       <TYPE>eCopperGigabitEthernet</TYPE>\n       <POWER>false</POWER>\n"
           "       <IP/>\n       <SUBNET/>\n      </PORT>\n     </MODULE>\n"
           "     <SYS_NAME>Router</SYS_NAME>\n     <RUNNINGCONFIG>\n"
           "      <LINE>hostname Router</LINE>\n      <LINE>interface GigabitEthernet0/0</LINE>\n"
           "      <LINE> no ip address</LINE>\n      <LINE> shutdown</LINE>\n"
           "     </RUNNINGCONFIG>\n     <STARTUPCONFIG/>\n    </ENGINE>\n   </DEVICE>\n"
           "  </DEVICES>\n  <LINKS/>\n </NETWORK>\n</PACKETTRACER5>\n").encode()

    def codec_loop():
        for fmt in codec.Format:
            blob = codec.encode(xml, fmt)
            d = codec.decode(blob)
            assert d.xml == xml and d.format is fmt
            assert codec.encode(d.xml, d.format, d.zlib_params) == blob

    def edit_loop():
        pt = PTFile.from_xml(xml)
        r = pt.network().device("R0")
        ops.apply_snippet(r, "hostname Lab\nint g0/0\n ip add 10.0.0.1 255.255.255.0\n no shut",
                          write_memory=True)
        again = PTFile.from_bytes(pt.to_bytes())
        r2 = again.network().device("R0")
        assert r2.hostname == "Lab" and r2.port(0).ip == "10.0.0.1" and r2.port(0).power
        assert again.network().check() == []

    def window():
        root = tk.Tk()
        root.withdraw()
        try:
            PtkitApp(root, synchronous=True)
            root.update()
        finally:
            root.destroy()

    def solve_loop():
        answer = xml.replace(b"hostname Router", b"hostname Solution").replace(
            b"<SYS_NAME>Router</SYS_NAME>", b"<SYS_NAME>Solution</SYS_NAME>")
        activity = (b"<PACKETTRACER5_ACTIVITY>" + xml + xml + answer
                    + b'<ACTIVITY PASS="internal"/></PACKETTRACER5_ACTIVITY>')
        pt = PTFile.from_xml(activity)
        solved = PTFile.from_bytes(codec.encode(solver.solution_xml(pt)))
        assert solved.network("user").device("R0").hostname == "Solution"
        assert solved.is_protected and pt.to_xml() == activity

    def grading_loop():
        comparisons = (b'<COMPARISONS><NODE><NAME>R0</NAME><ID>R0</ID><NODE>'
                       b'<NAME checkType="2" nodeValue="Graded">Host Name</NAME>'
                       b'<ID>HostName</ID><POINTS>1</POINTS></NODE></NODE></COMPARISONS>')
        activity = (b'<PACKETTRACER5_ACTIVITY>' + xml + comparisons
                    + b'<ACTIVITY PASS="internal"/></PACKETTRACER5_ACTIVITY>')
        candidate, report = solver.prepare_solution(PTFile.from_xml(activity))
        assert report['matched_locally'] == 1 and report['packet_tracer_score'] is None
        assert PTFile.from_xml(candidate).network('net0').device('R0').hostname == 'Graded'

    def legacy_grading_loop():
        from .compatibility import repair_legacy_assessment
        reference = xml.replace(b'</RUNNINGCONFIG>',
            b'<LINE>router ospf 1</LINE><LINE> passive-interface GigabitEthernet0/0</LINE>'
            b'</RUNNINGCONFIG>')
        comparisons = (b'<COMPARISONS><NODE><NAME>R0</NAME><ID>R0</ID><NODE>'
            b'<NAME>OSPF</NAME><ID>OSPF</ID><NODE><NAME>Process ID 1</NAME><ID>1</ID>'
            b'<NODE><NAME>Passive Interface</NAME><ID>Passive Interface</ID><NODE>'
            b'<NAME checkType="2" nodeValue="GigabitEthernet0/0">GigabitEthernet0/0</NAME>'
            b'<ID>GigabitEthernet0/0</ID></NODE></NODE></NODE></NODE></NODE></COMPARISONS>')
        pt = PTFile.from_xml(b'<PACKETTRACER5_ACTIVITY>' + xml + xml + reference
                             + comparisons + b'</PACKETTRACER5_ACTIVITY>')
        answer = pt.root.findall('PACKETTRACER5')[2]
        repairs = repair_legacy_assessment(pt, answer)
        assert len(repairs) == 1 and repairs[0]['expected'] == '1'

    def writable_probe():
        # Must return promptly for a read-only folder (Program Files) and a
        # writable one; run under a watchdog so a regression fails, not hangs.
        targets = [Path(os.environ.get("ProgramFiles", "")), fallback_output_dir().parent]
        result: dict = {}
        t = threading.Thread(target=lambda: result.update(
            v=[is_writable_dir(p) for p in targets if str(p)]), daemon=True)
        t.start()
        t.join(5)
        assert "v" in result, "write probe did not return within 5s"

    step("twofish", crypto)
    step("codec", codec_loop)
    step("edit", edit_loop)
    step("solve activity", solve_loop)
    step("reconcile grading", grading_loop)
    step("legacy assessment compatibility", legacy_grading_loop)
    step("write probe", writable_probe)
    step("tk window", window)
    return failures


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--selftest"]:
        failures = selftest()
        _FileLog(log_file_path()).write(
            "error" if failures else "ok",
            "selftest " + ("FAILED: " + "; ".join(failures) if failures else "passed"))
        return 1 if failures else 0
    if sys.platform == "win32":
        try:  # crisp text on high-DPI displays
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            # own taskbar identity (otherwise grouped as "Python")
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ptkit.workbench")
        except Exception:  # noqa: BLE001
            pass
    try:
        root = tk.Tk()
        _set_window_icon(root)
        app = PtkitApp(root)
        if argv:
            app.in_var.set(argv[0])
            if len(argv) > 1:
                app.out_var.set(argv[1])
            root.after(100, app.load_clicked)
        root.mainloop()
        return 0
    except Exception:  # noqa: BLE001 - a windowed app has nowhere else to report
        report = traceback.format_exc()
        path = log_file_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [fatal] {report}\n")
        except OSError:
            pass
        try:
            messagebox.showerror("ptkit failed to start", f"{report}\n\nLogged to {path}")
        except Exception:  # noqa: BLE001
            pass
        return 1


def _set_window_icon(root: tk.Tk) -> None:
    """Use the bundled .ico (exe build) or the repo's assets/ copy, if present."""
    here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    for cand in (here / "assets" / "ptkit.ico", here / "ptkit.ico"):
        if cand.is_file():
            try:
                root.iconbitmap(default=str(cand))
            except tk.TclError:
                pass
            return


if __name__ == "__main__":
    sys.exit(main())
