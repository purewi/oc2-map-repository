from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from publisher.models import Catalog, InstalledMap, MapMetadata
from publisher.map_manifest import read_info_manifest
from publisher.versioning import compare_versions

from .catalog_service import CatalogService
from .config import ClientConfig
from .download_manager import DownloadManager
from .framework_service import FrameworkInstallService
from .install_service import InstallService, SupportInstallService
from .local_state import LocalStateStore
from .models import DownloadProgress, MapEntry, MapStatus
from .variant import current_variant


_FRAMEWORK_BUILTIN_MAP_KEYS = {"oc1_story", "test_level"}


@dataclass(frozen=True, slots=True)
class LocalMap:
    path: Path
    version: str | None


@dataclass(frozen=True, slots=True)
class FrameworkStatus:
    local_version: str | None
    cloud_version: str | None
    installed: bool
    matches: bool
    local_newer: bool


class MapManager:
    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self.state = LocalStateStore(config.state_file)
        self.state.load()
        self.catalog_service: CatalogService | None = None
        self.downloads = DownloadManager(
            config.cache_directory,
            timeout_seconds=config.timeout_seconds,
            retries=config.retries,
            cos_enabled=current_variant().cos_enabled,
        )
        self.catalog = Catalog()

    def close(self) -> None:
        if self.catalog_service is not None:
            self.catalog_service.close()

    def refresh(self) -> tuple[list[MapEntry], bool]:
        if not self.config.cloud_repository:
            raise ValueError("客户端尚未配置地图云端仓库")
        if self.catalog_service is not None:
            self.catalog_service.close()
        self.catalog_service = CatalogService(self.config)
        self.catalog, cached = self.catalog_service.refresh()
        return self.entries(), cached

    def entries(self) -> list[MapEntry]:
        discovered = self._scan_local_maps()
        entries = [self._entry(metadata, discovered) for metadata in self.catalog.maps]
        catalog_ids = {item.id for item in self.catalog.maps}
        managed_paths: set[Path] = set()
        for map_id, installed in self.state.state.installed_maps.items():
            path = Path(installed.install_path)
            if path.is_dir():
                managed_paths.add(path.resolve())
            if map_id not in catalog_ids and path.is_dir():
                entries.append(
                    MapEntry(
                        id=map_id,
                        name=path.name,
                        author="本地",
                        version="-",
                        local_version=installed.version,
                        size=0,
                        updated_at="-",
                        status=MapStatus.LOCAL_ONLY,
                        metadata=None,
                        install_path=path,
                    )
                )
        catalog_keys = {
            item.map_key.casefold()
            for item in self.catalog.maps
            if item.map_key
        }
        catalog_keys.update(key for item in self.catalog.maps for key in item.previous_map_keys)
        for map_key, path in discovered.items():
            if path.path.resolve() in managed_paths or map_key in catalog_keys:
                continue
            entries.append(
                MapEntry(
                    id=f"local-{map_key}",
                    name=path.path.name,
                    author="本地",
                    version="-",
                    local_version=path.version or "未知",
                    size=0,
                    updated_at="-",
                    status=MapStatus.LOCAL_ONLY,
                    metadata=None,
                    install_path=path.path,
                )
            )
        return entries

    def install(
        self,
        metadata: MapMetadata,
        *,
        progress: Callable[[DownloadProgress], None] | None = None,
        phase: Callable[[str], None] | None = None,
    ) -> Path:
        if self.config.maps_directory is None:
            raise ValueError("请先设置游戏地图目录")
        if phase:
            phase("下载中")
        archive = self.downloads.download(metadata, progress)
        support = self._support_for(metadata)
        if support is not None:
            if self.config.game_directory is None:
                raise ValueError("此地图需要支持文件，请先设置 Overcooked! 2 游戏目录")
            support_profile = _support_profile(metadata)
            if phase:
                phase("下载地图前置文件")
            support_archive = self.downloads.download_asset(
                asset_id=f"oc2-support-{support_profile or 'legacy'}",
                version=support.version,
                url=support.download_url,
                size=support.size,
                sha256=support.sha256,
                progress=progress,
            )
            if phase:
                phase("安装地图前置文件")
            SupportInstallService(
                self.config.game_directory, self.config.cache_directory
            ).install(
                support_archive,
                metadata.id,
                profile=support_profile,
            )
        if phase:
            phase("安装中")
        installed = self.state.get(metadata.id)
        existing = Path(installed.install_path) if installed and Path(installed.install_path).is_dir() else None
        service = InstallService(self.config.maps_directory, self.config.cache_directory)
        result = service.install(
            metadata,
            archive,
            existing_path=existing,
            obsolete_files=_obsolete_map_files(metadata),
        )
        self.state.set(
            metadata.id,
            InstalledMap(
                version=metadata.version,
                map_version=_map_version(metadata),
                sha256=metadata.sha256,
                installed_at=datetime.now(UTC),
                install_path=str(result.install_path),
            ),
        )
        return result.install_path

    def download(
        self,
        metadata: MapMetadata,
        *,
        force: bool = False,
        progress: Callable[[DownloadProgress], None] | None = None,
    ) -> Path:
        archive = self.downloads.download(metadata, progress, force=force)
        support = self._support_for(metadata)
        if support is not None:
            support_profile = _support_profile(metadata)
            self.downloads.download_asset(
                asset_id=f"oc2-support-{support_profile or 'legacy'}",
                version=support.version,
                url=support.download_url,
                size=support.size,
                sha256=support.sha256,
                progress=progress,
                force=force,
            )
        return archive

    def install_support_bundle(
        self,
        *,
        progress: Callable[[DownloadProgress], None] | None = None,
        phase: Callable[[str], None] | None = None,
    ) -> tuple[Path, ...]:
        support = self.catalog.support_bundle
        if support is None:
            raise ValueError("云端暂未提供通用前置文件")
        if self.config.game_directory is None:
            raise ValueError("请先在设置中选择 Overcooked! 2 游戏目录")
        if phase:
            phase("正在下载通用前置文件")
        archive = self.downloads.download_asset(
            asset_id="oc2-unified-support",
            version=support.version,
            url=support.download_url,
            size=support.size,
            sha256=support.sha256,
            progress=progress,
        )
        if phase:
            phase("正在安装通用前置文件")
        return SupportInstallService(
            self.config.game_directory, self.config.cache_directory
        ).install(
            archive,
            "unified-support",
            profile=("unified", "jonny"),
        )

    def _support_for(self, metadata: MapMetadata):
        return metadata.support_package

    def install_framework(
        self,
        *,
        progress: Callable[[DownloadProgress], None] | None = None,
        phase: Callable[[str], None] | None = None,
    ) -> Path:
        framework = self.catalog.framework
        if framework is None:
            raise ValueError("云端暂未提供地图框架")
        if self.config.game_directory is None:
            raise ValueError("请先在设置中选择 Overcooked! 2 游戏目录")
        if phase:
            phase("正在下载地图框架")
        archive = self.downloads.download_asset(
            asset_id="oc2diylevel-framework",
            version=framework.version,
            url=framework.download_url,
            size=framework.size,
            sha256=framework.sha256,
            progress=progress,
        )
        if phase:
            phase("正在安装地图框架")
        target = FrameworkInstallService(self.config.game_directory).install(
            archive,
            version=framework.version,
            sha256=framework.sha256,
        )
        self.state.set_framework(
            InstalledMap(
                version=framework.version,
                sha256=framework.sha256,
                installed_at=datetime.now(UTC),
                install_path=str(target),
            )
        )
        return target

    def framework_status(self) -> FrameworkStatus:
        from .framework_service import detect_framework_version

        cloud = self.catalog.framework
        local_version = None
        installed = False
        if self.config.game_directory is not None:
            target = (
                self.config.game_directory
                / "BepInEx"
                / "plugins"
                / "OC2DIYLevel"
            )
            installed = target.is_dir() and (target / "OC2DIYLevel.dll").is_file()
            if installed:
                local_version = detect_framework_version(self.config.game_directory)
        if local_version is None:
            state = self.state.get_framework()
            if state is not None and Path(state.install_path).is_dir():
                local_version = state.version
        cloud_version = cloud.version if cloud is not None else None
        comparison = (
            compare_versions(local_version, cloud_version)
            if local_version is not None and cloud_version is not None
            else None
        )
        return FrameworkStatus(
            local_version=local_version,
            cloud_version=cloud_version,
            installed=installed,
            matches=comparison == 0,
            local_newer=comparison is not None and comparison > 0,
        )

    def remove(self, entry: MapEntry) -> None:
        if self.config.maps_directory is None or entry.install_path is None:
            return
        InstallService(self.config.maps_directory, self.config.cache_directory).remove(entry.install_path)
        self.state.remove(entry.id)

    def ensure_preview(self, metadata: MapMetadata) -> Path:
        if self.catalog_service is None:
            raise RuntimeError("catalog 尚未加载")
        return self.catalog_service.ensure_preview(metadata)

    def ensure_thumbnail(self, metadata: MapMetadata) -> Path:
        if self.catalog_service is None:
            raise RuntimeError("catalog 尚未加载")
        return self.catalog_service.ensure_thumbnail(metadata)

    def _entry(
        self,
        metadata: MapMetadata,
        discovered: dict[str, LocalMap],
    ) -> MapEntry:
        installed = self.state.get(metadata.id)
        install_path = Path(installed.install_path) if installed else None
        unmanaged = (
            discovered.get(metadata.map_key.casefold()) if metadata.map_key else None
        )
        if unmanaged is None:
            unmanaged = next((discovered[key] for key in metadata.previous_map_keys if key in discovered), None)
        if installed is None or install_path is None or not install_path.is_dir():
            if unmanaged is not None:
                cloud_map_version = _map_version(metadata)
                local_version = unmanaged.version or "未知"
                install_path = unmanaged.path
                if unmanaged.version is None:
                    status = MapStatus.UPDATE_AVAILABLE
                else:
                    comparison = compare_versions(
                        unmanaged.version, cloud_map_version
                    )
                    status = (
                        MapStatus.LOCAL_NEWER
                        if comparison > 0
                        else (
                            MapStatus.UPDATE_AVAILABLE
                            if comparison < 0
                            else MapStatus.LATEST
                        )
                    )
            else:
                status = MapStatus.NOT_INSTALLED
                local_version = "-"
        elif installed.sha256 == metadata.sha256:
            status = MapStatus.LATEST
            # The cloud package hash is authoritative.  Older clients stored
            # generated release versions or stale embedded map versions here;
            # do not expose those implementation details after the exact
            # package is known to match.
            local_version = _map_version(metadata)
        else:
            # A changed cloud hash is an update even when the author's version
            # number went backwards.  This is required for official rollbacks
            # and avoids comparing legacy date-based release identifiers with
            # author package versions.
            status = MapStatus.UPDATE_AVAILABLE
            local_version = installed.map_version or installed.version
        return MapEntry(
            id=metadata.id,
            name=metadata.name,
            author=metadata.author,
            version=_map_version(metadata),
            local_version=local_version,
            size=metadata.size,
            updated_at=metadata.updated_at.astimezone().strftime("%Y-%m-%d %H:%M"),
            status=MapStatus.AUTHOR_UPDATE if (
                metadata.availability_notice and metadata.support_package is None
            ) else status,
            metadata=metadata,
            install_path=install_path,
        )

    def _scan_local_maps(self) -> dict[str, LocalMap]:
        root = self.config.maps_directory
        if root is None or not root.is_dir():
            return {}
        result: dict[str, LocalMap] = {}
        for directory in root.iterdir():
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            try:
                infos = [
                    item for item in directory.iterdir()
                    if item.is_file() and item.name.casefold().startswith("info_")
                ]
                has_stage = any(
                    item.is_file() and item.name.casefold().startswith("s_")
                    for item in directory.iterdir()
                )
            except OSError:
                continue
            if len(infos) != 1 or not has_stage:
                continue
            map_key = infos[0].name[len("info_") :].strip().casefold()
            if not map_key or map_key in _FRAMEWORK_BUILTIN_MAP_KEYS:
                continue
            version: str | None = None
            try:
                manifest = read_info_manifest(infos[0])
                version = manifest.version
                manifest.close()
            except (OSError, ValueError):
                pass
            result.setdefault(map_key, LocalMap(directory.resolve(), version))
        return result


def _map_version(metadata: MapMetadata) -> str:
    """Return the published package version shown to updater users.

    LevelSetInfoSO is author-controlled content and is often left at an older
    value even when the distributed ZIP has advanced.  Displaying it as the
    latest version made a current package look stale, so the updater uses the
    cloud package/source version instead.
    """

    return metadata.source_version or metadata.version


def _support_profile(metadata: MapMetadata) -> str | None:
    prefix = "support-profile:"
    return next(
        (
            tag[len(prefix) :].strip()
            for tag in metadata.tags
            if tag.casefold().startswith(prefix) and tag[len(prefix) :].strip()
        ),
        None,
    )


def _obsolete_map_files(metadata: MapMetadata) -> tuple[str, ...]:
    prefix = "map-cleanup:"
    return tuple(
        tag[len(prefix) :].strip()
        for tag in metadata.tags
        if tag.casefold().startswith(prefix) and tag[len(prefix) :].strip()
    )
