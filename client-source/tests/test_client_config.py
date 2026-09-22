from __future__ import annotations

from client.config import (
    DEFAULT_GITEE_REPOSITORY,
    DEFAULT_GITHUB_REPOSITORY,
    ClientConfig,
    load_config,
)


def test_new_config_uses_official_repository() -> None:
    assert ClientConfig().cloud_provider == "github"
    assert ClientConfig().cloud_repository == DEFAULT_GITHUB_REPOSITORY
    assert ClientConfig().github_repository == DEFAULT_GITHUB_REPOSITORY


def test_load_config_migrates_empty_repository(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    config = ClientConfig(github_repository="", cache_directory=tmp_path / "cache")
    config.save()

    loaded = load_config()

    assert loaded.github_repository == DEFAULT_GITHUB_REPOSITORY


def test_load_config_migrates_gitee_client_to_github(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    config = ClientConfig(
        schema_version=2,
        cloud_provider="gitee",
        cloud_repository=DEFAULT_GITEE_REPOSITORY,
        cache_directory=tmp_path / "cache",
    )
    config.save()

    loaded = load_config()

    assert loaded.schema_version == 3
    assert loaded.cloud_provider == "github"
    assert loaded.cloud_repository == DEFAULT_GITHUB_REPOSITORY
