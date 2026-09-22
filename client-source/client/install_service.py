from __future__ import annotations

import os
import hashlib
import json
import logging
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from send2trash import send2trash

from publisher.models import MapMetadata


logger = logging.getLogger(__name__)


_BLOCKED_EXTENSIONS = {
    ".exe", ".dll", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".msi", ".lnk",
}
_ALLOWED_PLUGIN_ROOT_SUPPORT_FILES = {
    "jonnylevelloaderplugin.dll",
    "jonnylevelscripts.dll",
}
_JONNY_LEGACY_SUPPORT_PATHS = (
    PurePosixPath("OC2DIYLevel/JonnyLevelLoaderPlugin.dll"),
    PurePosixPath("OC2DIYLevel/JonnyLevelScripts.dll"),
    PurePosixPath("OC2DIYLevelRuntimeWLoader/JonnyLevelLoaderPlugin.dll"),
    PurePosixPath("OC2DIYLevelRuntimeWLoader/JonnyLevelScripts.dll"),
)


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InstallResult:
    install_path: Path
    replaced_existing: bool


@dataclass(frozen=True, slots=True)
class SupportFileTarget:
    source_name: str
    target_path: PurePosixPath


@dataclass(frozen=True, slots=True)
class SupportInstallPlan:
    profile: str
    files: tuple[SupportFileTarget, ...]
    cleanup: tuple[PurePosixPath, ...] = ()
    map_cleanup: tuple[tuple[tuple[str, ...], tuple[PurePosixPath, ...]], ...] = ()


class InstallService:
    def __init__(self, maps_directory: Path, cache_directory: Path) -> None:
        self.maps_directory = maps_directory.resolve()
        self.cache_directory = cache_directory.resolve()

    def install(
        self,
        metadata: MapMetadata,
        archive_path: Path,
        *,
        existing_path: Path | None = None,
        obsolete_files: tuple[str, ...] = (),
    ) -> InstallResult:
        self.maps_directory.mkdir(parents=True, exist_ok=True)
        logger.info("地图安装开始 map_id=%s version=%s", metadata.id, metadata.version)
        map_key = _safe_component(metadata.map_key or metadata.id)
        target = existing_path.resolve() if existing_path and existing_path.is_dir() else self._find_existing(map_key)
        if target is None:
            target = (self.maps_directory / map_key).resolve()
        _require_child(target, self.maps_directory)

        with tempfile.TemporaryDirectory(prefix="oc2-map-install-") as temporary:
            extracted = Path(temporary) / "payload"
            detected_key = safe_extract_map(
                archive_path,
                extracted,
                obsolete_files=obsolete_files,
            )
            if metadata.map_key and detected_key.casefold() != metadata.map_key.casefold():
                raise InstallError(
                    f"地图内部标识不一致: catalog={metadata.map_key} zip={detected_key}"
                )
            validate_map_directory(extracted, detected_key)

            token = uuid.uuid4().hex
            incoming = self.maps_directory / f".oc2_new_{token}"
            previous = self.maps_directory / f".oc2_previous_{token}"
            backup = (
                self.cache_directory
                / "backups"
                / metadata.id
                / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            )
            replaced = target.is_dir()
            activated = False
            old_moved = False
            try:
                shutil.copytree(extracted, incoming)
                validate_map_directory(incoming, detected_key)
                if replaced:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(target, backup)
                    logger.info("旧地图备份完成 map_id=%s", metadata.id)
                    os.replace(target, previous)
                    old_moved = True
                os.replace(incoming, target)
                activated = True
                validate_map_directory(target, detected_key)
                if previous.exists():
                    shutil.rmtree(previous)
                if backup.exists():
                    shutil.rmtree(backup)
                logger.info("地图安装成功 map_id=%s path=%s", metadata.id, target)
                return InstallResult(target, replaced)
            except Exception as exc:
                if activated and target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                if old_moved and previous.exists():
                    os.replace(previous, target)
                    logger.warning("地图安装失败，旧版本已回滚 map_id=%s", metadata.id)
                shutil.rmtree(incoming, ignore_errors=True)
                raise InstallError(f"安装失败，已尝试回滚: {type(exc).__name__}") from exc

    def remove(self, install_path: Path) -> None:
        resolved = install_path.resolve()
        _require_child(resolved, self.maps_directory)
        if resolved.is_dir():
            send2trash(str(resolved))
            logger.info("本地地图已移动到回收站 path=%s", resolved)

    def _find_existing(self, map_key: str) -> Path | None:
        expected_info = f"info_{map_key}".casefold()
        matches = []
        for directory in self.maps_directory.iterdir():
            if directory.is_dir() and not directory.name.startswith("."):
                if any(item.name.casefold() == expected_info for item in directory.iterdir() if item.is_file()):
                    matches.append(directory.resolve())
        if len(matches) > 1:
            raise InstallError("发现多个相同内部标识的地图目录，拒绝自动替换")
        return matches[0] if matches else None


