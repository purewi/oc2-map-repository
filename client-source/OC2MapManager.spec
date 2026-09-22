# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    get_package_paths,
)


_, shiboken_package = get_package_paths("shiboken6")
build_variant = os.environ.get("OC2_CLIENT_BUILD_VARIANT", "cos").casefold()
is_public = build_variant == "public"
entry_script = "client_main_public.py" if is_public else "client_main.py"
executable_name = "OC2MapManager-Public" if is_public else "OC2MapManager-COS-VIP"

a = Analysis(
    [entry_script],
    pathex=[],
    # PyInstaller 6.22 + Python 3.14 places this DLL beside Shiboken.pyd,
    # while PySide6's Qt*.pyd resolves it from the PySide6 DLL directory.
    binaries=[(str(Path(shiboken_package) / "shiboken6.abi3.dll"), "PySide6")]
    + collect_dynamic_libs("fmod_toolkit"),
    datas=collect_data_files("UnityPy") + collect_data_files("archspec") + [
        ("assets/kitchen-updater.ico", "assets"),
        ("proxy_sources.json", "."),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["boto3", "botocore", "nonebot", "sqlalchemy", "pytest", "_pytest",
              "pygments", "PIL.AvifImagePlugin", "PIL._avif", "PIL.ImageQt"],
    noarchive=False,
    optimize=0,
)

# The Codex desktop process exposes a Poppler ICU build on PATH. Qt on Windows
# intentionally links to the Windows system ICU forwarder; bundling Poppler's
# same-named DLL causes QtWidgets to fail with ERROR_PROC_NOT_FOUND.
_foreign_icu = {"icuuc.dll", "icudt78.dll"}
a.binaries = [item for item in a.binaries if Path(item[0]).name.casefold() not in _foreign_icu]
# This is a raster Qt Widgets app: no QML/Quick, PDF rendering, or software
# OpenGL renderer is used. Keep Windows/offscreen platforms and raster images.
def client_binary(item):
    destination = item[0].replace("\\", "/").casefold()
    basename = destination.rsplit("/", 1)[-1]
    if basename == "opengl32sw.dll":
        return False
    if basename.startswith("qt6") and basename.endswith(".dll"):
        return basename in {"qt6core.dll", "qt6gui.dll", "qt6widgets.dll",
                            "qt6network.dll", "qt6svg.dll"}
    if "/plugins/" in destination:
        if "/platforms/" in destination:
            return basename in {"qwindows.dll", "qoffscreen.dll", "qminimal.dll"}
        if "/imageformats/" in destination:
            return basename in {"qjpeg.dll", "qwebp.dll", "qgif.dll", "qico.dll", "qsvg.dll"}
        return any(f"/{category}/" in destination for category in
                   ("styles", "iconengines", "tls", "networkinformation"))
    return True

a.binaries = [item for item in a.binaries if client_binary(item)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=executable_name,
    icon="assets/kitchen-updater.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    exclude_binaries=False,
    codesign_identity=None,
    entitlements_file=None,
)
