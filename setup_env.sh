#!/usr/bin/env sh
# Source use on macOS/Linux; the release EXE is Windows-only.
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
[ -d .venv ] || "$PY" -m venv .venv
VPY=.venv/bin/python
[ -x "$VPY" ] || VPY=.venv/Scripts/python.exe
"$VPY" -m pip install --upgrade pip
"$VPY" -m pip install -e '.[dev]'
"$VPY" -m pytest -q -m 'not samples'
printf '\nReady. Launch with the virtual environment Python: gui.py\n'
