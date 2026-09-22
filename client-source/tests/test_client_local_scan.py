from __future__ import annotations

from pathlib import Path

from client.config import ClientConfig
from client.map_manager import MapManager
from client.models import MapStatus
from publisher.models import Catalog, InstalledMap, MapMetadata
from publisher.map_manifest import MapManifest


def test_unmanaged_map_directory_is_detected_and_matched(
    tmp_path: Path, monkeypatch
) -> None:
    levels = tmp_path / "levels"
    local = levels / "existing-map"
    local.mkdir(parents=True)
    (local / "info_my-map").write_text("info", encoding="utf-8")
    (local / "s_stage").write_text("stage", encoding="utf-8")
    config = ClientConfig(cache_directory=tmp_path / "cache", maps_directory=levels)
    monkeypatch.setattr(
        "client.map_manager.read_info_manifest",
        lambda _: MapManifest(map_key="my-map", version="2.0"),
    )
    manager = MapManager(config)
    manager.catalog = Catalog(
        maps=[
            MapMetadata(
                id="map-123456789abc",
                name="Cloud map",
                author="Chef",
                version="2.0",
                preview_url="https://example.test/preview.webp",
                download_url="https://example.test/map.zip",
                file_name="map.zip",
                size=3,
                sha256="a" * 64,
                updated_at="2026-09-05T00:00:00Z",
                map_key="my-map",
            )
        ]
    )
    entry = manager.entries()[0]
    assert entry.status == MapStatus.LATEST
    assert entry.local_version == "2.0"
    assert entry.install_path == local.resolve()


def test_framework_builtin_maps_are_hidden(tmp_path: Path) -> None:
    levels = tmp_path / "levels"
    for key in ("oc1_story", "test_level"):
        local = levels / key
        local.mkdir(parents=True)
        (local / f"info_{key}").write_bytes(b"info")
        (local / f"s_{key}").write_bytes(b"stage")
    manager = MapManager(
        ClientConfig(cache_directory=tmp_path / "cache", maps_directory=levels)
    )
    manager.state.state.installed_maps.clear()

    assert manager.entries() == []


def test_compact_patch_local_version_is_older_than_new_minor(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    levels = tmp_path / "levels"
    local = levels / "hhm"
    local.mkdir(parents=True)
    (local / "info_hhm").write_text("info", encoding="utf-8")
    (local / "s_stage").write_text("stage", encoding="utf-8")
    monkeypatch.setattr(
        "client.map_manager.read_info_manifest",
        lambda _: MapManifest(map_key="hhm", version="0.162"),
    )
    manager = MapManager(
        ClientConfig(cache_directory=tmp_path / "cache", maps_directory=levels)
    )
    manager.catalog = Catalog(
        maps=[
            MapMetadata(
                id="hhm-8722d509c385",
                name="胡乱盛宴",
                author="呼呼喵",
                version="0.17",
                source_version="0.17",
                embedded_version="0.1",
                preview_url="",
                download_url="https://example.test/hhm.zip",
                file_name="hhm 0.17.zip",
                size=3,
                sha256="a" * 64,
                updated_at="2026-09-11T00:00:00Z",
                map_key="hhm",
            )
        ]
    )

    entry = manager.entries()[0]
    assert entry.local_version == "0.162"
    assert entry.version == "0.17"
    assert entry.status == MapStatus.UPDATE_AVAILABLE


def test_managed_legacy_release_number_cannot_be_reported_as_local_newer(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    levels = tmp_path / "levels"
    local = levels / "xia"
    local.mkdir(parents=True)
    manager = MapManager(
        ClientConfig(cache_directory=tmp_path / "cache", maps_directory=levels)
    )
    manager.state.set(
        "xia-example",
        InstalledMap(
            version="2026.09.08.1",
            map_version="0.1",
            sha256="a" * 64,
            installed_at="2026-09-09T00:00:00Z",
            install_path=str(local),
        ),
    )
    manager.catalog = Catalog(
        maps=[
            MapMetadata(
                id="xia-example", name="夏", author="不辑", version="0.17",
                source_version="0.17", embedded_version="0.1",
                download_url="https://example.test/xia.zip", file_name="不辑的零食铺0.17.zip",
                size=3, sha256="b" * 64, updated_at="2026-09-11T00:00:00Z",
                map_key="xia",
            )
        ]
    )

    entry = manager.entries()[0]
    assert entry.version == "0.17"
    assert entry.local_version == "0.1"
    assert entry.status == MapStatus.UPDATE_AVAILABLE


def test_matching_managed_hash_uses_cloud_package_version_for_local_display(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    levels = tmp_path / "levels"
    local = levels / "qichi"
    local.mkdir(parents=True)
    manager = MapManager(
        ClientConfig(cache_directory=tmp_path / "cache", maps_directory=levels)
    )
    manager.state.set(
        "qichi-example",
        InstalledMap(
            version="0.7.3", map_version="0.7.2", sha256="a" * 64,
            installed_at="2026-09-11T00:00:00Z", install_path=str(local),
        ),
    )
    manager.catalog = Catalog(
        maps=[
            MapMetadata(
                id="qichi-example", name="七池的地图系列", author="七池",
                version="0.7.3", source_version="0.7.3", embedded_version="0.7.2",
                download_url="https://example.test/qichi.zip", file_name="七池.v0.7.3.zip",
                size=3, sha256="a" * 64, updated_at="2026-09-11T00:00:00Z",
                map_key="qichi",
            )
        ]
    )

    entry = manager.entries()[0]
    assert entry.local_version == "0.7.3"
    assert entry.status == MapStatus.LATEST
