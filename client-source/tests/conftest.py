from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from config.settings import (
    AuthorSettings,
    DatabaseSettings,
    DownloadSettings,
    FilterSettings,
    GroupSettings,
    MapDetectionSettings,
    NotifySettings,
    Settings,
)


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    return Settings(
        groups=[GroupSettings(group_id=10001, name="测试地图群")],
        authors=[AuthorSettings(qq=20002, name="测试作者")],
        filter=FilterSettings(
            scan_all_joined_groups=False,
            require_whitelisted_author=True,
            require_known_filename_before_download=False,
        ),
        download=DownloadSettings(
            temp_directory=tmp_path / "temp",
            archive_directory=tmp_path / "archive",
            latest_directory=tmp_path / "latest",
            incoming_directory=tmp_path / "incoming",
            retries=2,
        ),
        database=DatabaseSettings(
            url=f"sqlite+aiosqlite:///{(tmp_path / 'maps.db').as_posix()}"
        ),
        map_detection=MapDetectionSettings(strict=False),
        notify=NotifySettings(enabled=False),
    )


def write_map_zip(path: Path, *, payload: str = "map") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("OC2DIYLevel/level.json", '{"name":"test"}')
        archive.writestr("OC2DIYLevel/data.bin", payload)
    return path
