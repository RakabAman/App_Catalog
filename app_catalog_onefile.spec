# -*- mode: python ; coding: utf-8 -*-
"""
app_catalog_onefile.spec -- PyInstaller onefile build for the App Catalog GUI.

Usage (from the project root, with ico.ico and run_gui.py alongside this file):
    pyinstaller --clean app_catalog_onefile.spec

Output:
    dist/AppCatalog.exe       <- single self-contained executable

console=True keeps the console window attached. app_paths.py still anchors
catalog.db / logs/ / manifest/ to the folder containing this .exe (not
PyInstaller's _MEIPASS temp folder), so the .exe's own folder is the app's
data folder -- the temp extraction is a runtime detail, not storage.
"""

block_cipher = None

hiddenimports = [
    "pefile",
    "py7zr",
    "rarfile",
    "pycdlib",
    "rapidfuzz",
    "rapidfuzz.fuzz",
    "rapidfuzz.string_metric",
    "requests",
    "charset_normalizer",
    "bs4",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]

excludes = [
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "IPython",
    "jupyter",
    "notebook",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "pytest",
]

a = Analysis(
    ["run_gui.py"],
    pathex=[],
    binaries=[],
    datas=[
        # Extracted to _MEIPASS on each launch, where run_gui.py finds it
        # via app_paths.get_resource_path("ico.ico"). The icon= below is
        # what stamps the .exe file itself -- separate concern.
        ("ico.ico", "."),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="AppCatalog",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,                    # <-- was False. Console window kept.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="ico.ico",
)