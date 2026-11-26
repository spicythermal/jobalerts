# PyInstaller build of the desktop app: `pyinstaller packaging/jobalerts.spec` from the repository root.
# Output: dist/jobalerts/ (Windows; Linux for testing). See docs/releasing.md.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).parent
version = {}
exec((root / "src" / "jobalerts" / "__init__.py").read_text(), version)

hidden = collect_submodules("uvicorn") + collect_submodules("jobalerts") + collect_submodules("pystray")
datas = [(str(root / "config"), "config")] + collect_data_files("jobalerts") + collect_data_files("certifi") \
    + collect_data_files("tzdata")

a = Analysis(
    [str(root / "packaging" / "launcher.py")],
    pathex=[str(root / "src")],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "ruff", "IPython", "matplotlib", "numpy"],
)
pyz = PYZ(a.pure)
icon = root / "packaging" / "jobalerts.ico"
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="jobalerts", console=False,
          icon=str(icon) if sys.platform == "win32" and icon.is_file() else None)
coll = COLLECT(exe, a.binaries, a.datas, name="jobalerts")
