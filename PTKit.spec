# Portable PyInstaller build specification. All build inputs are relative to this file.
from pathlib import Path

root = Path(SPECPATH)
datas = [(str(root / 'assets' / 'ptkit.ico'), 'assets'),
         (str(root / 'LICENSE'), '.'),
         (str(root / 'RESPONSIBLE_USE.md'), '.'),
         (str(root / 'THIRD_PARTY_NOTICES.md'), '.'),
         (str(root / 'build' / 'licenses'), 'licenses')]
a = Analysis([str(root / 'gui.py')], pathex=[str(root / 'src')],
             binaries=[], datas=datas, hiddenimports=[], hookspath=[], hooksconfig={},
             runtime_hooks=[], excludes=[], noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='PTKit', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          disable_windowed_traceback=False, argv_emulation=False,
          icon=str(root / 'assets' / 'ptkit.ico'),
          version=str(root / 'assets' / 'windows-version.txt'))
