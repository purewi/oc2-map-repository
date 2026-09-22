from __future__ import annotations

import os
from pathlib import Path

from publisher.models import InstalledMap, UpdaterState


class LocalStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.state = UpdaterState()

    def load(self) -> UpdaterState:
        if not self.path.is_file():
            self.state = UpdaterState()
            return self.state
        try:
            self.state = UpdaterState.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            backup = self.path.with_suffix(".json.invalid")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            self.state = UpdaterState()
        return self.state

    def get(self, map_id: str) -> InstalledMap | None:
        return self.state.installed_maps.get(map_id)

    def set(self, map_id: str, value: InstalledMap) -> None:
        self.state.installed_maps[map_id] = value
        self.save()

    def remove(self, map_id: str) -> None:
        self.state.installed_maps.pop(map_id, None)
        self.save()

    def get_framework(self) -> InstalledMap | None:
        return self.state.installed_framework

    def set_framework(self, value: InstalledMap) -> None:
        self.state.installed_framework = value
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(self.state.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