class SupportInstallService:
    """Install a publisher-generated support bundle below BepInEx/plugins."""

    def __init__(self, game_directory: Path, cache_directory: Path) -> None:
        self.plugins_directory = (
            game_directory.resolve() / "BepInEx" / "plugins"
        ).resolve()
        self.plugin_directory = (self.plugins_directory / "OC2DIYLevel").resolve()
        self.cache_directory = cache_directory.resolve()

    def install(
        self,
        archive_path: Path,
        map_id: str,
        *,
        profile: str | tuple[str, ...] | None = None,
    ) -> tuple[Path, ...]:
        if not (self.plugin_directory / "OC2DIYLevel.dll").is_file():
            raise InstallError("请先安装或更新 OC2DIYLevel 地图框架")
        with tempfile.TemporaryDirectory(prefix="oc2-support-") as temporary:
            extracted = Path(temporary) / "payload"
            names = safe_extract_support(archive_path, extracted)
            requested_profiles = profile if isinstance(profile, tuple) else (profile,)
            plans = tuple(
                _with_builtin_support_migrations(
                    read_support_install_plan(archive_path, names, profile=value)
                )
                for value in requested_profiles
            )
            plan = _combine_support_install_plans(plans)
            backup = (
                self.cache_directory
                / "backups"
                / "support"
                / _safe_component(map_id)
                / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            )
            backup.mkdir(parents=True, exist_ok=True)
            installed: list[Path] = []
            previous: list[tuple[Path, Path, bool]] = []
            try:
                cleanup_targets = [
                    self._plugin_target(path) for path in plan.cleanup
                ]
                cleanup_targets.extend(self._map_cleanup_targets(plan))
                writes: list[tuple[Path, Path]] = []
                for item in plan.files:
                    target = self._plugin_target(item.target_path)
                    incoming_file = (extracted / item.source_name).resolve()
                    _require_child(incoming_file, extracted)
                    covered_by_cleanup = any(
                        _is_same_or_child(target, cleanup)
                        for cleanup in cleanup_targets
                    )
                    if (
                        not covered_by_cleanup
                        and target.is_file()
                        and _same_file_content(target, incoming_file)
                    ):
                        logger.info("支持文件内容相同，跳过重复部署: %s", target.name)
                        continue
                    writes.append((incoming_file, target))

                mutation_targets = _collapse_paths(
                    cleanup_targets + [target for _, target in writes]
                )
                for index, target in enumerate(mutation_targets):
                    if not target.exists():
                        continue
                    saved = backup / f"{index:04d}"
                    if target.is_dir():
                        shutil.copytree(target, saved)
                        previous.append((target, saved, True))
                        shutil.rmtree(target)
                    else:
                        shutil.copy2(target, saved)
                        previous.append((target, saved, False))
                        target.unlink()

                for incoming_file, target in writes:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    staging = target.parent / f".oc2_support_{uuid.uuid4().hex}"
                    shutil.copy2(incoming_file, staging)
                    os.replace(staging, target)
                    installed.append(target)
                for item in plan.files:
                    target = self._plugin_target(item.target_path)
                    source = (extracted / item.source_name).resolve()
                    if not target.is_file() or not _same_file_content(target, source):
                        raise InstallError(f"前置文件未正确部署到目标位置: {item.target_path}")
                shutil.rmtree(backup, ignore_errors=True)
                logger.info(
                    "地图支持文件安装成功 map_id=%s profile=%s files=%s cleanup=%s",
                    map_id,
                    plan.profile,
                    len(installed),
                    len(cleanup_targets),
                )
                return tuple(installed)
            except Exception as exc:
                for target in reversed(_collapse_paths([item for item in installed])):
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        target.unlink(missing_ok=True)
                for target, saved, was_directory in reversed(previous):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if was_directory and saved.is_dir():
                        if target.exists():
                            shutil.rmtree(target, ignore_errors=True)
                        shutil.copytree(saved, target)
                    elif saved.is_file():
                        shutil.copy2(saved, target)
                raise InstallError(f"支持文件安装失败，已尝试回滚: {type(exc).__name__}") from exc

    def _plugin_target(self, relative: PurePosixPath) -> Path:
        target = (self.plugins_directory / Path(*relative.parts)).resolve()
        _require_child(target, self.plugins_directory)
        return target

    def _map_cleanup_targets(self, plan: SupportInstallPlan) -> list[Path]:
        levels = self.plugin_directory / "levels"
        if not levels.is_dir():
            return []
        rules: dict[str, tuple[PurePosixPath, ...]] = {}
        for keys, files in plan.map_cleanup:
            for key in keys:
                rules[key.casefold()] = files
        targets: list[Path] = []
        for directory in levels.iterdir():
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            infos = [
                item for item in directory.iterdir()
                if item.is_file() and item.name.casefold().startswith("info_")
            ]
            if len(infos) != 1:
                continue
            key = infos[0].name[len("info_") :].strip().casefold()
            for relative in rules.get(key, ()):
                target = (directory / Path(*relative.parts)).resolve()
                _require_child(target, directory.resolve())
                targets.append(target)
        return targets


