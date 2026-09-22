from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


APP_NAME = "OC2MapManager"
DEFAULT_GITHUB_REPOSITORY = "purewi/oc2-map-repository"
DEFAULT_GITEE_REPOSITORY = "purewi/oc2-map-updates"


def app_data_directory() -> Path:
    root = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return root / APP_NAME


class ClientConfig(BaseModel):
    schema_version: int = 3
    cloud_provider: Literal["gitee", "github"] = "github"
    cloud_repository: str = DEFAULT_GITHUB_REPOSITORY
    # Retained so v1.x config files remain readable and rollback stays possible.
    github_repository: str = DEFAULT_GITHUB_REPOSITORY
    game_directory: Path | None = None
    maps_directory: Path | None = None
    cache_directory: Path = Field(default_factory=lambda: app_data_directory() / "cache")
    auto_check_updates: bool = True
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    retries: int = Field(default=3, ge=1, le=10)

    @property
    def state_file(self) -> Path:
        return app_data_directory() / "updater_state.json"

    @property
    def config_file(self) -> Path:
        return app_data_directory() / "config.json"

    def ensure_directories(self) -> None:
        app_data_directory().mkdir(parents=True, exist_ok=True)
        self.cache_directory.mkdir(parents=True, exist_ok=True)
        (self.cache_directory / "downloads").mkdir(parents=True, exist_ok=True)
        (self.cache_directory / "previews").mkdir(parents=True, exist_ok=True)
        (self.cache_directory / "backups").mkdir(parents=True, exist_ok=True)

    def save(self) -> None:
        self.ensure_directories()
        temporary = self.config_file.with_suffix(".json.tmp")
        temporary.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, self.config_file)


def load_config() -> ClientConfig:
    path = app_data_directory() / "config.json"
    if path.is_file():
        try:
            config = ClientConfig.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = ClientConfig()
    else:
        config = ClientConfig()
    if config.schema_version < 3:
        config.schema_version = 3
        config.cloud_provider = "github"
        config.cloud_repository = config.github_repository or DEFAULT_GITHUB_REPOSITORY
        config.save()
    if not config.cloud_repository.strip():
        config.cloud_repository = (
            DEFAULT_GITEE_REPOSITORY
            if config.cloud_provider == "gitee"
            else DEFAULT_GITHUB_REPOSITORY
        )
    if not config.github_repository.strip():
        config.github_repository = DEFAULT_GITHUB_REPOSITORY
    if config.game_directory is None:
        if config.maps_directory is not None:
            config.game_directory = game_directory_from_levels(config.maps_directory)
        if config.game_directory is None:
            config.game_directory = detect_game_directory()
    if config.maps_directory is None and config.game_directory is not None:
        config.maps_directory = levels_directory_from_game(config.game_directory)
    config.ensure_directories()
    return config


def detect_levels_directory() -> Path | None:
    game = detect_game_directory()
    if game is None:
        return None
    candidate = levels_directory_from_game(game)
    return candidate.resolve() if candidate.is_dir() else None


def detect_game_directory() -> Path | None:
    for steam_root in _steam_roots():
        candidate = steam_root / "steamapps" / "common" / "Overcooked! 2"
        if candidate.is_dir():
            return candidate.resolve()
    return None


def levels_directory_from_game(game: Path) -> Path:
    return (
        game
        / "BepInEx"
        / "plugins"
        / "OC2DIYLevel"
        / "levels"
    )


def game_directory_from_levels(levels: Path) -> Path | None:
    resolved = levels.resolve()
    try:
        game = resolved.parents[3]
    except IndexError:
        return None
    expected = game / "BepInEx" / "plugins" / "OC2DIYLevel" / "levels"
    return game if expected.resolve() == resolved else None


def _steam_roots() -> list[Path]:
    roots: list[Path] = []
    if os.name == "nt":
        try:
            import winreg

            for key_name, value_name in (
                (r"Software\Valve\Steam", "SteamPath"),
                (r"Software\WOW6432Node\Valve\Steam", "InstallPath"),
            ):
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
                        roots.append(Path(str(winreg.QueryValueEx(key, value_name)[0])))
                except OSError:
                    continue
        except ImportError:
            pass
    roots.extend((Path("C:/Program Files (x86)/Steam"), Path("C:/Program Files/Steam")))
    libraries: list[Path] = []
    for root in roots:
        if root not in libraries:
            libraries.append(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if not vdf.is_file():
            continue
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for value in re.findall(r'"path"\s+"([^"]+)"', text, re.IGNORECASE):
            path = Path(value.replace("\\\\", "\\"))
            if path not in libraries:
                libraries.append(path)
    return libraries
