from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from client.config import ClientConfig, levels_directory_from_game
from client.models import MapEntry


class SettingsDialog(QDialog):
    def __init__(self, config: ClientConfig, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(560)
        self.config = config
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.game_directory = QLineEdit(str(config.game_directory or ""))
        self.auto_check = QCheckBox("启动时检查地图更新")
        self.auto_check.setChecked(config.auto_check_updates)
        form.addRow(
            "游戏目录",
            _path_row(self.game_directory, self, "选择 Overcooked! 2 游戏目录"),
        )
        form.addRow("", self.auto_check)
        hint = QLabel(
            "通常会自动识别，无需修改。若未识别，请选择 Steam 中的 "
            "Overcooked! 2 游戏目录；地图安装位置会自动设置。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#6b7280")
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def accept(self) -> None:
        game = self.game_directory.text().strip()
        self.config.game_directory = Path(game).expanduser().resolve() if game else None
        self.config.maps_directory = (
            levels_directory_from_game(self.config.game_directory)
            if self.config.game_directory is not None
            else None
        )
        self.config.auto_check_updates = self.auto_check.isChecked()
        self.config.save()
        super().accept()


class DetailsDialog(QDialog):
    install_requested = Signal(object)
    download_requested = Signal(object)
    remove_requested = Signal(object)

    def __init__(self, entry: MapEntry, preview: Path | None, parent=None) -> None:
        super().__init__(parent)
        self.entry = entry
        self.setWindowTitle(entry.name)
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        metadata = entry.metadata
        form = QFormLayout()
        form.addRow("名称", QLabel(entry.name))
        form.addRow("作者", QLabel(entry.author))
        form.addRow("云端版本", QLabel(entry.version))
        if metadata and metadata.availability_notice and not metadata.support_package:
            notice = QLabel(metadata.availability_notice)
            notice.setTextFormat(Qt.PlainText)
            notice.setWordWrap(True)
            notice.setStyleSheet("color:#b45309;font-weight:600")
            layout.addWidget(notice)
        if metadata and _has_conflict_notice(metadata.tags):
            conflict = QLabel("⚠ 此地图有共存/互斥说明，请先阅读下方描述再安装。")
            conflict.setTextFormat(Qt.PlainText)
            conflict.setWordWrap(True)
            conflict.setStyleSheet("color:#b91c1c;font-weight:600")
            layout.addWidget(conflict)
        self.support_notice = QLabel()
        self.support_notice.setTextFormat(Qt.PlainText)
        self.support_notice.setWordWrap(True)
        self.support_notice.setStyleSheet("color:#b45309;font-weight:600")
        layout.addWidget(self.support_notice)
        self._update_support_notice()
        if entry.local_version != "-":
            form.addRow("已安装版本", QLabel(entry.local_version))
        form.addRow("状态", QLabel(entry.status.value))
        if metadata:
            description = QLabel(metadata.description or "-")
            description.setWordWrap(True)
            form.addRow("描述", description)
            form.addRow("更新时间", QLabel(entry.updated_at))
            form.addRow("文件大小", QLabel(_size(entry.size)))
            form.addRow("标签", QLabel(", ".join(metadata.tags) or "-"))
            changelog = QLabel(metadata.changelog or "-")
            changelog.setWordWrap(True)
            form.addRow("更新说明", changelog)
        layout.addLayout(form)
        actions = QHBoxLayout()
        if metadata:
            install = QPushButton("更新" if entry.local_version != "-" else "安装")
            install.clicked.connect(lambda: self.install_requested.emit(self.selected_metadata()))
            actions.addWidget(install)
            redownload = QPushButton("重新下载")
            redownload.clicked.connect(lambda: self.download_requested.emit(self.selected_metadata()))
            actions.addWidget(redownload)
        if entry.install_path:
            remove = QPushButton("删除本地地图")
            remove.setStyleSheet("color:#b91c1c")
            remove.clicked.connect(lambda: self.remove_requested.emit(entry))
            actions.addWidget(remove)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        actions.addStretch()
        actions.addWidget(close)
        layout.addLayout(actions)

    def selected_metadata(self):
        return self.entry.metadata

    def _update_support_notice(self):
        metadata = self.selected_metadata()
        if metadata is None:
            self.support_notice.clear()
            self.support_notice.hide()
        elif metadata.support_package:
            profile = _support_profile(metadata.tags)
            label = {
                "qichi": "七池专用前置文件",
                "jonny": "Jonny 专用前置文件",
            }.get(profile, "统一前置文件")
            self.support_notice.setText(f"⚠ 此地图需要{label}，下载器会自动安装。")
            self.support_notice.show()
        elif metadata.support_required_notice:
            self.support_notice.setText("⚠ " + metadata.support_required_notice)
            self.support_notice.show()
        else:
            self.support_notice.clear()
            self.support_notice.hide()

def _path_row(edit: QLineEdit, parent: QDialog, title: str):
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    button = QPushButton("浏览…")

    def choose() -> None:
        selected = QFileDialog.getExistingDirectory(parent, title, edit.text())
        if selected:
            edit.setText(selected)

    button.clicked.connect(choose)
    layout.addWidget(edit)
    layout.addWidget(button)
    return container


def _size(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024 or unit == "GB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


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
