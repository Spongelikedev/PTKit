"""Launch the ptkit desktop GUI.

    python gui.py [input.pkt|.pka [output.pkt|.pka]]

Works from a source checkout without installing (adds ./src to sys.path).
If the interpreter running this file lacks ptkit's dependencies (e.g. the
system Python rather than the project's .venv), it relaunches itself with
.venv's interpreter; failing that it explains what to do in a dialog instead
of dying silently. The packaged build (dist/PTKit.exe) needs none of this.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))


def _deps_missing() -> str | None:
    try:
        import lxml.etree  # noqa: F401
    except ImportError as e:
        return str(e)
    return None


def _relaunch_in_venv() -> bool:
    scripts = ROOT / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    for name in ("pythonw.exe", "python.exe", "python"):
        exe = scripts / name
        if exe.is_file():
            if Path(sys.executable).resolve() == exe.resolve():
                return False  # already in the venv: relaunching would loop
            subprocess.Popen([str(exe), str(Path(__file__).resolve()), *sys.argv[1:]],
                             cwd=str(ROOT))
            return True
    return False


if __name__ == "__main__":
    problem = _deps_missing()
    if problem:
        if _relaunch_in_venv():
            sys.exit(0)
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "ptkit: dependencies missing",
            f"{problem}\n\nThis Python ({sys.executable}) does not have ptkit's "
            "dependencies installed.\n\nRun setup_env.ps1 in the project folder once, "
            "or start dist\\PTKit.exe instead.")
        sys.exit(1)
    from ptkit.gui import main
    sys.exit(main())
