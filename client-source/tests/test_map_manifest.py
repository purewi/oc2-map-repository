from __future__ import annotations

import zipfile
from pathlib import Path

from publisher.map_manifest import read_map_manifest


def test_non_unity_info_file_keeps_internal_key_fallback(tmp_path: Path) -> None:
    archive_path = tmp_path / "map.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("stable/info_stable", b"not a Unity bundle")

    manifest = read_map_manifest(archive_path, include_screenshots=True)

    assert manifest.map_key == "stable"
    assert manifest.uid is None
    assert manifest.author is None
    assert manifest.screenshots == ()
    assert manifest.level_scenes == ()
