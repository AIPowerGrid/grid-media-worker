# -*- mode: python ; coding: utf-8 -*-
import uv

from PyInstaller.utils.hooks import collect_data_files

datas = []
datas += collect_data_files('bridge')
binaries = [(uv.find_uv_bin(), 'uv/bin')]


a = Analysis(
    ['manager_entry.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=['bridge.ws_worker'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', '_tkinter'],
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
    name='grid-media-manager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
