from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from client.install_service import (
    InstallError, InstallService, SupportInstallService, safe_extract_map,
)
from publisher.models import MapMetadata


def metadata() -> MapMetadata:
    return MapMetadata(
        id="wenr-123456789abc",
        name="WenR",
        author="Chef",
        version="0.6",
        description="",
        preview_url="https://example/preview.webp",
        download_url="https://example/map.zip",
        file_name="WenR_v0.6.zip",
        size=1,
        sha256="a" * 64,
        updated_at="2026-09-05T00:00:00Z",
        map_key="wenr",
    )


def write_map(path: Path, *, unsafe: bool = False) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("WenR/info_wenr", b"info")
        archive.writestr("WenR/s_level_1", b"scene")
        if unsafe:
            archive.writestr("WenR/../../escape.txt", b"bad")


def test_safe_extract_rejects_zip_slip(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    write_map(archive, unsafe=True)
    with pytest.raises(InstallError, match="不安全路径"):
        safe_extract_map(archive, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_install_and_update_replace_without_duplicate(tmp_path: Path) -> None:
    maps = tmp_path / "levels"
    cache = tmp_path / "cache"
    archive = tmp_path / "map.zip"
    write_map(archive)
    service = InstallService(maps, cache)
    first = service.install(metadata(), archive)
    assert (first.install_path / "info_wenr").is_file()

    with zipfile.ZipFile(archive, "w") as updated:
        updated.writestr("WenR/info_wenr", b"new")
        updated.writestr("WenR/s_level_1", b"new scene")
    second = service.install(metadata(), archive, existing_path=first.install_path)
    assert second.replaced_existing
    assert (second.install_path / "info_wenr").read_bytes() == b"new"
    assert len([item for item in maps.iterdir() if not item.name.startswith(".")]) == 1


def test_update_failure_restores_old_map(tmp_path: Path, monkeypatch) -> None:
    maps = tmp_path / "levels"
    cache = tmp_path / "cache"
    archive = tmp_path / "map.zip"
    write_map(archive)
    service = InstallService(maps, cache)
    first = service.install(metadata(), archive)
    old_content = (first.install_path / "info_wenr").read_bytes()

    import os

    real_replace = os.replace

    def fail_activation(source, destination):
        if Path(source).name.startswith(".oc2_new_"):
            raise OSError("activation failed")
        return real_replace(source, destination)

    monkeypatch.setattr("client.install_service.os.replace", fail_activation)
    with pytest.raises(InstallError, match="已尝试回滚"):
        service.install(metadata(), archive, existing_path=first.install_path)
    assert (first.install_path / "info_wenr").read_bytes() == old_content


def test_support_files_install_only_into_framework_directory(tmp_path: Path) -> None:
    game = tmp_path / "Overcooked! 2"
    plugin = game / "BepInEx" / "plugins" / "OC2DIYLevel"
    plugin.mkdir(parents=True)
    (plugin / "OC2DIYLevel.dll").write_bytes(b"framework")
    archive = tmp_path / "support.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("payload/commonW1", b"common")
        output.writestr("payload/OC2LevelRuntimeLoader.dll", b"runtime")
        output.writestr(
            "support-manifest.json",
            '{"schemaVersion":1,"installTarget":"OC2DIYLevel","files":['
            '{"name":"commonW1","size":6},'
            '{"name":"OC2LevelRuntimeLoader.dll","size":7}]}',
        )
    installed = SupportInstallService(game, tmp_path / "cache").install(
        archive, "dmm-example"
    )
    assert {item.name for item in installed} == {"commonW1", "OC2LevelRuntimeLoader.dll"}
    assert (plugin / "commonW1").read_bytes() == b"common"
    assert not (game / "commonW1").exists()


def test_identical_support_files_are_not_redeployed(tmp_path: Path) -> None:
    game = tmp_path / "Overcooked! 2"
    plugin = game / "BepInEx" / "plugins" / "OC2DIYLevel"
    plugin.mkdir(parents=True)
    (plugin / "OC2DIYLevel.dll").write_bytes(b"framework")
    (plugin / "commonW1").write_bytes(b"same content")
    archive = tmp_path / "support.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("payload/commonW1", b"same content")
        output.writestr(
            "support-manifest.json",
            '{"schemaVersion":1,"installTarget":"OC2DIYLevel","files":['
            '{"name":"commonW1","size":12}]}',
        )
    assert SupportInstallService(game, tmp_path / "cache").install(
        archive, "shared-support"
    ) == ()


def test_profiled_support_installs_new_folder_and_cleans_legacy_files(tmp_path: Path) -> None:
    game = tmp_path / "Overcooked! 2"
    plugin = game / "BepInEx" / "plugins" / "OC2DIYLevel"
    levels = plugin / "levels"
    dmm = levels / "Dmm"
    dmm.mkdir(parents=True)
    (plugin / "OC2DIYLevel.dll").write_bytes(b"framework")
    (plugin / "commonW1").write_bytes(b"old common")
    (plugin / "commonW2").write_bytes(b"old common")
    (plugin / "Loader.dll").write_bytes(b"old loader")
    (plugin / "OC2LevelRuntimeLoader.dll").write_bytes(b"old runtime")
    (dmm / "info_dmm").write_bytes(b"info")
    (dmm / "s_dmm_1").write_bytes(b"level")
    (dmm / "runtime").write_bytes(b"obsolete")
    archive = tmp_path / "support.zip"
    layout = {
        "schemaVersion": 1,
        "defaultProfile": "unified",
        "profiles": {
            "unified": {
                "files": [
                    {"source": "commonW1", "target": "OC2DIYLevelRuntimeWLoader/commonW1"},
                    {"source": "Loader.dll", "target": "OC2DIYLevelRuntimeWLoader/Loader.dll"},
                    {"source": "webcustomstub_runtime.dat", "target": "OC2DIYLevelRuntimeWLoader/webcustomstub_runtime"},
                ],
                "cleanup": [
                    "OC2DIYLevelRuntimeWLoader",
                    "OC2DIYLevel/commonW1",
                    "OC2DIYLevel/commonW2",
                    "OC2DIYLevel/Loader.dll",
                    "OC2DIYLevel/OC2LevelRuntimeLoader.dll",
                ],
                "mapCleanup": [{"mapKeys": ["dmm"], "files": ["runtime"]}],
            },
            "qichi": {
                "files": [
                    {"source": "QiChiRuntime.dll", "target": "OC2DIYLevel/QiChiRuntime.dll"}
                ]
            },
            "jonny": {
                "files": [
                    {"source": "JonnyLevelLoaderPlugin.dll", "target": "JonnyLevelLoaderPlugin.dll"},
                    {"source": "JonnyLevelScripts.dll", "target": "JonnyLevelScripts.dll"},
                ],
            },
        },
    }
    payload = {
        "commonW1": b"new common",
        "Loader.dll": b"new loader",
        "webcustomstub_runtime.dat": b"web runtime",
        "QiChiRuntime.dll": b"qichi runtime",
        "JonnyLevelLoaderPlugin.dll": b"jonny loader",
        "JonnyLevelScripts.dll": b"jonny scripts",
        "support-layout.json": __import__("json").dumps(layout).encode(),
    }
    manifest = {
        "schemaVersion": 1,
        "installTarget": "OC2DIYLevel",
        "files": [{"name": name, "size": len(content)} for name, content in payload.items()],
    }
    with zipfile.ZipFile(archive, "w") as output:
        for name, content in payload.items():
            output.writestr(f"payload/{name}", content)
        output.writestr("support-manifest.json", __import__("json").dumps(manifest))

    SupportInstallService(game, tmp_path / "cache").install(
        archive, "dmm-example", profile="unified"
    )
    new_folder = game / "BepInEx" / "plugins" / "OC2DIYLevelRuntimeWLoader"
    assert (new_folder / "commonW1").read_bytes() == b"new common"
    assert (new_folder / "Loader.dll").read_bytes() == b"new loader"
    assert (new_folder / "webcustomstub_runtime").read_bytes() == b"web runtime"
    assert not (plugin / "commonW1").exists()
    assert not (plugin / "commonW2").exists()
    assert not (plugin / "Loader.dll").exists()
    assert not (plugin / "OC2LevelRuntimeLoader.dll").exists()
    assert not (dmm / "runtime").exists()
    assert not (plugin / "QiChiRuntime.dll").exists()

    SupportInstallService(game, tmp_path / "cache").install(
        archive, "qichi-example", profile="qichi"
    )
    assert (plugin / "QiChiRuntime.dll").read_bytes() == b"qichi runtime"

    (plugin / "JonnyLevelLoaderPlugin.dll").write_bytes(b"wrong legacy loader")
    (plugin / "JonnyLevelScripts.dll").write_bytes(b"wrong legacy scripts")
    SupportInstallService(game, tmp_path / "cache").install(
        archive, "unified-support", profile=("unified", "jonny")
    )
    plugins = game / "BepInEx" / "plugins"
    assert (plugins / "JonnyLevelLoaderPlugin.dll").read_bytes() == b"jonny loader"
    assert (plugins / "JonnyLevelScripts.dll").read_bytes() == b"jonny scripts"
    assert not (plugin / "JonnyLevelLoaderPlugin.dll").exists()
    assert not (plugin / "JonnyLevelScripts.dll").exists()


def test_map_install_can_skip_known_obsolete_runtime(tmp_path: Path) -> None:
    archive = tmp_path / "dmm.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("Dmm/info_dmm", b"info")
        output.writestr("Dmm/s_dmm_1", b"level")
        output.writestr("Dmm/runtime", b"old prerequisite")
        output.writestr("Dmm/requires.txt", b"keep")
    metadata_value = metadata().model_copy(update={"map_key": "dmm"})
    result = InstallService(tmp_path / "levels", tmp_path / "cache").install(
        metadata_value,
        archive,
        obsolete_files=("runtime",),
    )
    assert not (result.install_path / "runtime").exists()
    assert (result.install_path / "requires.txt").read_bytes() == b"keep"
