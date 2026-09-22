from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class MapVersion(BaseModel):
    """One immutable published map archive."""

    model_config = ConfigDict(extra="ignore")

    version: str = Field(min_length=1, max_length=80)
    updated_at: datetime
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    file_name: str = Field(min_length=1, max_length=512)
    source_version: str | None = Field(default=None, max_length=80)
    download_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("download_url", "downloadUrl"),
        serialization_alias="downloadUrl",
    )
    cos_url: str | None = None
    preview_url: str | None = None
    thumbnail_url: str | None = None


class SupportPackage(BaseModel):
    """Extra runtime files required by one map series."""

    model_config = ConfigDict(extra="ignore")

    version: str = Field(min_length=1, max_length=80)
    download_url: str = Field(
        validation_alias=AliasChoices("download_url", "downloadUrl"),
        serialization_alias="downloadUrl",
    )
    file_name: str = Field(min_length=1, max_length=512)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: list[str] = Field(default_factory=list)
    required: bool = True
    notice: str = "此地图需要额外支持文件才能游玩"


class MapMetadata(BaseModel):
    """Catalog entry and per-map metadata document.

    Required fields intentionally match catalog schema v1. Optional fields can be
    added without forcing older desktop clients to understand them.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{5,79}$")
    name: str = Field(min_length=1, max_length=255)
    author: str = Field(default="未知作者", max_length=255)
    version: str = Field(min_length=1, max_length=80)
    description: str = ""
    preview_url: str = ""
    thumbnail_url: str | None = None
    download_url: str = Field(
        validation_alias=AliasChoices("download_url", "downloadUrl"),
        serialization_alias="downloadUrl",
    )
    cos_url: str | None = None
    file_name: str = Field(min_length=1, max_length=512)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    updated_at: datetime
    tags: list[str] = Field(default_factory=list)
    source: dict[str, Any] | None = None
    player_count: str | None = None
    difficulty: str | None = None
    category: str | None = None
    changelog: str | None = None
    source_version: str | None = None
    map_key: str | None = Field(default=None, max_length=100)
    map_uid: str | None = Field(default=None, max_length=255)
    embedded_version: str | None = Field(default=None, max_length=80)
    level_count: int | None = Field(default=None, ge=0)
    versions: list[MapVersion] = Field(default_factory=list)
    disabled: bool = False
    author_latest_version: str | None = None
    availability_notice: str | None = None
    previous_map_keys: list[str] = Field(default_factory=list)
    previous_map_uids: list[str] = Field(default_factory=list)
    support_package: SupportPackage | None = None
    # A deliberately retained, known-working archive from before this map
    # started requiring external runtime files.
    compatibility_version: MapVersion | None = None
    # Used when the author says support is required but the support files have
    # not yet been supplied/published.  The client must not silently install
    # the current map in that state.
    support_required_notice: str | None = None

    @field_validator("tags")
    @classmethod
    def unique_tags(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    def catalog_view(self) -> "MapMetadata":
        # Source is operational state as well as audit metadata.  In
        # particular, retaining ``manual`` lets a restarted bot reject group
        # files older than the last administrator checkpoint.
        return self.model_copy(update={"disabled": False})


class ClientRelease(BaseModel):
    model_config = ConfigDict(extra="ignore")

    latest_version: str | None = None
    minimum_version: str | None = None
    download_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("download_url", "downloadUrl"),
        serialization_alias="downloadUrl",
    )
    size: int | None = Field(default=None, ge=1)
    mandatory: bool = False
    release_notes: list[str] | str = Field(default_factory=list)
    published_at: datetime | None = None
    variants: dict[str, "ClientUpdateAsset"] = Field(default_factory=dict)

    def asset_for(self, variant: str) -> "ClientUpdateAsset | None":
        selected = self.variants.get(variant.strip().casefold())
        if selected is not None:
            return selected
        if self.download_url and self.size:
            return ClientUpdateAsset(
                download_url=self.download_url,
                size=self.size,
                file_name="OC2MapManager.exe",
            )
        return None

    def notes(self) -> list[str]:
        if isinstance(self.release_notes, str):
            return [self.release_notes.strip()] if self.release_notes.strip() else []
        return [str(item).strip() for item in self.release_notes if str(item).strip()]


class ClientUpdateAsset(BaseModel):
    model_config = ConfigDict(extra="ignore")

    download_url: str = Field(
        validation_alias=AliasChoices("download_url", "downloadUrl"),
        serialization_alias="downloadUrl",
    )
    size: int = Field(ge=1)
    file_name: str = Field(min_length=1, max_length=512)
    cos_url: str | None = None


class FrameworkRelease(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str = Field(min_length=1, max_length=80)
    download_url: str = Field(
        validation_alias=AliasChoices("download_url", "downloadUrl"),
        serialization_alias="downloadUrl",
    )
    file_name: str = Field(min_length=1, max_length=512)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    updated_at: datetime


class Catalog(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int = Field(default=1, ge=1)
    generated_at: datetime = Field(default_factory=utc_now)
    maps: list[MapMetadata] = Field(default_factory=list)
    client: ClientRelease | None = None
    framework: FrameworkRelease | None = None
    support_bundle: SupportPackage | None = None

    @field_validator("maps")
    @classmethod
    def unique_map_ids(cls, maps: list[MapMetadata]) -> list[MapMetadata]:
        ids = [item.id for item in maps]
        if len(ids) != len(set(ids)):
            raise ValueError("catalog maps 中存在重复 id")
        return maps

    def find(self, map_id: str) -> MapMetadata | None:
        return next((item for item in self.maps if item.id == map_id), None)

    def upsert(self, metadata: MapMetadata) -> None:
        visible = metadata.catalog_view()
        self.maps = [item for item in self.maps if item.id != metadata.id]
        if not metadata.disabled:
            self.maps.append(visible)
        self.maps.sort(key=lambda item: (item.name.casefold(), item.id))
        self.generated_at = utc_now()

    def remove(self, map_id: str) -> MapMetadata | None:
        existing = self.find(map_id)
        self.maps = [item for item in self.maps if item.id != map_id]
        if existing is not None:
            self.generated_at = utc_now()
        return existing


class InstalledMap(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str
    map_version: str | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installed_at: datetime
    install_path: str


class UpdaterState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    installed_maps: dict[str, InstalledMap] = Field(default_factory=dict)
    installed_framework: InstalledMap | None = None
