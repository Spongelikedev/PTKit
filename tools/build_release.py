"""Build the standalone Windows executable and test it after relocation."""
from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def collect_licenses(root):
    target = root / 'build' / 'licenses'
    target.mkdir(parents=True, exist_ok=True)
    base = Path(sys.base_prefix)
    for name in ('LICENSE.txt', 'LICENSE'):
        if (base / name).is_file():
            shutil.copyfile(base / name, target / 'Python-LICENSE.txt')
            break
    else:
        raise RuntimeError('Python license file was not found in this Python installation.')
    for package in ('lxml', 'pyinstaller'):
        distribution = importlib.metadata.distribution(package)
        count = 0
        for entry in distribution.files or []:
            if '/licenses/' in str(entry).replace('\\', '/').lower():
                path = Path(distribution.locate_file(entry))
                if path.is_file():
                    shutil.copyfile(path, target / (package + '-' + path.name))
                    count += 1
        if not count:
            raise RuntimeError(f'License notices were not found for {package}. Upgrade the build dependency.')
    for folder in ('tcl8.6', 'tk8.6', 'tcl9.0', 'tk9.0'):
        path = base / 'tcl' / folder / 'license.terms'
        if path.is_file():
            shutil.copyfile(path, target / (folder + '-license.terms'))


def smoke_test(exe, root):
    # No source checkout, developer Python, or Cisco installation is placed on
    # PATH. The EXE must bring its own runtime and work from a folder with spaces.
    with tempfile.TemporaryDirectory(prefix='portable smoke ', dir=root / 'build') as directory:
        work = Path(directory)
        relocated = work / 'PTKit.exe'
        shutil.copyfile(exe, relocated)
        env = os.environ.copy()
        for name in ('PYTHONPATH', 'PYTHONHOME', 'TCL_LIBRARY', 'TK_LIBRARY'):
            env.pop(name, None)
        system = Path(env.get('SystemRoot', r'C:\Windows'))
        env['PATH'] = str(system / 'System32') + os.pathsep + str(system)
        env['LOCALAPPDATA'] = str(work / 'appdata')
        env['PTKIT_LOG_FILE'] = str(work / 'selftest.log')
        result = subprocess.run([str(relocated), '--selftest'], cwd=work, env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=90)
        log = work / 'selftest.log'
        if result.returncode or not log.is_file() or 'selftest passed' not in log.read_text(encoding='utf-8'):
            raise RuntimeError('Relocated executable self-test failed.')
    print('Standalone executable passed its self-test after relocation with a minimal PATH.')


def main():
    if sys.platform != 'win32':
        raise SystemExit('Build the Windows executable on Windows. Other platforms can run PTKit from source.')
    root = Path(__file__).resolve().parents[1]
    collect_licenses(root)
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                    '--distpath', str(root / 'release'), '--workpath', str(root / 'build' / 'release'),
                    str(root / 'PTKit.spec')], cwd=root, check=True)
    smoke_test(root / 'release' / 'PTKit.exe', root)
    try:
        from .package_release import package
    except ImportError:
        from package_release import package
    count = package(root, root / 'release')
    print(f'Release ready: PTKit.exe, Windows ZIP, {count}-file source bundle, privacy report and checksums.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
