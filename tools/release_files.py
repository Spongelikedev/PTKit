"""Explicit source-distribution inventory. Never package the whole workspace."""
from pathlib import Path

ROOT_FILES = (
    '.gitignore', 'README.md', 'LICENSE', 'RESPONSIBLE_USE.md', 'THIRD_PARTY_NOTICES.md', 'pyproject.toml',
    'gui.py', 'setup_env.ps1', 'setup_env.sh', 'build_exe.ps1', 'PTKit.spec',
    'assets/ptkit.ico', 'assets/windows-version.txt',
    'docs/PRIVACY.md', 'docs/RELEASING.md',
    '.github/workflows/windows-build.yml',
    'tools/__init__.py', 'tools/make_icon.py', 'tools/release_files.py',
    'tools/privacy_audit.py', 'tools/build_release.py', 'tools/package_release.py',
)


def source_files(root: Path) -> list[Path]:
    root = root.resolve()
    files = [root / name for name in ROOT_FILES]
    for folder in ('src/ptkit', 'tests'):
        files.extend(p for p in (root / folder).rglob('*.py') if '__pycache__' not in p.parts)
    for path in files:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f'Missing or unsafe distribution file: {path.relative_to(root)}')
    return sorted(set(files), key=lambda p: p.relative_to(root).as_posix())
