# Privacy and portable operation

## What is safe to put in the repository

Use the generated `release/repository/` directory, or extract `PTKit-source.zip`
and upload its contents at the repository root. It contains only the explicit
source inventory: code, synthetic tests, build tools, documentation, workflow,
icon and generic Windows version metadata.

The release intentionally identifies the public publisher as Sponge and credits
contributors collectively. It does not require other contributors' personal names,
emails, account tokens, or hardcoded workstation paths. Addresses and passwords in
synthetic tests are fixture data, not the developer's network or credentials.
The icon is generated from simple shapes and carries no screenshot/photo metadata.

The audit checks current build-user and computer identifiers in the selected
source and in decompressed executable entries, including embedded Python modules
and the standard-library archive. Extra identifiers can be supplied with
`python tools/privacy_audit.py --deny IDENTIFIER`. The public audit report omits
private token values and absolute build paths.

This is a targeted leak check, not a general proof that a future edit contains no
secret. Third-party runtime libraries may carry their own upstream build paths
or license author names; those are not the workstation owner's identity.

## Files to keep private

Do not upload the development workspace wholesale. Keep these out of Git and
release assets:

- `.venv/`, `__pycache__/`, `.pytest_cache/`, `.test-tmp*/`, `build/`, old `dist/` builds.
- `solutions/`, actual `.pkt`/`.pka`/`.pkz` files, XML export folders and manifests.
- Solve reports, validation reports, screenshots, logs and `PROGRESS.md`.
- `.env` files, private keys, certificates with private keys, access tokens or credentials.

These exclusions are in `.gitignore`, and release packaging uses an additional
allowlist. Ignoring a file does not remove it from an existing Git history or
from a ZIP that someone manually assembled. Review staged files before the
first push. If private files were committed earlier, removing them in a new
commit alone does not remove the old copies from history.

## Git and GitHub identity

A clean source tree does not anonymize the account that pushes it or the author
fields of Git commits. Before creating commits, choose the group-approved author
name and commit email for this repository. Configure them locally to this clone
if needed; there is no need to change the user's global Git identity. A private
repository still exposes that metadata to its collaborators.

## What happens when someone runs PTKit

PTKit reads and writes chosen local files. It does not use an upload API, telemetry,
update service or developer-hosted backend. The EXE includes its runtime and does
not launch or depend on the developer's Packet Tracer installation.

Documents, fallback outputs, local logs and browse locations are calculated from
the recipient's user profile and environment. No absolute source-workstation path
is required. Runtime logs can include the recipient's paths and activity data;
they are intentionally excluded from source/release packaging.

New complete XML manifests and solve reports use filenames rather than absolute
computer paths. Filenames and the activity/configuration contents can still be
sensitive; PTKit cannot anonymize a real topology while preserving all of its
meaning. Old user-only XML bundles can contain an original absolute source path
because their legacy re-import process needs that file.

## Portability validation

The release builder relocates the EXE to a folder with spaces, removes developer
Python/Tcl configuration from the child environment, limits PATH to Windows system
folders, and runs the packaged self-test. That checks for dependencies on the
source checkout and developer tools. It does not replace testing on another
physical computer or testing every possible Packet Tracer version and activity.
