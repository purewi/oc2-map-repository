from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import UnityPy
from PIL import Image


_MAX_INFO_BYTES = 128 * 1024 * 1024
_MAX_SCREENSHOTS = 12
_MAX_IMAGE_PIXELS = 50_000_000
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(slots=True)
class LevelScreenshot:
    scene_name: str
    image: Image.Image


@dataclass(slots=True)
class MapManifest:
    map_key: str
    uid: str | None = None
    author: str | None = None
    name: str | None = None
    name_zh: str | None = None
    version: str | None = None
    level_count: int | None = None
    level_scenes: tuple[str, ...] = ()
    screenshots: tuple[LevelScreenshot, ...] = ()
    screenshot_errors: tuple[str, ...] = ()

    @property
    def display_name(self) -> str | None:
        return self.name_zh or self.name

    def close(self) -> None:
        for screenshot in self.screenshots:
            screenshot.image.close()


def read_map_manifest(
    zip_path: Path, *, include_screenshots: bool = False
) -> MapManifest:
    """Read the authoritative LevelSetInfoSO data from an OC2 map archive.

    Old packages and synthetic test fixtures may contain an ``info_*`` file that
    is not a Unity asset bundle. Those archives still publish normally, using the
    internal key and the existing filename/uploader fallbacks.
    """

    with zipfile.ZipFile(zip_path) as archive:
        candidates = [
            item
            for item in archive.infolist()
            if not item.is_dir()
            and PurePosixPath(item.filename).name.casefold().startswith("info_")
            and item.file_size <= _MAX_INFO_BYTES
        ]
        if not candidates:
            raise ValueError("地图包缺少 info_* 文件")
        candidates.sort(
            key=lambda item: (len(PurePosixPath(item.filename).parts), item.filename)
        )
        selected = candidates[0]
        basename = PurePosixPath(selected.filename).name
        map_key = basename[5:].strip()
        if not map_key:
            raise ValueError("地图包 info_* 内部标识为空")
        payload = archive.read(selected)

    return _read_manifest_payload(payload, map_key, include_screenshots)


def read_info_manifest(info_path: Path) -> MapManifest:
    """Read metadata directly from an installed ``info_*`` AssetBundle."""

    basename = info_path.name
    if not basename.casefold().startswith("info_"):
        raise ValueError("文件名不是 info_*")
    map_key = basename[5:].strip()
    if not map_key:
        raise ValueError("info_* 内部标识为空")
    return _read_manifest_payload(info_path.read_bytes(), map_key, False)


def _read_manifest_payload(
    payload: bytes, map_key: str, include_screenshots: bool
) -> MapManifest:
    manifest = MapManifest(map_key=map_key)
    try:
        environment = UnityPy.load(payload)
    except Exception:
        return manifest

    objects = {item.path_id: item for item in environment.objects}
    level_set: dict[str, Any] | None = None
    trees: dict[int, dict[str, Any]] = {}
    for item in environment.objects:
        if item.type.name != "MonoBehaviour":
            continue
        try:
            tree = item.read_typetree()
        except Exception:
            continue
        if not isinstance(tree, dict):
            continue
        trees[item.path_id] = tree
        if "uid" in tree and "author" in tree and "levelInfos" in tree:
            level_set = tree

    if level_set is None:
        return manifest

    level_refs = _pointer_ids(level_set.get("levelInfos"))
    manifest.uid = _clean_text(level_set.get("uid"), 255)
    manifest.author = _clean_text(level_set.get("author"), 255)
    manifest.name = _clean_text(level_set.get("levelSetName"), 255)
    manifest.name_zh = _clean_text(level_set.get("levelSetNameZH"), 255)
    manifest.version = _clean_text(level_set.get("version"), 80)
    manifest.level_count = len(level_refs)
    manifest.level_scenes = tuple(
        scene
        for level_id in level_refs
        if (tree := trees.get(level_id)) is not None
        if (scene := _clean_text(tree.get("sceneName"), 100)) is not None
    )
    if not include_screenshots:
        return manifest

    screenshots: list[LevelScreenshot] = []
    screenshot_errors: list[str] = []
    seen_sprites: set[int] = set()
    for level_id in level_refs:
        tree = trees.get(level_id)
        if tree is None:
            continue
        screenshot_id = _pointer_id(tree.get("screenshot"))
        if not screenshot_id or screenshot_id in seen_sprites:
            continue
        sprite_object = objects.get(screenshot_id)
        if sprite_object is None or sprite_object.type.name != "Sprite":
            continue
        try:
            image = sprite_object.read().image
            image.load()
            if image.width <= 0 or image.height <= 0:
                image.close()
                continue
            if image.width * image.height > _MAX_IMAGE_PIXELS:
                image.close()
                continue
            screenshots.append(
                LevelScreenshot(
                    scene_name=_clean_text(tree.get("sceneName"), 100) or "",
                    image=image.copy(),
                )
            )
            image.close()
            seen_sprites.add(screenshot_id)
        except Exception as exc:
            screenshot_errors.append(f"{type(exc).__name__}: {exc}")
            continue
        if len(screenshots) >= _MAX_SCREENSHOTS:
            break
    manifest.screenshots = tuple(screenshots)
    manifest.screenshot_errors = tuple(screenshot_errors)
    return manifest


def _pointer_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        pointer = _pointer_id(item)
        if pointer:
            result.append(pointer)
    return result


def _pointer_id(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    try:
        return int(value.get("m_PathID") or 0)
    except (TypeError, ValueError):
        return 0


def _clean_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL_CHARACTERS.sub("", value).strip()
    return cleaned[:limit] or None
