# Uploading the repository and creating releases

## First upload

1. Use `release/repository/`, which is the clean repository-ready source folder.
   Alternatively, extract `release/PTKit-source.zip` into a new folder.
2. Put its **contents** at the root of the group's private repository. `README.md`,
   `pyproject.toml`, `gui.py`, `src/`, and the other listed items belong at that root.
   Do not upload the source ZIP as the only file in the repository.
3. Include `.gitignore` and `.github/`; file pickers may hide names starting with a dot.
   A Git client is preferable for retaining the folder structure and hidden files.
4. Review the staged files and the local commit identity before pushing.

Source contents:

```text
.github/workflows/windows-build.yml
.gitignore
README.md
LICENSE
RESPONSIBLE_USE.md
THIRD_PARTY_NOTICES.md
pyproject.toml
PTKit.spec
gui.py
setup_env.ps1
setup_env.sh
build_exe.ps1
assets/                  icon and generic Windows version metadata
docs/                    privacy and release instructions
src/ptkit/               application and library code
tests/                   synthetic tests, optional local sample discovery
tools/                   build, packaging and privacy-audit helpers
```

Do not add the original working folder's `solutions/`, `.venv/`, `build/`, `dist/`,
test output, logs, XML exports or activity files. The generated source ZIP and
repository-ready folder exclude those files regardless of what exists locally.

## Recommended release assets

Create a release for the matching version tag, for example `v0.2.1`, and attach:

- **PTKit-windows-x64.zip** — recommended download: EXE, README, MIT license, responsibility notice and third-party notices/licenses.
- **PTKit.exe** — optional direct download; exactly the EXE inside the Windows ZIP.
- **SHA256SUMS.txt** — SHA-256 checksums for the EXE and both ZIP files.
- **PTKit-source.zip** — optional curated source download, in addition to repository source.
- **privacy-check.json** — optional audit summary with no private identifier values.

Use the newly audited `release/PTKit.exe`, not any older `dist/` executable.
No environment folder, separate Python installation, or Cisco installation path
needs to be distributed with the EXE. Cisco Packet Tracer itself is not bundled.

## Build on another Windows computer

Use 64-bit Python with Tkinter. From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,build]"
.\.venv\Scripts\python.exe -m pytest -q -m "not samples"
.\.venv\Scripts\python.exe tools/build_release.py
```

`build_exe.ps1` wraps dependency installation and the release build. Its optional
`-Install` switch adds current-user Start Menu and Desktop shortcuts. Default
builds do not install shortcuts or change system settings.

The builder gathers installed runtime license notices, builds the EXE with
relative project inputs, runs a relocated self-test, audits source and decompressed
executable contents, prepares the upload folder and ZIPs, and generates checksums.
If an audit fails, review it before distributing the candidate. Do not publish
logs or an entire `build/` directory as troubleshooting attachments.

To regenerate only source staging/ZIPs, run `python tools/package_release.py`.
If a PTKit.exe is already present in the output directory, that command audits it
and recreates its Windows ZIP too; build again after changing application code.

## GitHub-hosted build

The included **Windows build** workflow runs manually from Actions or when a `v*`
tag is pushed. It installs Python, runs tests without Cisco samples, builds and
self-tests the EXE, and uploads release candidates as workflow artifacts. Download
the artifact, review the audit and checksums, and attach the files to a release.
The workflow does not publish to Releases or request a personal access token.

It uses the official [checkout](https://github.com/actions/checkout),
[setup-python](https://github.com/actions/setup-python) and
[upload-artifact](https://github.com/actions/upload-artifact) actions. The workflow
has been prepared locally; verify its first run in the private repository.

## Before publishing a new version

Update the version in `pyproject.toml`, `src/ptkit/__init__.py`, and
`assets/windows-version.txt`; keep all three aligned. Build from the corresponding
source revision, test the release, and upload only the selected assets above.
The build checks local identifiers, but maintainers should still review new source
and generated data before sharing. Retain the MIT license, responsibility notice
and third-party notices in the repository and release packages. Only license
contributions for which the contributors can grant permission; the publisher name
does not transfer other contributors' copyrights or grant rights to Cisco content.
