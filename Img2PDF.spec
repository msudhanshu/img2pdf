# -*- mode: python ; coding: utf-8 -*-
# Build on Windows: double-click build_windows.bat
#   or: pyinstaller --noconfirm Img2PDF.spec

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
# cv2/numpy back the document scanner; PyInstaller's bundled hooks pull in their
# binaries, they just need to be named because the import is inside a try block.
hiddenimports = ["PIL._tkinter_finder", "cv2", "numpy"]

for package in ("customtkinter", "pypdfium2", "pypdfium2_raw"):
    # pypdfium2 ships the PDFium shared library as package data, so a plain
    # hiddenimport is not enough — the binary has to come along too.
    tmp_ret = collect_all(package)
    datas += tmp_ret[0]
    binaries += tmp_ret[1]
    hiddenimports += tmp_ret[2]

# Bundle the document model when it is present, so the .exe needs no download.
_model = Path("models/u2netp.onnx")
if _model.is_file():
    datas += [(str(_model), ".")]

a = Analysis(
    ["run_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Img2PDF",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # no terminal / black console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
