from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath


logger = logging.getLogger(__name__)


class FrameworkInstallError(RuntimeError):
    pass


class FrameworkInstallService:
    """Safely installs OC2DIYLevel while preserving user maps and saves."""

    def __init__(self, game_directory: Path) -> None:
        self.game_directory = game_directory.resolve()
        self.plugins_directory = self.game_directory / "BepInEx" / "plugins"
        self.target = self.plugins_directory / "OC2DIYLevel"

    def install(
        self,
        archive_path: Path,
        *,
        version: str | None = None,
        sha256: str | None = None,
    ) -> Path:
        self.plugins_directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="oc2-framework-install-") as temporary:
            extracted = Path(temporary) / "OC2DIYLevel"
            safe_extract_framework(archive_path, extracted)

            token = uuid.uuid4().hex
            incoming = self.plugins_directory / f".oc2_framework_new_{token}"
            previous = self.plugins_directory / f".oc2_framework_previous_{token}"
            old_moved = False
            activated = False
            try:
                shutil.copytree(extracted, incoming)
                if self.target.is_dir():
                    _preserve_user_content(self.target, incoming)
                if version:
                    (incoming / ".oc2-framework-version.json").write_text(
                        json.dumps(
                            {"version": version, "sha256": sha256},
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                if self.target.is_dir():
                    os.replace(self.target, previous)
                    old_moved = True
                os.replace(incoming, self.target)
                activated = True
                validate_framework_directory(self.target)
            except Exception as exc:
                if activated and self.target.exists():
                    shutil.rmtree(self.target, ignore_errors=True)
                if old_moved and previous.exists():
                    try:
                        os.replace(previous, self.target)
                    except OSError:
                        logger.exception("地图框架回滚失败 previous=%s", previous)
                shutil.rmtree(incoming, ignore_errors=True)
                if isinstance(exc, PermissionError):
                    detail = (
                        "框架文件正在被占用或当前账号没有写入权限。请彻底退出 "
                        "Overcooked! 2 后重试；若游戏已关闭，请以管理员身份运行更新器。"
                    )
                else:
                    detail = f"{type(exc).__name__}: {exc}"
                raise FrameworkInstallError(
                    f"地图框架安装失败，已尝试恢复原文件。{detail}"
                ) from exc

            # The replacement is already active and validated. Old-directory
            # cleanup is best effort and must not roll a successful update back.
            if previous.exists():
                try:
                    shutil.rmtree(previous)
                except OSError:
                    logger.warning(
                        "新框架已安装，但旧框架临时目录暂时无法清理: %s",
                        previous,
                        exc_info=True,
                    )
            return self.target


def _preserve_user_content(existing: Path, incoming: Path) -> None:
    """Keep maps, saves and third-party support files absent from the new package."""

    fresh_names = {item.name.casefold() for item in incoming.iterdir()}
    for source in existing.iterdir():
        if source.name == ".oc2-framework-version.json":
            continue
        destination = incoming / source.name
        if source.name.casefold() in {"levels", "savefiles"} and source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
            continue
        if source.name.casefold() in fresh_names:
            continue
        if source.is_dir():
            shutil.copytree(source, destination)
        elif source.is_file():
            shutil.copy2(source, destination)


def detect_framework_version(game_directory: Path) -> str | None:
    """Detect the actually installed OC2DIYLevel plugin version.

    New updater installs carry a marker.  Existing/manual installs are detected
    from the BepInPlugin metadata strings embedded in the managed DLL.
    """

    target = game_directory.resolve() / "BepInEx" / "plugins" / "OC2DIYLevel"
    marker = target / ".oc2-framework-version.json"
    if marker.is_file():
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
            version = str(value.get("version") or "").strip().removeprefix("v")
            if version:
                return version
        except (OSError, ValueError, AttributeError):
            pass

    dll = target / "OC2DIYLevel.dll"
    if not dll.is_file():
        return None
    try:
        payload = dll.read_bytes()
    except OSError:
        return None
    strings = [match.group().decode("ascii", "ignore") for match in re.finditer(rb"[\x20-\x7e]{3,}", payload)]
    plugin_names = {"dev.gua.overcooked.diylevel", "Overcooked2 DIYLevel Plugin"}
    version_pattern = re.compile(r"^v?(\d+(?:\.\d+){1,3})$")
    for index, value in enumerate(strings):
        if value not in plugin_names:
            continue
        for nearby in strings[index + 1 : index + 8]:
            match = version_pattern.fullmatch(nearby.strip())
            if match:
                return match.group(1)
    # Older builds also keep a human-readable UTF-16 version string.
    text = payload.decode("utf-16le", "ignore")
    match = re.search(r"(?i)(?:version|m_VersionString).{0,160}?v?(\d+(?:\.\d+){1,3})", text)
    return match.group(1) if match else None


def safe_extract_framework(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    prefix = PurePosixPath("OC2DIYLevel")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                path = PurePosixPath(info.filename.replace("\\", "/"))
                _validate_member(info, path)
                try:
                    relative = path.relative_to(prefix)
                except ValueError:
                    raise FrameworkInstallError(
                        "框架 ZIP 顶层目录必须是 OC2DIYLevel"
                    ) from None
                if not relative.parts:
                    continue
                target = (root / Path(*relative.parts)).resolve()
                _require_child(target, root)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
    except zipfile.BadZipFile as exc:
        raise FrameworkInstallError("框架文件不是有效 ZIP") from exc
    validate_framework_directory(destination)


def validate_framework_directory(path: Path) -> None:
    required = (path / "OC2DIYLevel.dll", path / "LevelEditorStub.dll", path / "levels")
    if not required[0].is_file() or not required[1].is_file() or not required[2].is_dir():
        raise FrameworkInstallError("框架包缺少 OC2DIYLevel.dll、LevelEditorStub.dll 或 levels")


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
        raise FrameworkInstallError(f"框架 ZIP 包含不安全路径: {info.filename}")
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind == stat.S_IFLNK or kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise FrameworkInstallError(f"框架 ZIP 包含链接或特殊文件: {info.filename}")
    if info.flag_bits & 0x1:
        raise FrameworkInstallError("不支持加密 ZIP")


def _require_child(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise FrameworkInstallError(f"框架 ZIP 路径逃逸: {path}") from exc
