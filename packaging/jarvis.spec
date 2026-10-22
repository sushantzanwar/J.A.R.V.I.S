# PyInstaller spec for the Jarvis desktop app (one folder, windowed). Build with scripts\build.ps1.
# Only the UI is packaged; bridge/ and the worker stay in the project folder.
from pathlib import Path

root = Path(SPECPATH).parent

a = Analysis(
    [str(root / "jarvis" / "__main__.py")],
    pathex=[str(root)],
    datas=[(str(root / "jarvis" / "ui" / "qml"), "jarvis/ui/qml")],
    hiddenimports=[],
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Jarvis", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="Jarvis")
