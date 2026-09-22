from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from publisher.models import MapMetadata


class MapStatus(StrEnum):
    LATEST = "已是最新版"
    UPDATE_AVAILABLE = "有更新"
    NOT_INSTALLED = "未安装"
    DOWNLOADING = "下载中"
    INSTALLING = "安装中"
    FAILED = "失败"
    LOCAL_ONLY = "本地地图 / 云端不存在"
    LOCAL_NEWER = "本地版本较新"
    AUTHOR_UPDATE = "旧版可下载 · 新版请找作者"


@dataclass(slots=True)
class MapEntry:
    id: str
    name: str
    author: str
    version: str
    local_version: str
    size: int
    updated_at: str
    status: MapStatus
    metadata: MapMetadata | None
    install_path: Path | None = None
    selected: bool = False


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    map_id: str
    downloaded: int
    total: int
    bytes_per_second: float

    @property
    def percent(self) -> int:
        return int(self.downloaded * 100 / self.total) if self.total > 0 else 0
