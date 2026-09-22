from __future__ import annotations

import os
from datetime import UTC, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from client.config import ClientConfig
from client import __version__
from client.models import MapEntry, MapStatus
from client.ui.dialogs import DetailsDialog
from client.ui.main_window import MainWindow
from publisher.models import MapMetadata, MapVersion, SupportPackage


def test_framework_prompt_once_and_completion_popup(tmp_path, monkeypatch):
    from client.map_manager import FrameworkStatus
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    config = ClientConfig(cache_directory=tmp_path / "cache", auto_check_updates=False)
    config.ensure_directories()
    window = MainWindow(config)
    questions, completions, installs = [], [], []
    monkeypatch.setattr(window.manager, "framework_status",
                        lambda: FrameworkStatus("0.9", "0.10", True, False, False))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args: questions.append(args) or QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: completions.append(args))
    monkeypatch.setattr(window, "install_framework", lambda: installs.append(True))
    window._prompt_framework_update()
    window._prompt_framework_update()
    assert len(questions) == len(installs) == 1
    window._batch_completed(True)
    assert "安装完成" in completions[0][2]
    assert window.progress.value() == 100
    window.close()
    app.processEvents()


def test_main_window_starts_with_default_repository(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])
    config = ClientConfig(
        cache_directory=tmp_path / "cache",
        maps_directory=tmp_path / "levels",
        auto_check_updates=False,
    )
    config.ensure_directories()
    window = MainWindow(config)
    assert config.cloud_provider == "github"
    assert config.cloud_repository == "purewi/oc2-map-repository"
    assert config.github_repository == "purewi/oc2-map-repository"
    assert window.table.columnCount() == 6
    assert window.table.columnWidth(3) >= 100
    assert window.version_label.text() == f"v{__version__}"
    window.close()
    app.processEvents()


def test_public_window_is_clearly_labeled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OC2_CLIENT_VARIANT", "public")
    app = QApplication.instance() or QApplication([])
    config = ClientConfig(cache_directory=tmp_path / "cache", auto_check_updates=False)
    config.ensure_directories()
    window = MainWindow(config)
    assert "公开版" in window.windowTitle()
    assert not window.manager.downloads.cos_enabled
    window.close()
    app.processEvents()


def test_details_dialog_has_no_preview() -> None:
    app = QApplication.instance() or QApplication([])
    entry = MapEntry(
        id="local-example",
        name="示例地图",
        author="本地",
        version="-",
        local_version="未知",
        size=0,
        updated_at="-",
        status=MapStatus.LOCAL_ONLY,
        metadata=None,
    )
    dialog = DetailsDialog(entry, None)
    assert not hasattr(dialog, "image")
    dialog.close()
    app.processEvents()


def test_select_uninstalled_does_not_select_installed(
    tmp_path, monkeypatch
) -> None:
    app = QApplication.instance() or QApplication([])
    config = ClientConfig(cache_directory=tmp_path / "cache", auto_check_updates=False)
    config.ensure_directories()
    window = MainWindow(config)

    window.entries = []
    for index in range(32):
        metadata = MapMetadata(
            id=f"map-{index:012d}",
            name=f"Map {index}",
            author="Author",
            version="1.0",
            preview_url="https://example.test/preview.webp",
            download_url="https://example.test/map.zip",
            file_name="map.zip",
            size=1,
            sha256="a" * 64,
            updated_at=datetime.now(UTC),
        )
        window.entries.append(
            MapEntry(
                id=metadata.id,
                name=metadata.name,
                author=metadata.author,
                version=metadata.version,
                local_version="-",
                size=metadata.size,
                updated_at="-",
                status=MapStatus.NOT_INSTALLED,
                metadata=metadata,
            )
        )
    window.entries[0].local_version = "1.0"
    window.populate_table()
    window.select_rows("uninstalled")
    assert len(window._selected_entries()) == 31
    assert window.entries[0] not in window._selected_entries()
    window.close()
    app.processEvents()


def test_details_always_uses_current_cloud_version() -> None:
    app = QApplication.instance() or QApplication([])
    current = MapMetadata(
        id="map-support-example", name="Support map", author="Author", version="2.0",
        download_url="https://example.test/v2.zip", file_name="v2.zip", size=2,
        sha256="b" * 64, updated_at=datetime.now(UTC), embedded_version="2.0",
        support_package=SupportPackage(
            version="2.0", download_url="https://example.test/support.zip",
            file_name="support.zip", size=1, sha256="c" * 64,
        ),
        compatibility_version=MapVersion(
            version="1.0", source_version="1.0", updated_at=datetime.now(UTC),
            download_url="https://example.test/v1.zip", file_name="v1.zip", size=1,
            sha256="a" * 64,
        ),
    )
    entry = MapEntry(
        id=current.id, name=current.name, author=current.author, version="2.0",
        local_version="-", size=2, updated_at="-", status=MapStatus.NOT_INSTALLED,
        metadata=current,
    )
    dialog = DetailsDialog(entry, None)
    chosen = dialog.selected_metadata()
    assert chosen is current
    assert not hasattr(dialog, "version_box")
    dialog.close()
    app.processEvents()


def test_main_table_shows_current_cloud_version_without_selector(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    app = QApplication.instance() or QApplication([])
    config = ClientConfig(cache_directory=tmp_path / "cache", auto_check_updates=False)
    config.ensure_directories()
    window = MainWindow(config)
    current = MapMetadata(
        id="map-support-example", name="Support map", author="Author", version="2.0",
        download_url="https://example.test/v2.zip", file_name="v2.zip", size=2,
        sha256="b" * 64, updated_at=datetime.now(UTC), embedded_version="2.0",
        support_package=SupportPackage(
            version="2.0", download_url="https://example.test/support.zip",
            file_name="support.zip", size=1, sha256="c" * 64,
        ),
        compatibility_version=MapVersion(
            version="1.0", source_version="1.0", updated_at=datetime.now(UTC),
            download_url="https://example.test/v1.zip", file_name="v1.zip", size=1,
            sha256="a" * 64,
        ),
    )
    window.entries = [MapEntry(
        id=current.id, name=current.name, author=current.author, version="2.0",
        local_version="-", size=2, updated_at="-", status=MapStatus.NOT_INSTALLED,
        metadata=current,
    )]
    window.populate_table()
    assert window.table.cellWidget(0, 3) is None
    assert window.table.item(0, 3).text() == "2.0"
    assert window._metadata_for_entry(window.entries[0]) is current
    window.close()
    app.processEvents()

