# -*- mode: python ; coding: utf-8 -*-
# onedir 模式：不再每次启动解压 30MB 到临时目录，显著缩短启动时间。
# 交付形态为 dist/护眼小助手/ 文件夹，桌面放快捷方式指向其中的 exe。

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('web/dist', 'web/dist'), ('icon.ico', '.')],
    hiddenimports=['webview.platforms.edgechromium', 'webview.platforms.winforms'],
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
    [],
    exclude_binaries=True,
    name='护眼小助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='护眼小助手',
)
