from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from client import __version__
from client.config import ClientConfig
from client.map_manager import MapManager
from client.models import DownloadProgress, MapEntry, MapStatus
from client.self_update import (
    ClientSelfUpdater,
    ClientUpdateError,
    ClientUpdateInfo,
    resolve_client_update,
)
from client.variant import current_variant

from .dialogs import DetailsDialog, SettingsDialog
from .workers import Worker


logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, config: ClientConfig) -> None:
        super().__init__()
        self.config = config
        self.manager = MapManager(config)
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(3)
        self._workers: set[Worker] = set()
        self._closing = False
        self._manager_closed = False
        self.entries: list[MapEntry] = []
        self.busy = False
        self._framework_prompted: str | None = None
        self._client_update_prompted: str | None = None
        self._pending_client_update: ClientUpdateInfo | None = None
        self.variant = current_variant()
        self.client_updater = ClientSelfUpdater(
            config.cache_directory,
            timeout_seconds=config.timeout_seconds,
            retries=config.retries,
        )
        self.setWindowTitle(f"胡闹厨房自制地图 - {self.variant.display_name}")
        self.resize(1040, 720)
        self._build_ui()
        self._apply_style()
        if config.auto_check_updates and config.cloud_repository:
            self.refresh_catalog()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        header = QHBoxLayout()
        title = QLabel("胡闹厨房自制地图")
        title.setObjectName("title")
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索地图名称或作者…")
        self.search.textChanged.connect(self.apply_filter)
        self.filter_box = QComboBox()
        self.filter_box.addItems(("全部", "已安装", "未安装", "有更新", "最新"))
        self.filter_box.currentTextChanged.connect(self.apply_filter)
        self.tag_box = QComboBox()
        self.tag_box.addItem("全部标签")
        self.tag_box.currentTextChanged.connect(self.apply_filter)
        refresh = QPushButton("检查更新")
        refresh.clicked.connect(self.refresh_catalog)
        self.framework_button = QPushButton("安装地图框架")
        self.framework_button.setEnabled(False)
        self.framework_button.clicked.connect(self.install_framework)
        self.support_button = QPushButton("安装通用前置文件")
        self.support_button.setEnabled(False)
        self.support_button.clicked.connect(self.install_support_bundle)
        settings = QPushButton("设置")
        settings.clicked.connect(self.open_settings)
        header.addWidget(title)
        variant_badge = QLabel(self.variant.badge_text)
        variant_badge.setObjectName(
            "cosVariantBadge" if self.variant.cos_enabled else "publicVariantBadge"
        )
        header.addWidget(variant_badge)
        header.addStretch()
        header.addWidget(self.search, 2)
        header.addWidget(self.filter_box)
        header.addWidget(self.tag_box)
        header.addWidget(refresh)
        header.addWidget(self.framework_button)
        header.addWidget(self.support_button)
        header.addWidget(settings)
        layout.addLayout(header)

        framework_row = QHBoxLayout()
        self.framework_status_label = QLabel("地图框架　本地：检测中 · 云端：检查中")
        self.framework_status_label.setObjectName("frameworkStatus")
        framework_row.addWidget(self.framework_status_label)
        framework_row.addStretch()
        layout.addLayout(framework_row)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ("选择", "地图", "作者", "云端版本", "大小", "状态")
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.doubleClicked.connect(self.open_details)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(1, QHeaderView.Stretch)
        header_view.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.Interactive)
        self.table.setColumnWidth(3, 120)
        layout.addWidget(self.table)

        actions = QHBoxLayout()
        for label, handler in (
            ("全选", lambda: self.select_rows("all")),
            ("取消选择", lambda: self.select_rows("none")),
            ("选择可更新", lambda: self.select_rows("updates")),
            ("选择未安装", lambda: self.select_rows("uninstalled")),
            ("安装 / 更新", self.install_selected),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            actions.addWidget(button)
        self.cancel_button = QPushButton("取消当前下载")
        self.cancel_button.clicked.connect(self.manager.downloads.cancel)
        self.cancel_button.setEnabled(False)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        layout.addLayout(actions)

        status_row = QHBoxLayout()
        self.status_label = QLabel("就绪")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setMaximumWidth(360)
        status_row.addWidget(self.status_label)
        status_row.addStretch()
        status_row.addWidget(self.progress)
        self.version_label = QLabel(f"v{__version__}")
        self.version_label.setObjectName("clientVersion")
        self.version_label.setToolTip("客户端版本")
        status_row.addWidget(self.version_label)
        layout.addLayout(status_row)
        self.setCentralWidget(root)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background:#f5f7fb; color:#172033; font-size:14px; }
            QLabel#title { font-size:24px; font-weight:700; color:#b45309; }
            QLabel#frameworkStatus { color:#475569; padding:1px 2px 5px 2px; }
            QLabel#clientVersion { color:#94a3b8; font-size:11px; padding-left:6px; }
            QLabel#cosVariantBadge { color:#075985; background:#e0f2fe; border:1px solid #7dd3fc; border-radius:8px; padding:4px 8px; font-weight:600; }
            QLabel#publicVariantBadge { color:#475569; background:#f1f5f9; border:1px solid #cbd5e1; border-radius:8px; padding:4px 8px; font-weight:600; }
            QLineEdit, QComboBox { background:white; border:1px solid #cbd5e1; border-radius:7px; padding:8px; }
            QPushButton { background:#f59e0b; color:#172033; border:0; border-radius:7px; padding:8px 13px; font-weight:600; }
            QPushButton:hover { background:#fbbf24; }
            QPushButton:disabled { background:#cbd5e1; color:#64748b; }
            QTableWidget { background:white; border:1px solid #dbe2ea; border-radius:8px; gridline-color:#eef2f7; }
            QHeaderView::section { background:#24324e; color:white; padding:8px; border:0; }
            QProgressBar { background:#e2e8f0; border:0; border-radius:6px; text-align:center; }
            QProgressBar::chunk { background:#f59e0b; border-radius:6px; }
            """
        )

    def refresh_catalog(self) -> None:
        if self.busy:
            return
        if self.thread_pool.activeThreadCount() > 0:
            self.status_label.setText("预览正在加载，请稍候再检查更新")
            return
        if not self.config.cloud_repository:
            self.open_settings()
            return
        self._set_busy(True, "正在获取更新…")
        worker = Worker(lambda *, progress: self.manager.refresh())
        worker.signals.result.connect(self._catalog_loaded)
        worker.signals.error.connect(self._show_error)
        worker.signals.finished.connect(lambda: self._set_busy(False))
        self._start_worker(worker)

    def _catalog_loaded(self, result: object) -> None:
        if self._closing:
            return
        self.entries, cached = result  # type: ignore[misc]
        updates = sum(item.status == MapStatus.UPDATE_AVAILABLE for item in self.entries)
        suffix = "（离线缓存）" if cached else ""
        self.status_label.setText(
            f"共 {len(self.entries)} 张地图，发现 {updates} 张可以更新 {suffix}".strip()
        )
        current_tag = self.tag_box.currentText()
        tags = sorted(
            {tag for entry in self.entries if entry.metadata for tag in entry.metadata.tags},
            key=str.casefold,
        )
        self.tag_box.blockSignals(True)
        self.tag_box.clear()
        self.tag_box.addItems(["全部标签", *tags])
        if current_tag in tags:
            self.tag_box.setCurrentText(current_tag)
        self.tag_box.blockSignals(False)
        self.populate_table()
        self._update_framework_button()
        self._update_support_button()
        QTimer.singleShot(0, self._prompt_client_update)

    def _prompt_client_update(self) -> None:
        if self._closing:
            return
        try:
            info = resolve_client_update(
                self.manager.catalog.client,
                variant=self.variant.key,
                local_version=__version__,
            )
        except ClientUpdateError as exc:
            logger.warning("[CLIENT_UPDATE] 更新信息无效: %s", exc)
            QTimer.singleShot(0, self._prompt_framework_update)
            return
        if info is None or not self.client_updater.can_apply:
            QTimer.singleShot(0, self._prompt_framework_update)
            return
        if self.busy:
            QTimer.singleShot(100, self._prompt_client_update)
            return
        if self._client_update_prompted == info.version:
            QTimer.singleShot(0, self._prompt_framework_update)
            return
        self._client_update_prompted = info.version
        notes = "\n".join(f"• {item}" for item in info.release_notes)
        detail = (
            f"当前版本：{__version__}\n最新版本：{info.version}"
            + (f"\n\n更新内容：\n{notes}" if notes else "")
        )
        logger.info(
            "[CLIENT_UPDATE] local_version=%s remote_version=%s mandatory=%s",
            __version__,
            info.version,
            info.mandatory,
        )
        if info.mandatory:
            QMessageBox.information(
                self,
                "需要更新客户端",
                detail + "\n\n当前版本已经过旧，需要更新后才能继续使用。",
            )
            self._start_client_update(info)
            return
        answer = QMessageBox.question(
            self,
            "发现客户端新版本",
            detail + "\n\n是否立即更新？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self._start_client_update(info)
        else:
            QTimer.singleShot(0, self._prompt_framework_update)

    def _start_client_update(self, info: ClientUpdateInfo) -> None:
        if self.busy:
            QTimer.singleShot(100, lambda: self._start_client_update(info))
            return
        self._pending_client_update = info
        self._set_busy(True, "正在下载客户端更新…")
        worker = Worker(
            lambda *, progress: self.client_updater.download(info, progress)
        )
        worker.signals.progress.connect(self._progress_changed)
        worker.signals.result.connect(self._client_update_downloaded)
        worker.signals.error.connect(self._client_update_failed)
        worker.signals.finished.connect(lambda: self._set_busy(False))
        self._start_worker(worker)

    def _client_update_downloaded(self, path: object) -> None:
        info = self._pending_client_update
        if info is None or not isinstance(path, Path):
            self._client_update_failed("客户端更新状态丢失")
            return
        self.progress.setValue(100)
        QMessageBox.information(
            self,
            "客户端更新已下载",
            "程序将自动关闭、替换并重新启动。用户设置和地图文件不会被覆盖。",
        )
        try:
            self.client_updater.start_apply(path, info)
        except Exception as exc:
            self._client_update_failed(f"{type(exc).__name__}: {exc}")
            return
        self.status_label.setText("正在重启并应用客户端更新…")
        logger.info("[CLIENT_UPDATE] launching helper version=%s", info.version)
        self.manager.close()
        self._manager_closed = True
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _client_update_failed(self, message: str) -> None:
        info = self._pending_client_update
        self.status_label.setText("客户端更新失败，当前版本保持不变")
        logger.warning("[CLIENT_UPDATE] failed error=%s", message)
        if info is not None and info.mandatory:
            answer = QMessageBox.critical(
                self,
                "强制更新失败",
                f"{message}\n\n旧客户端没有被替换。是否重试？",
                QMessageBox.Retry | QMessageBox.Close,
                QMessageBox.Retry,
            )
            if answer == QMessageBox.Retry:
                QTimer.singleShot(0, lambda: self._start_client_update(info))
            else:
                app = QApplication.instance()
                if app is not None:
                    app.quit()
            return
        QMessageBox.warning(
            self,
            "客户端更新失败",
            f"{message}\n\n当前客户端仍可继续使用。",
        )
        QTimer.singleShot(0, self._prompt_framework_update)

    def _prompt_framework_update(self) -> None:
        if self._closing:
            return
        status = self.manager.framework_status()
        if not status.cloud_version or status.matches or status.local_newer:
            return
        if self.busy:
            QTimer.singleShot(100, self._prompt_framework_update)
            return
        if self._framework_prompted == status.cloud_version:
            return
        self._framework_prompted = status.cloud_version
        answer = QMessageBox.question(
            self, "请优先更新地图框架",
            f"本地框架：{status.local_version or '未安装 / 版本未知'}\n"
            f"云端框架：{status.cloud_version}\n\n"
            "建议先更新框架，再安装地图，以免新地图无法运行。现在更新框架吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self.install_framework()

    def populate_table(self) -> None:
        self.table.setRowCount(0)
        for entry in self.entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setRowHeight(row, 44)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            check.setCheckState(Qt.Checked if entry.selected else Qt.Unchecked)
            check.setData(Qt.UserRole, entry.id)
            self.table.setItem(row, 0, check)
            support_line = ""
            if entry.metadata and entry.metadata.support_package:
                profile = _support_profile(entry.metadata.tags)
                support_line = {
                    "qichi": "\n⚠ 需七池专用前置（自动安装）",
                    "jonny": "\n⚠ 需 Jonny 专用前置（自动安装）",
                }.get(profile, "\n⚠ 需统一前置文件（自动安装）")
            elif entry.metadata and entry.metadata.support_required_notice:
                support_line = "\n⚠ 需额外支持文件（云端暂缺）"
            if entry.metadata and _has_conflict_notice(entry.metadata.tags):
                support_line += "\n⚠ 有地图互斥说明（点开查看）"
            values = (entry.name + support_line, entry.author)
            for column, value in enumerate(values, start=1):
                self.table.setItem(row, column, QTableWidgetItem(value))

            self.table.setItem(row, 3, QTableWidgetItem(entry.version))

            trailing = (_size(entry.size), entry.status.value)
            for column, value in enumerate(trailing, start=4):
                if column == 5 and entry.metadata and entry.metadata.availability_notice and not entry.metadata.support_package:
                    value = "旧版可下载 · 新版请找作者"
                item = QTableWidgetItem(value)
                if entry.metadata and entry.metadata.availability_notice and not entry.metadata.support_package:
                    item.setToolTip(entry.metadata.availability_notice)
                elif entry.metadata and entry.metadata.support_required_notice:
                    item.setToolTip(entry.metadata.support_required_notice)
                if column == 5:
                    item.setForeground(QColor(_status_color(entry.status)))
                self.table.setItem(row, column, item)
        self.apply_filter()

    def apply_filter(self) -> None:
        query = self.search.text().strip().casefold()
        selected_filter = self.filter_box.currentText()
        selected_tag = self.tag_box.currentText()
        for row in range(self.table.rowCount()):
            entry = self._entry_for_row(row)
            if entry is None:
                continue
            haystack = f"{entry.name} {entry.author} {' '.join(entry.metadata.tags) if entry.metadata else ''}".casefold()
            matches_text = not query or query in haystack
            matches_status = (
                selected_filter == "全部"
                or (selected_filter == "已安装" and entry.local_version != "-")
                or (selected_filter == "未安装" and entry.status == MapStatus.NOT_INSTALLED)
                or (selected_filter == "有更新" and entry.status == MapStatus.UPDATE_AVAILABLE)
                or (selected_filter == "最新" and entry.status == MapStatus.LATEST)
            )
            matches_tag = (
                selected_tag == "全部标签"
                or (entry.metadata is not None and selected_tag in entry.metadata.tags)
            )
            self.table.setRowHidden(row, not (matches_text and matches_status and matches_tag))

    def select_rows(self, mode: str) -> None:
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue
            entry = self._entry_for_row(row)
            checked = (mode == "uninstalled" and entry is not None and entry.metadata is not None and entry.local_version == "-") or mode == "all" or (
                mode == "updates" and entry is not None and entry.status == MapStatus.UPDATE_AVAILABLE
            )
            self.table.item(row, 0).setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def install_selected(self) -> None:
        self._start_batch(install=True)

    def install_framework(self) -> None:
        if self.busy:
            return
        if self.manager.catalog.framework is None:
            QMessageBox.information(self, "地图框架", "请先检查更新，获取地图框架信息。")
            return
        if self.config.game_directory is None:
            QMessageBox.information(
                self,
                "选择游戏目录",
                "请先在设置中选择 Overcooked! 2 游戏目录。",
            )
            self.open_settings()
            return
        answer = QMessageBox.question(
            self,
            "安装地图框架",
            "安装前请关闭游戏。现有自制地图和存档会保留，是否继续？",
        )
        if answer != QMessageBox.Yes:
            return
        self._set_busy(True, "正在准备地图框架…")
        worker = Worker(
            lambda *, progress: self.manager.install_framework(
                progress=progress,
                phase=lambda status: progress(("framework", status)),
            )
        )
        worker.signals.progress.connect(self._progress_changed)
        worker.signals.result.connect(self._framework_installed)
        worker.signals.error.connect(self._show_error)
        worker.signals.finished.connect(lambda: self._set_busy(False))
        self._start_worker(worker)

    def install_support_bundle(self) -> None:
        if self.busy:
            return
        if self.manager.catalog.support_bundle is None:
            QMessageBox.information(self, "通用前置文件", "云端暂未提供通用前置文件。")
            return
        if self.config.game_directory is None:
            QMessageBox.information(
                self, "选择游戏目录", "请先在设置中选择 Overcooked! 2 游戏目录。"
            )
            self.open_settings()
            return
        if QMessageBox.question(
            self,
            "安装通用前置文件",
            "安装前请关闭游戏。新版统一前置及 Jonny 前置会部署到 "
            "BepInEx\\plugins，并清理旧版散落前置和已知地图目录中的旧 runtime。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        ) != QMessageBox.Yes:
            return
        self._set_busy(True, "正在准备通用前置文件…")
        worker = Worker(
            lambda *, progress: self.manager.install_support_bundle(
                progress=progress,
                phase=lambda status: progress(("support", status)),
            )
        )
        worker.signals.progress.connect(self._progress_changed)
        worker.signals.result.connect(self._support_installed)
        worker.signals.error.connect(self._show_error)
        worker.signals.finished.connect(lambda: self._set_busy(False))
        self._start_worker(worker)

    def _support_installed(self, _paths: object) -> None:
        self.progress.setValue(100)
        self.status_label.setText("通用前置文件安装完成")
        QMessageBox.information(
            self,
            "安装完成",
            "新版统一前置及 Jonny 前置已安装到 BepInEx\\plugins，旧版已知前置已清理。",
        )

    def _update_support_button(self) -> None:
        self.support_button.setEnabled(
            self.manager.catalog.support_bundle is not None and not self.busy
        )

    def _framework_installed(self, path: object) -> None:
        self.progress.setValue(100)
        self.status_label.setText("地图框架安装完成")
        self._update_framework_button()
        QMessageBox.information(self, "安装完成", f"地图框架已安装到：\n{path}")

    def _update_framework_button(self) -> None:
        framework = self.manager.catalog.framework
        self.framework_button.setEnabled(framework is not None and not self.busy)
        status = self.manager.framework_status()
        local = f"v{status.local_version}" if status.local_version else (
            "版本未知" if status.installed else "未安装"
        )
        cloud = f"v{status.cloud_version}" if status.cloud_version else "未获取"
        if framework is None:
            self.framework_button.setText("安装地图框架")
            result = "等待云端信息"
        elif status.matches:
            self.framework_button.setText("重新安装框架")
            result = "版本已匹配"
        elif status.local_newer:
            self.framework_button.setText("重新安装框架")
            result = "本地版本较新"
        elif status.installed:
            self.framework_button.setText("一键更新框架")
            result = "发现新版本"
        else:
            self.framework_button.setText("一键安装框架")
            result = "可以安装"
        self.framework_status_label.setText(
            f"地图框架　本地：{local}　·　云端：{cloud}　·　{result}"
        )

    def _start_batch(self, *, install: bool) -> None:
        if self.busy:
            return
        selected = self._selected_entries()
        if not selected:
            QMessageBox.information(self, "没有选择", "请先勾选至少一张云端地图。")
            return
        if install and self.config.maps_directory is None:
            QMessageBox.warning(self, "缺少地图目录", "请先在设置中选择 OC2DIYLevel\\levels 目录。")
            return
        selected_metadata = {
            entry.id: self._metadata_for_entry(entry) for entry in selected
        }
        missing_support = [
            entry for entry in selected
            if selected_metadata[entry.id].support_required_notice
            and selected_metadata[entry.id].support_package is None
        ]
        if missing_support:
            metadata = selected_metadata[missing_support[0].id]
            QMessageBox.warning(self, "支持文件尚未收录", metadata.support_required_notice)
            return
        notices = [
            f"{entry.name}：{selected_metadata[entry.id].availability_notice}"
            for entry in selected
            if selected_metadata[entry.id].availability_notice
            and selected_metadata[entry.id].support_package is None
        ]
        if notices and QMessageBox.question(
            self, "下载版本提醒", "\n\n".join(notices) + "\n\n仍要下载当前提供的旧版吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        conflict_notices = [
            f"{entry.name}：{selected_metadata[entry.id].description}"
            for entry in selected
            if _has_conflict_notice(selected_metadata[entry.id].tags)
        ]
        if conflict_notices and QMessageBox.question(
            self,
            "地图互斥提醒",
            "\n\n".join(conflict_notices)
            + "\n\n请确认已按说明移除冲突地图/前置。仍要继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        needs_support = any(
            selected_metadata[entry.id].support_package for entry in selected
        )
        if needs_support and QMessageBox.question(
            self,
            "需要地图前置文件",
            "所选地图需要统一或作者专用前置文件。下载器会按地图类型自动选择，"
            "相同文件只下载一次。安装前请关闭游戏，是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        ) != QMessageBox.Yes:
            return
        self._set_busy(True, "正在处理选中的地图…")
        self.cancel_button.setEnabled(True)

        def batch(*, progress):
            results = []
            for entry in selected:
                metadata = selected_metadata[entry.id]
                if install:
                    results.append(
                        self.manager.install(
                            metadata,
                            progress=progress,
                            phase=lambda status, map_id=entry.id: progress(
                                ("status", map_id, status)
                            ),
                        )
                    )
                else:
                    progress(("status", entry.id, "下载中"))
                    results.append(self.manager.download(metadata, progress=progress))
            return results

        worker = Worker(batch)
        worker.signals.progress.connect(self._progress_changed)
        worker.signals.result.connect(lambda _: self._batch_completed(install))
        worker.signals.error.connect(self._show_error)
        worker.signals.finished.connect(lambda: self._set_busy(False))
        self._start_worker(worker)

    def _batch_completed(self, installed: bool) -> None:
        if self._closing:
            return
        action = "安装/更新" if installed else "下载"
        self.status_label.setText(f"选中地图{action}完成")
        self.entries = self.manager.entries()
        self.populate_table()

        self.progress.setValue(100)
        QMessageBox.information(
            self, f"地图{action}完成",
            "选中地图已下载并安装完成，可以进入游戏游玩。" if installed
            else "地图已下载完成。安装后即可进入游戏游玩。",
        )

    def _progress_changed(self, value: object) -> None:
        if self._closing:
            return
        if isinstance(value, tuple) and len(value) == 2 and value[0] == "framework":
            self.status_label.setText(str(value[1]))
            return
        if isinstance(value, tuple) and len(value) == 2 and value[0] == "support":
            self.status_label.setText(str(value[1]))
            return
        if isinstance(value, tuple) and len(value) == 3 and value[0] == "status":
            self._set_row_status(str(value[1]), str(value[2]))
            self.status_label.setText(f"{value[2]}：{value[1]}")
            return
        if not isinstance(value, DownloadProgress):
            return
        self._set_row_status(value.map_id, "下载中")
        self.progress.setValue(value.percent)
        display_name = {
            "oc2diylevel-framework": "地图框架",
            "oc2-unified-support": "通用前置文件",
            "client-update": "客户端更新",
        }.get(value.map_id, value.map_id)
        if value.map_id.startswith("oc2-support-"):
            display_name = "地图前置文件"
        self.status_label.setText(
            f"下载 {display_name}：{_size(value.downloaded)} / {_size(value.total)}，"
            f"{_size(int(value.bytes_per_second))}/s"
        )

    def open_settings(self) -> None:
        if self.busy or self.thread_pool.activeThreadCount() > 0:
            self.status_label.setText("后台任务正在完成，请稍候再打开设置")
            return
        dialog = SettingsDialog(self.config, self)
        if dialog.exec():
            self.manager.close()
            self.manager = MapManager(self.config)
            self.status_label.setText("设置已保存")
            if self.config.cloud_repository:
                self.refresh_catalog()

    def open_details(self) -> None:
        row = self.table.currentRow()
        entry = self._entry_for_row(row)
        if entry is None:
            return
        dialog = DetailsDialog(entry, None, self)
        dialog.install_requested.connect(lambda metadata: (dialog.accept(), self._install_one(entry, metadata)))
        dialog.download_requested.connect(
            lambda metadata: (dialog.accept(), self._download_metadata(entry, metadata, force=True))
        )
        dialog.remove_requested.connect(lambda selected: self._remove_one(dialog, selected))
        dialog.exec()

    def _install_one(self, entry: MapEntry, metadata=None) -> None:
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(
                Qt.Checked if self._entry_for_row(row) is entry else Qt.Unchecked
            )
        self.install_selected()

    def _download_one(self, entry: MapEntry, *, force: bool) -> None:
        self._download_metadata(entry, entry.metadata, force=force)

    def _download_metadata(self, entry: MapEntry, metadata, *, force: bool) -> None:
        if self.busy or metadata is None:
            return
        if metadata.support_required_notice and metadata.support_package is None:
            QMessageBox.warning(self, "支持文件尚未收录", metadata.support_required_notice)
            return
        if metadata.availability_notice and not metadata.support_package and QMessageBox.question(
            self, "下载版本提醒", metadata.availability_notice + "\n\n仍要下载旧版吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self._set_busy(True, "正在重新下载地图…")
        worker = Worker(
            lambda *, progress: self.manager.download(
                metadata,
                force=force,
                progress=progress,
            )
        )
        worker.signals.progress.connect(self._progress_changed)
        worker.signals.result.connect(lambda _: self._batch_completed(False))
        worker.signals.error.connect(self._show_error)
        worker.signals.finished.connect(lambda: self._set_busy(False))
        self._start_worker(worker)

    def _remove_one(self, dialog: DetailsDialog, entry: MapEntry) -> None:
        answer = QMessageBox.question(
            dialog,
            "删除本地地图",
            "地图目录将移动到 Windows 回收站，是否继续？",
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.manager.remove(entry)
            dialog.accept()
            self.entries = self.manager.entries()
            self.populate_table()
        except Exception as exc:
            QMessageBox.critical(dialog, "删除失败", str(exc))

    def _selected_entries(self) -> list[MapEntry]:
        result = []
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).checkState() == Qt.Checked:
                entry = self._entry_for_row(row)
                if entry is not None and entry.metadata is not None:
                    result.append(entry)
        return result

    def _entry_for_row(self, row: int) -> MapEntry | None:
        if row < 0 or row >= self.table.rowCount():
            return None
        item = self.table.item(row, 0)
        map_id = item.data(Qt.UserRole) if item else None
        return next((entry for entry in self.entries if entry.id == map_id), None)

    def _metadata_for_entry(self, entry: MapEntry):
        return entry.metadata

    def _show_error(self, message: str) -> None:
        if self._closing:
            return
        self.status_label.setText("操作失败")
        for entry in self._selected_entries():
            self._set_row_status(entry.id, "失败")
        QMessageBox.critical(self, "操作失败", message)

    def _set_row_status(self, map_id: str, text: str) -> None:
        for row in range(self.table.rowCount()):
            entry = self._entry_for_row(row)
            if entry is not None and entry.id == map_id:
                item = self.table.item(row, 5)
                if item is not None:
                    item.setText(text)
                    item.setForeground(QColor("#b91c1c" if text == "失败" else "#b45309"))
                return

    def _set_busy(self, busy: bool, message: str | None = None) -> None:
        if self._closing:
            return
        self.busy = busy
        self.cancel_button.setEnabled(busy)
        if not busy:
            self.cancel_button.setEnabled(False)
        if hasattr(self, "framework_button"):
            self.framework_button.setEnabled(
                not busy and self.manager.catalog.framework is not None
            )
        if hasattr(self, "support_button"):
            self._update_support_button()
        if message:
            self.status_label.setText(message)

    def _start_worker(self, worker: Worker) -> None:
        self._workers.add(worker)
        worker.signals.finished.connect(
            lambda current=worker: self._workers.discard(current)
        )
        self.thread_pool.start(worker)

    def closeEvent(self, event) -> None:
        if not self._closing:
            self._closing = True
            self.manager.downloads.cancel()
            self.client_updater.cancel()
            self.thread_pool.clear()
        if self.thread_pool.activeThreadCount() > 0:
            self.hide()
            event.ignore()
            QTimer.singleShot(100, self._finish_shutdown)
            return
        if not self._manager_closed:
            self.manager.close()
            self._manager_closed = True
        event.accept()

    def _finish_shutdown(self) -> None:
        if self.thread_pool.activeThreadCount() > 0:
            QTimer.singleShot(100, self._finish_shutdown)
            return
        if not self._manager_closed:
            self.manager.close()
            self._manager_closed = True
        app = QApplication.instance()
        if app is not None:
            app.quit()


def _size(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024 or unit == "GB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def _status_color(status: MapStatus) -> str:
    return {
        MapStatus.LATEST: "#15803d",
        MapStatus.UPDATE_AVAILABLE: "#b45309",
        MapStatus.NOT_INSTALLED: "#475569",
        MapStatus.FAILED: "#b91c1c",
        MapStatus.LOCAL_ONLY: "#7c3aed",
        MapStatus.LOCAL_NEWER: "#0369a1",
    }.get(status, "#172033")


def _support_profile(tags: list[str]) -> str | None:
    prefix = "support-profile:"
    return next(
        (
            tag[len(prefix) :].strip()
            for tag in tags
            if tag.casefold().startswith(prefix) and tag[len(prefix) :].strip()
        ),
        None,
    )


def _has_conflict_notice(tags: list[str]) -> bool:
    return any(tag == "⚠ 互斥提醒" for tag in tags)
