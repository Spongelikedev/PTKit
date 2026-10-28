# PTKit — Packet Tracer Toolkit

PTKit is a desktop workbench and Python toolkit for inspecting, editing, exporting,
and rebuilding Cisco Packet Tracer `.pkt` networks and `.pka` activities. It brings
instructions, device configuration, grading checklists, reference topologies, batch
editing, and solution-copy generation into one interface for lab authors and networking teams.

Published by **Sponge** on behalf of the project contributors. Licensed under the
[MIT License](LICENSE). **Use at your own risk:** users are responsible for their
own actions, permissions, compliance with applicable rules, and validation of
outputs. Read the [responsibility and risk notice](RESPONSIBLE_USE.md).
The desktop app's **License & use** button displays the bundled license and notices.

The Windows release is a portable application: download it, extract it, and run
`PTKit.exe`. Python and XML dependencies are bundled. It does not depend on a
particular username, computer name, project folder, or Packet Tracer installation
path. Cisco Packet Tracer is installed separately and is used to open the resulting
activities and run its actual grader.

## Features

- Read and write modern and legacy Packet Tracer containers.
- Preserve untouched XML, including Packet Tracer's serializer quirks.
- View embedded instructions, topology metadata, and grading checklists.
- Inspect user, initial, baseline, answer, and reference topology parameters,
  including activities with an Activity Wizard password.
- Edit IOS configuration or apply multi-device batch JSON plans.
- Export complete XML and network fragments, edit them, and re-import them.
- Create separate solution candidates from reference topology and supported
  explicit assessment values. Known legacy assessment formatting is repaired
  using reference-derived target values.
- Verify saved files by decoding them again before reporting success.

## Download and run — Windows

1. Open this repository's **Releases** page.
2. Download **PTKit-windows-x64.zip** and extract it to a writable folder.
3. Run **PTKit.exe**. No Python installation or installer is required.
4. Keep `LICENSE`, `RESPONSIBLE_USE.md`, `THIRD_PARTY_NOTICES.md` and `licenses/`
   with the distribution.
5. Use **Browse…** to select a `.pkt` or `.pka`, or drop a file onto the executable.

The release targets 64-bit Windows 10/11. A directly attached `PTKit.exe` is the
same executable as the one inside the ZIP; the ZIP is preferred because it also
contains instructions and third-party notices. The executable is not code-signed.

PTKit processes files locally. It does not upload activities or contact a server.
It can inspect files without a Packet Tracer installation; opening outputs and
checking completion requires the recipient's own Packet Tracer installation.
Modern and legacy container support does not imply that every Packet Tracer
version can load every activity or emulate every device.

## Basic workflow

1. **Load a file.** Choose an input and an output path. Normal edits default to a
   separate `-ptkit` file. Read-only input folders fall back to the current user's
   Documents folder.
2. **Inspect the information tabs.** Read Instructions, Metadata, Grading checklist,
   or **Target & Reference Values** beside the device editor.
3. **Select a configuration target.** Choose a network and device, or Batch mode.
4. **Apply & Compile.** Changes are applied to an independent working copy, checked,
   encoded, and read back. Successive changes accumulate in the editor session.
5. **Open the output in Packet Tracer.** Use its Check Results and connectivity
   tests to validate the activity in the simulator.

### Reference values

The **Reference Topology Parameters** tree selects an answer/reference network
when available, then an initial/baseline state. Its selector exposes other embedded
states. A standard `.pkt` retains the baseline captured when it was opened, even
after edits are applied.

**Copy Reference Parameters to Batch JSON** copies supported operations targeting
the editor's selected working network. Paste the JSON into Batch mode to review
and apply it. Other XML fields remain in `reference_parameters` metadata for review;
metadata itself does not execute changes.

### Solve PKA

Click **Solve PKA…** and choose a separate output filename. The solver copies the
embedded answer/reference topology into the user network, then reconciles supported
explicit grading values such as hostname, IPv4 address, subnet mask, endpoint
DNS/gateway, and power state. It also handles activities with an Activity Wizard
password. The source file and editor session remain unchanged.

A companion `.solve-report.json` records local checks, unsupported requirements,
and any legacy assessment compatibility repairs. Repairs preserve selected checks
and point totals, deriving compatible serialized targets from the unchanged answer
network. Named grading variables and explicit overrides are retained.

