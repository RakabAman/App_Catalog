# -*- mode: python ; coding: utf-8 -*-
"""
app_catalog_onedir.spec -- PyInstaller onedir build for the App Catalog GUI.

Usage (from the project root, with ico.ico and run_gui.py alongside this file):
    pyinstaller --clean app_catalog_onedir.spec

Output:
    dist/AppCatalog/
        AppCatalog.exe
        _internal/
            ico.ico
            ...PyInstaller-collected dependencies...

console=True keeps the console window attached -- run_gui.py's logging
already sends to both console and logs/app.log, so a live console is a
useful second view of what a long scan/resolve is doing.
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
        # Bundled so run_gui.py can load it at runtime via
        # app_paths.get_resource_path("ico.ico") for the window/taskbar
        # icon. The icon= parameter below only stamps the .exe file.
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
    [],
    exclude_binaries=True,
    name="AppCatalog",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                    # <-- was False. Console window kept.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="ico.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AppCatalog",
)