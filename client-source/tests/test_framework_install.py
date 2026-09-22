from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from client.framework_service import (
    detect_framework_version,
    FrameworkInstallError,
    FrameworkInstallService,
    safe_extract_framework,
)


def write_framework(path: Path, *, unsafe: bool = False) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("OC2DIYLevel/OC2DIYLevel.dll", b"new framework")
        archive.writestr("OC2DIYLevel/LevelEditorStub.dll", b"stub")
        archive.writestr("OC2DIYLevel/levels/example/info_example", b"info")
        if unsafe:
            archive.writestr("OC2DIYLevel/../../escape.exe", b"bad")


def test_framework_install_preserves_existing_maps_and_saves(tmp_path: Path) -> None:
    game = tmp_path / "Overcooked! 2"
    existing = game / "BepInEx" / "plugins" / "OC2DIYLevel"
    (existing / "levels" / "my_map").mkdir(parents=True)
    (existing / "levels" / "my_map" / "info_my_map").write_bytes(b"mine")
    (existing / "savefiles").mkdir()
    (existing / "savefiles" / "progress.dat").write_bytes(b"save")
    (existing / "OC2DIYLevel.dll").write_bytes(b"old")
    archive = tmp_path / "framework.zip"
    write_framework(archive)

    target = FrameworkInstallService(game).install(archive)

    assert (target / "OC2DIYLevel.dll").read_bytes() == b"new framework"
    assert (target / "levels" / "my_map" / "info_my_map").read_bytes() == b"mine"
    assert (target / "savefiles" / "progress.dat").read_bytes() == b"save"


def test_framework_install_preserves_third_party_support_files(tmp_path: Path) -> None:
    game = tmp_path / "Overcooked! 2"
    target = game / "BepInEx" / "plugins" / "OC2DIYLevel"
    target.mkdir(parents=True)
    (target / "OC2DIYLevel.dll").write_bytes(b"old framework")
    (target / "commonW1").write_bytes(b"custom common")
    (target / "OC2LevelRuntimeLoader.dll").write_bytes(b"custom runtime")
    (target / "qichi_assets.qichi").write_bytes(b"custom assets")
    archive = tmp_path / "framework.zip"
    write_framework(archive)

    FrameworkInstallService(game).install(archive, version="0.10")

    assert (target / "OC2DIYLevel.dll").read_bytes() == b"new framework"
    assert (target / "commonW1").read_bytes() == b"custom common"
    assert (target / "OC2LevelRuntimeLoader.dll").read_bytes() == b"custom runtime"
    assert (target / "qichi_assets.qichi").read_bytes() == b"custom assets"


def test_framework_cleanup_failure_does_not_rollback_valid_update(
    tmp_path: Path, monkeypatch
) -> None:
    game = tmp_path / "Overcooked! 2"
    target = game / "BepInEx" / "plugins" / "OC2DIYLevel"
    target.mkdir(parents=True)
    (target / "OC2DIYLevel.dll").write_bytes(b"old framework")
    archive = tmp_path / "framework.zip"
    write_framework(archive)
    real_rmtree = shutil.rmtree

    def fail_previous(path, *args, **kwargs):
        if Path(path).name.startswith(".oc2_framework_previous_"):
            raise PermissionError("in use")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", fail_previous)

    result = FrameworkInstallService(game).install(archive, version="0.10")

    assert result == target
    assert (target / "OC2DIYLevel.dll").read_bytes() == b"new framework"


def test_framework_extract_rejects_zip_slip(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    write_framework(archive, unsafe=True)
    with pytest.raises(FrameworkInstallError, match="不安全路径"):
        safe_extract_framework(archive, tmp_path / "out")
    assert not (tmp_path / "escape.exe").exists()


def test_detect_framework_version_from_existing_plugin_metadata(tmp_path: Path) -> None:
    plugin = tmp_path / "BepInEx" / "plugins" / "OC2DIYLevel"
    plugin.mkdir(parents=True)
    (plugin / "OC2DIYLevel.dll").write_bytes(
        b"dev.gua.overcooked.diylevel\0Overcooked2 DIYLevel Plugin\0"
        b"0.10.0\0"
    )
    assert detect_framework_version(tmp_path) == "0.10.0"


def test_framework_install_records_cloud_version(tmp_path: Path) -> None:
    game = tmp_path / "Overcooked! 2"
    archive = tmp_path / "framework.zip"
    write_framework(archive)
    FrameworkInstallService(game).install(
        archive, version="0.10", sha256="a" * 64
    )
    assert detect_framework_version(game) == "0.10"
