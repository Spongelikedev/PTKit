"""Prepare audited repository files and Windows release assets."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

try:
    from .release_files import source_files
    from .privacy_audit import audit_source, audit_executable
except ImportError:
    from release_files import source_files
    from privacy_audit import audit_source, audit_executable


def write_zip(path, entries):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)


def package(root: Path, output: Path):
    root, output = root.resolve(), output.resolve()
    if output == root or root.is_relative_to(output):
        raise ValueError('Release output must not be the project root or its parent.')
    output.mkdir(parents=True, exist_ok=True)
    source_report = audit_source(root)
    if not source_report['passed']:
        raise ValueError('Source privacy audit failed; run tools/privacy_audit.py for details.')
    inventory = source_files(root)
    stage = output / 'repository'
    allowed = {p.relative_to(root).as_posix() for p in inventory}
    if stage.exists():
        if stage.is_symlink() or not stage.resolve().is_relative_to(output):
            raise ValueError('Unsafe repository staging directory.')
        extras = [p for p in stage.rglob('*') if p.is_file()
                  and p.relative_to(stage).as_posix() not in allowed]
        if extras:
            raise ValueError('Staging contains extra files; use a fresh release output folder.')
    entries = []
    for path in inventory:
        relative = path.relative_to(root).as_posix()
        target = stage / relative
        if target.is_symlink() or not target.resolve().is_relative_to(stage.resolve()):
            raise ValueError('Unsafe source staging destination.')
        target.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        target.write_bytes(data)
        entries.append((relative, data))
    write_zip(output / 'PTKit-source.zip', entries)
    reports = [source_report]
    exe = output / 'PTKit.exe'
    if exe.exists():
        binary_report = audit_executable(exe)
        if not binary_report['passed']:
            raise ValueError('Executable privacy audit failed; no Windows ZIP was produced.')
        reports.append(binary_report)
        windows_entries = [('PTKit.exe', exe.read_bytes()),
                           ('LICENSE', (root / 'LICENSE').read_bytes()),
                           ('RESPONSIBLE_USE.md', (root / 'RESPONSIBLE_USE.md').read_bytes()),
                           ('README.md', (root / 'README.md').read_bytes()),
                           ('THIRD_PARTY_NOTICES.md', (root / 'THIRD_PARTY_NOTICES.md').read_bytes())]
        licenses = root / 'build' / 'licenses'
        if not licenses.is_dir() or not any(licenses.iterdir()):
            raise ValueError('Build license notices first with tools/build_release.py.')
        windows_entries.extend(('licenses/' + p.relative_to(licenses).as_posix(), p.read_bytes())
                               for p in licenses.rglob('*') if p.is_file())
        for path in licenses.rglob('*'):
            if path.is_file():
                target = output / 'licenses' / path.relative_to(licenses)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        write_zip(output / 'PTKit-windows-x64.zip', windows_entries)
        for name in ('LICENSE', 'RESPONSIBLE_USE.md', 'THIRD_PARTY_NOTICES.md'):
            shutil.copyfile(root / name, output / name)
    (output / 'privacy-check.json').write_text(json.dumps({'audits': reports}, indent=2), encoding='utf-8')
    assets = [p for p in output.iterdir() if p.is_file() and p.suffix in ('.zip', '.exe')]
    checksums = ''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n'
                        for p in sorted(assets))
    (output / 'SHA256SUMS.txt').write_text(checksums, encoding='ascii')
    return len(inventory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    count = package(root, args.output or root / 'release')
    print(f'Prepared {count} source files; upload the contents of release/repository to the repository.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