**A solution candidate is not a universal 100% guarantee.** Custom scripts, runtime
objects, progress conditions, and unsupported checks require evaluation in Packet
Tracer. A locally unresolved check can still pass in Packet Tracer. The legacy
Skills Integration Challenge used during validation reached 100% in Packet Tracer
9.0.1, including all assessment categories and both connectivity checks; that result
is specific to that activity.

### XML export and import

**Export XML Data…** creates `document.xml`, per-network XML where recognized, and
`manifest.json`. Complete exports include reference networks and grading data,
including for password-marked activities. New manifests record the source filename,
not an absolute computer path.

**Import XML & Re-encode** accepts an export folder or a complete XML document.
Edit either `document.xml` or individual network fragments, then import the folder.
Editing both representations is rejected as ambiguous. Complete bundles do not
require the original container. Older user-only bundles still require their
original source file.

## Run from source

Requirements: Python 3.10 or later, `lxml`, and Tkinter for the GUI. On Linux,
Tkinter may be supplied as a separate operating-system package. Source operation
is intended to be portable; the packaged EXE and automated release build are
Windows-only.

On Windows, open PowerShell in the repository folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe gui.py
```

Alternatively, `setup_env.ps1` creates the environment and runs the core tests.
On macOS/Linux, use `sh setup_env.sh`, then `.venv/bin/python gui.py`.
No source edit is needed to change the user profile or Packet Tracer folder.

## Command line and Python API

After installing from source, these commands are available in the virtual environment:

```text
ptkit info lab.pka --json
ptkit devices lab.pkt
ptkit check lab.pkt
ptkit export lab.pka -o lab-xml
ptkit import lab-xml -o rebuilt.pka
ptkit batch lab.pka plan.json -o edited.pka
ptkit-gui
```

Example batch plan; addresses below are synthetic lab data:

```json
{
  "network": "user",
  "operations": [
    {"device": "Router0", "op": "hostname", "name": "R1"},
    {"device": "Router0", "op": "set_ip", "port": "GigabitEthernet0/0",
     "ip": "192.0.2.1", "mask": "255.255.255.0", "no_shutdown": true}
  ]
}
```

Python inspection:

```python
from ptkit import PTFile

activity = PTFile.load("lab.pka")
parameters = activity.extract_topologies()
```

## Development, testing, and releases

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m "not samples"
.\build_exe.ps1
```

The build creates an audited `release/PTKit.exe`, a Windows distribution ZIP,
a source ZIP, a repository-ready folder, checksums, and a targeted privacy report.
It tests the executable after moving it to another folder with a minimal PATH.
Optional sample tests discover the local Packet Tracer saves directory or use
`PTKIT_SAMPLES`; no Cisco activities are shipped with this project.

For exactly what to upload and how to create a release, see
[Releasing](docs/RELEASING.md). For generated data, logs, exclusions, and commit
identity, see [Privacy](docs/PRIVACY.md).

The included GitHub Actions workflow builds release candidates on a Windows runner.
It runs manually or on version tags and uploads workflow artifacts; it does not
publish a release automatically. This workflow must be tested in the group's
repository after upload.

## Privacy and distribution

Upload the contents of the generated **release/repository/** folder as the repository
root. Keep actual activities, XML exports, solve reports, screenshots, logs, virtual
environments, test output, and old builds out of Git. The supplied `.gitignore`
and source-bundle allowlist exclude them.

Runtime logs are local to the current user, normally under
`%LOCALAPPDATA%/ptkit/ptkit-gui.log`. Logs and the contents of input/output activities
can contain filenames, device names, addresses and credentials; review them before
sharing. New solve reports store filenames instead of full local paths.

## License and responsibility

PTKit is licensed under the standard [MIT License](LICENSE), including its warranty
disclaimer and limitation of liability. The public notice uses **Sponge and
contributors**; the group does not need to be a company to publish this project.

Users are responsible for their own actions and use the software at their own
risk, subject to applicable law. Keep backups and independently check outputs.
The license does not guarantee complete immunity from liability or grant rights
the contributors do not hold. It does not authorize violations of third-party
licenses, access restrictions, institutional policies, or applicable law.

PTKit is independent of Cisco and does not include Packet Tracer itself. Cisco
and third-party components retain their own rights and licenses. See the
[responsibility notice](RESPONSIBLE_USE.md) and
[third-party notices](THIRD_PARTY_NOTICES.md).
