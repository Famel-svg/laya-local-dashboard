# -*- mode: python ; coding: utf-8 -*-
# Build (venv fora do OneDrive): ver build.ps1

ROOT = SPECPATH.replace('\\', '/')  # pasta deste arquivo

a = Analysis(
    [ROOT + '/laya_app.py'],
    pathex=[ROOT],
    binaries=[],
    datas=[(ROOT + '/laya_tray.ico', '.'), (ROOT + '/laya_startup.vbs', '.')],
    hiddenimports=['startup'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtPdf', 'PySide6.QtOpenGL',
              'PySide6.QtSvg', 'PySide6.QtDBus', 'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtXml'],
    noarchive=False,
    optimize=0,
)

# DLLs do Qt que o app não usa (renderizador por software, QML, PDF, tradução).
_DROP = ('opengl32sw', 'qt6quick', 'qt6qml', 'qt6pdf', 'qt6opengl', 'qt6svg', 'qt6virtualkeyboard',
         'qt6dbus', 'd3dcompiler', 'qtwebengine')
a.binaries = [b for b in a.binaries if not any(k in b[0].lower() for k in _DROP)]
a.datas = [d for d in a.datas if '/translations/' not in d[0].replace('\\', '/').lower()]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LayaLocal',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ROOT + '/laya_tray.ico',
)