def _same_file_content(first: Path, second: Path) -> bool:
    if first.stat().st_size != second.stat().st_size:
        return False
    def digest(path: Path) -> bytes:
        value = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(block)
        return value.digest()
    return digest(first) == digest(second)


def safe_extract_support(archive_path: Path, destination: Path) -> tuple[str, ...]:
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive_path) as archive:
        try:
            manifest = json.loads(archive.read("support-manifest.json").decode("utf-8"))
        except (KeyError, UnicodeDecodeError, ValueError) as exc:
            raise InstallError("支持包缺少有效的 support-manifest.json") from exc
        if manifest.get("schemaVersion") != 1 or manifest.get("installTarget") != "OC2DIYLevel":
            raise InstallError("不支持的地图支持包格式或安装目标")
        declarations = manifest.get("files")
        if not isinstance(declarations, list) or not declarations:
            raise InstallError("支持包没有声明文件")
        names: list[str] = []
        for item in declarations:
            if not isinstance(item, dict):
                raise InstallError("支持包文件声明无效")
            name = str(item.get("name") or "").strip()
            if not _safe_support_name(name):
                raise InstallError(f"支持文件名不安全: {name}")
            if name.casefold() in {value.casefold() for value in names}:
                raise InstallError(f"支持文件重名: {name}")
            names.append(name)
        declared_entries = {f"payload/{name}" for name in names}
        actual_entries = {
            item.filename.replace("\\", "/")
            for item in archive.infolist()
            if not item.is_dir() and item.filename != "support-manifest.json"
        }
        if actual_entries != declared_entries:
            raise InstallError("支持包内容与清单不一致")
        total = 0
        for name in names:
            info = archive.getinfo(f"payload/{name}")
            _validate_member(info, PurePosixPath(info.filename))
            total += info.file_size
            if total > 1024 * 1024 * 1024:
                raise InstallError("支持包解压后体积超过 1GB")
            with archive.open(info) as source, (destination / name).open("xb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
    return tuple(names)


def read_support_install_plan(
    archive_path: Path,
    names: tuple[str, ...],
    *,
    profile: str | None,
) -> SupportInstallPlan:
    """Read the optional layout carried inside a backwards-compatible v1 bundle."""

    if "support-layout.json" not in {name.casefold() for name in names}:
        return SupportInstallPlan(
            profile="legacy",
            files=tuple(
                SupportFileTarget(name, PurePosixPath("OC2DIYLevel") / name)
                for name in names
            ),
        )
    actual_layout_name = next(
        name for name in names if name.casefold() == "support-layout.json"
    )
    with zipfile.ZipFile(archive_path) as archive:
        try:
            layout = json.loads(
                archive.read(f"payload/{actual_layout_name}").decode("utf-8")
            )
        except (KeyError, UnicodeDecodeError, ValueError) as exc:
            raise InstallError("前置包内的安装布局无效") from exc
    if layout.get("schemaVersion") != 1:
        raise InstallError("不支持的前置安装布局版本")
    profiles = layout.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise InstallError("前置安装布局没有可用配置")
    selected_name = (profile or str(layout.get("defaultProfile") or "")).strip()
    selected = profiles.get(selected_name)
    if not selected_name or not isinstance(selected, dict):
        raise InstallError(f"前置包不包含所需配置: {selected_name or '未指定'}")
    declared = {name.casefold(): name for name in names}
    files: list[SupportFileTarget] = []
    for item in selected.get("files") or []:
        if not isinstance(item, dict):
            raise InstallError("前置安装布局的文件声明无效")
        requested_source = str(item.get("source") or "").strip()
        source = declared.get(requested_source.casefold())
        target = _safe_support_relative_path(str(item.get("target") or ""))
        if source is None or source.casefold() == "support-layout.json":
            raise InstallError(f"前置安装布局引用了未声明文件: {requested_source}")
        files.append(SupportFileTarget(source, target))
    if not files:
        raise InstallError(f"前置配置没有声明安装文件: {selected_name}")
    cleanup = tuple(
        _safe_support_relative_path(str(value))
        for value in (selected.get("cleanup") or [])
    )
    map_cleanup = []
    for rule in selected.get("mapCleanup") or []:
        if not isinstance(rule, dict):
            raise InstallError("地图旧前置清理规则无效")
        keys = tuple(
            str(value).strip() for value in (rule.get("mapKeys") or [])
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", str(value).strip())
        )
        paths = tuple(
            _safe_map_cleanup_path(str(value))
            for value in (rule.get("files") or [])
        )
        if not keys or not paths:
            raise InstallError("地图旧前置清理规则缺少地图标识或文件")
        map_cleanup.append((keys, paths))
    return SupportInstallPlan(selected_name, tuple(files), cleanup, tuple(map_cleanup))


def _combine_support_install_plans(
    plans: tuple[SupportInstallPlan, ...],
) -> SupportInstallPlan:
    if not plans:
        raise InstallError("没有指定需要安装的前置配置")
    files_by_target: dict[str, SupportFileTarget] = {}
    cleanup: list[PurePosixPath] = []
    map_cleanup: list[tuple[tuple[str, ...], tuple[PurePosixPath, ...]]] = []
    for plan in plans:
        for item in plan.files:
            key = item.target_path.as_posix().casefold()
            existing = files_by_target.get(key)
            if existing is not None and existing.source_name.casefold() != item.source_name.casefold():
                raise InstallError(f"多个前置配置写入同一目标: {item.target_path}")
            files_by_target[key] = item
        for path in plan.cleanup:
            if path.as_posix().casefold() not in {
                value.as_posix().casefold() for value in cleanup
            }:
                cleanup.append(path)
        map_cleanup.extend(plan.map_cleanup)
    return SupportInstallPlan(
        profile="+".join(plan.profile for plan in plans),
        files=tuple(files_by_target.values()),
        cleanup=tuple(cleanup),
        map_cleanup=tuple(map_cleanup),
    )


def _with_builtin_support_migrations(plan: SupportInstallPlan) -> SupportInstallPlan:
    """Clean locations used by the first Jonny-support client release.

    The migration stays client-side so v1.10.2 can repair the already-published
    2026.09.19.1 bundle without pretending its 2.2.1 runtime is a new release.
    """

    if plan.profile.casefold() != "jonny":
        return plan
    cleanup = list(plan.cleanup)
    known = {path.as_posix().casefold() for path in cleanup}
    cleanup.extend(
        path
        for path in _JONNY_LEGACY_SUPPORT_PATHS
        if path.as_posix().casefold() not in known
    )
    return SupportInstallPlan(
        profile=plan.profile,
        files=plan.files,
        cleanup=tuple(cleanup),
        map_cleanup=plan.map_cleanup,
    )


def _safe_support_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/").strip("/"))
    windows = PureWindowsPath(value)
    if (
        not value
        or path.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in value
        or (
            path.parts[0].casefold()
            not in {"oc2diylevel", "oc2diylevelruntimewloader"}
            and not (
                len(path.parts) == 1
                and path.name.casefold() in _ALLOWED_PLUGIN_ROOT_SUPPORT_FILES
            )
        )
    ):
        raise InstallError(f"前置安装路径不安全: {value}")
    return path


def _safe_map_cleanup_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/").strip("/"))
    windows = PureWindowsPath(value)
    if (
        not value
        or path.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in value
        or path.name.casefold().startswith(("info_", "s_"))
    ):
        raise InstallError(f"地图旧前置清理路径不安全: {value}")
    return path


def _safe_support_name(name: str) -> bool:
    if not name or Path(name).name != name or name in {".", ".."}:
        return False
    suffix = Path(name).suffix.casefold()
    return (
        suffix in {".dll", ".qichi", ".txt", ".json", ".cfg", ".ini", ".xml", ".dat"}
        or re.fullmatch(r"commonw\d+", name, re.IGNORECASE) is not None
    )


def safe_extract_map(
    archive_path: Path,
    destination: Path,
    *,
    obsolete_files: tuple[str, ...] = (),
) -> str:
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        files = [item for item in archive.infolist() if not item.is_dir()]
        infos = [item for item in files if PurePosixPath(item.filename.replace("\\", "/")).name.casefold().startswith("info_")]
        if len(infos) != 1:
            raise InstallError("地图 ZIP 必须恰好包含一个 info_* 文件")
        info_path = PurePosixPath(infos[0].filename.replace("\\", "/"))
        map_key = info_path.name[len("info_") :].strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", map_key):
            raise InstallError("info_* 地图内部标识不安全")
        prefix = info_path.parent
        obsolete = {
            _safe_map_cleanup_path(value).as_posix().casefold()
            for value in obsolete_files
        }
        if not any(
            PurePosixPath(item.filename.replace("\\", "/")).parent == prefix
            and PurePosixPath(item.filename.replace("\\", "/")).name.casefold().startswith("s_")
            for item in files
        ):
            raise InstallError("info_* 同层没有 s_* 关卡文件")
        extracted = 0
        for item in archive.infolist():
            normalized = item.filename.replace("\\", "/")
            path = PurePosixPath(normalized)
            _validate_member(item, path)
            try:
                relative = path.relative_to(prefix)
            except ValueError:
                continue
            if not relative.parts:
                continue
            if relative.as_posix().casefold() in obsolete:
                logger.info("安装地图时跳过旧版前置文件: %s", relative)
                continue
            target = (root / Path(*relative.parts)).resolve()
            _require_child(target, root)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if target.suffix.casefold() in _BLOCKED_EXTENSIONS:
                raise InstallError(f"压缩包含禁止文件: {relative}")
            extracted += item.file_size
            if extracted > 2 * 1024 * 1024 * 1024:
                raise InstallError("解压后体积超过 2GB")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
    validate_map_directory(destination, map_key)
    return map_key


def validate_map_directory(path: Path, map_key: str) -> None:
    if not (path / f"info_{map_key}").is_file():
        raise InstallError(f"解压结果缺少 info_{map_key}")
    if not any(item.is_file() and item.name.casefold().startswith("s_") for item in path.iterdir()):
        raise InstallError("解压结果没有同层 s_* 关卡文件")
    if any(item.suffix.casefold() in _BLOCKED_EXTENSIONS for item in path.rglob("*")):
        raise InstallError("地图目录含禁止的可执行内容")


def _validate_member(info: zipfile.ZipInfo, path: PurePosixPath) -> None:
    windows = PureWindowsPath(info.filename)
    if (
        not info.filename
        or "\x00" in info.filename
        or path.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part == ".." for part in path.parts)
        or ":" in info.filename
    ):
        raise InstallError(f"ZIP 包含不安全路径: {info.filename}")
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind == stat.S_IFLNK or kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise InstallError(f"ZIP 包含链接或特殊文件: {info.filename}")
    if info.flag_bits & 0x1:
        raise InstallError("不支持加密 ZIP")


def _safe_component(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip(" .")
    if not result or result in {".", ".."}:
        raise InstallError("无法生成安全的地图目录名")
    return result[:100]


def _require_child(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise InstallError(f"路径逃逸出地图目录: {path}") from exc


def _is_same_or_child(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _collapse_paths(paths: list[Path]) -> list[Path]:
    unique = sorted(
        {path.resolve() for path in paths},
        key=lambda value: (len(value.parts), str(value).casefold()),
    )
    result: list[Path] = []
    for path in unique:
        if any(_is_same_or_child(path, parent) for parent in result):
            continue
        result.append(path)
    return result
