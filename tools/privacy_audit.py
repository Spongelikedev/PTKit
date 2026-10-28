"""Scan selected source or decompressed executable entries for local identifiers.

This is a targeted leak check, not a general secret detector. Findings name the
affected file/module without copying private content into the public report.
"""
from __future__ import annotations

import argparse
import getpass
import io
import json
import os
from pathlib import Path
import re
import zipfile

try:
    from .release_files import source_files
except ImportError:
    from release_files import source_files


def local_tokens():
    generic = {'user', 'root', 'runner', 'runneradmin', 'administrator', 'system', 'builder'}
    values = [getpass.getuser(), Path.home().name, os.environ.get('COMPUTERNAME', '')]
    return [s for s in set(values) if len(s) >= 4 and s.casefold() not in generic]


def scan_blob(name: str, blob: bytes, tokens) -> list[dict]:
    findings = []
    texts = [blob.decode('utf-8', errors='ignore'), blob.decode('utf-16-le', errors='ignore')]
    for token in tokens:
        pattern = re.compile(r'(?<![\w])' + re.escape(token) + r'(?![\w])', re.I)
        if any(pattern.search(text) for text in texts):
            findings.append({'entry': name, 'reason': 'local identifier found'})
            break
    return findings


def audit_source(root: Path, tokens=None) -> dict:
    tokens = local_tokens() if tokens is None else tokens
    findings = []
    files = source_files(root)
    for path in files:
        name = path.relative_to(root).as_posix()
        data = path.read_bytes()
        findings.extend(scan_blob(name, name.encode() + b'\n' + data, tokens))
        # Source files should not contain any real user-profile path, even
        # when it belongs to a different contributor than the current builder.
        text = data.decode('utf-8', errors='ignore')
        if re.search(r'[A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+\w|/(?:home|Users)/\w', text):
            findings.append({'entry': name, 'reason': 'absolute user-profile path found'})
    return {'kind': 'source', 'entries_scanned': len(files), 'findings': findings,
            'passed': not findings}


def audit_executable(path: Path, tokens=None) -> dict:
    from PyInstaller.archive.readers import CArchiveReader
    tokens = local_tokens() if tokens is None else tokens
    archive = CArchiveReader(str(path))
    findings, count = [], 0

    def scan(name, blob):
        nonlocal count
        count += 1
        findings.extend(scan_blob(name, blob, tokens))

    scan('executable', path.read_bytes())
    for name, entry in archive.toc.items():
        blob = archive.extract(name)
        scan(name, blob)
        if entry[-1] == 'z':
            pyz = archive.open_embedded_archive(name)
            for module in pyz.toc:
                data = pyz.extract(module, raw=True)
                if data:
                    scan(name + ':' + module, data)
        elif name.endswith('.zip'):
            with zipfile.ZipFile(io.BytesIO(blob)) as library:
                for member in library.namelist():
                    scan(name + ':' + member, library.read(member))
    return {'kind': 'executable', 'entries_scanned': count, 'findings': findings,
            'passed': not findings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--deny', action='append', default=[], help='additional private identifier to reject')
    args = parser.parse_args()
    tokens = local_tokens() + args.deny
    report = audit_executable(args.exe, tokens) if args.exe else audit_source(args.root.resolve(), tokens)
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
