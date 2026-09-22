from __future__ import annotations

import html
import uuid
from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from client.maintenance import load_settings, save_json, settings_path, snapshot
from .main_window import MainWindow
from .workers import Worker


COLUMNS = [("author", "作者"), ("name", "地图"), ("key", "内部标识"),
           ("cloud", "云端包版本"), ("embedded", "包内版本"), ("observed", "群包文件名版本"),
           ("expected", "参考/目标版本（可改）"), ("status", "核对结果"),
           ("observed_file", "群文件"), ("cloud_file", "云端文件"),
           ("record_status", "处理状态"), ("record_id", "记录编号"), ("note", "说明 / 备注（可改）")]

EXTERNAL_COLUMNS = [
    ("author", "作者"), ("map_name", "地图 / 系列"), ("channel", "发布渠道"),
    ("url", "检查地址"), ("current_version", "上次看到的版本"),
    ("last_checked", "上次检查日期"), ("interval_days", "检查间隔（天）"),
    ("due", "检查状态"), ("note", "备注"),
]


class MaintenanceWindow(MainWindow):
    """Separate entry point; shares the client runtime without another large EXE."""
    def __init__(self, config):
        # Reuse only worker lifecycle/shutdown; no game scanning or installation.
        from PySide6.QtCore import QThreadPool
        QMainWindow.__init__(self)
        self.config = config
        self.settings = load_settings()
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(1)
        self._workers = set()
        self._closing = False
        self._manager_closed = False
        self.busy = False
        self.report = None
        self.setWindowTitle("厨房地图 · 维护版本核对表")
        self.resize(1450, 850)
        root = QWidget()
        layout = QVBoxLayout(root)
        top = QHBoxLayout()
        title = QLabel("地图版本核对表")
        title.setObjectName("title")
        top.addWidget(title)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索作者、地图、漏更、失败…")
        self.search.textChanged.connect(self.filter_rows)
        top.addWidget(self.search, 1)
        for text, callback in (("刷新核对", self.refresh_report), ("选择服务端目录", self.choose_server), ("导出核对表", self.export_report)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            top.addWidget(button)
        layout.addLayout(top)
        self.summary = QLabel("正在读取…")
        layout.addWidget(self.summary)
        hint = QLabel("每 5 分钟刷新云端与机器人数据库；仅代表已扫描记录。参考版本来自你提供的表，可双击修改；文件名版本不等于包内版本。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        tabs = QTabWidget()
        self.tabs = tabs
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([label for _, label in COLUMNS])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.itemChanged.connect(self.save_note)
        tabs.addTab(self.table, "地图版本对照")
        self.problems = QTableWidget(0, 4)
        self.problems.setHorizontalHeaderLabels(["记录编号", "群文件名", "处理状态", "失败 / 拒绝原因"])
        self.problems.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.problems.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.problems.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        tabs.addTab(self.problems, "失败和未识别文件")
        external_page = QWidget()
        external_layout = QVBoxLayout(external_page)
        external_actions = QHBoxLayout()
        for text, callback in (("打开检查地址", self.open_external_url), ("标记今天已检查", self.mark_external_checked),
                               ("新增记录", self.add_external), ("删除选中记录", self.delete_external)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            external_actions.addWidget(button)
        external_actions.addStretch(1)
        external_layout.addLayout(external_actions)
        external_hint = QLabel("这里专门记录不通过 QQ 群发布的作者。双击即可修改；选中一行后可打开链接或标记检查日期。")
        external_hint.setWordWrap(True)
        external_layout.addWidget(external_hint)
        self.external = QTableWidget(0, len(EXTERNAL_COLUMNS))
        self.external.setHorizontalHeaderLabels([label for _, label in EXTERNAL_COLUMNS])
        self.external.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.external.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.external.horizontalHeader().setSectionResizeMode(8, QHeaderView.Stretch)
        self.external.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.external.setAlternatingRowColors(True)
        self.external.itemChanged.connect(self.save_external)
        external_layout.addWidget(self.external)
        tabs.addTab(external_page, "外部作者检查")
        layout.addWidget(tabs)
        self.setCentralWidget(root)
        self._apply_style()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_report)
        self.timer.start(300_000)
        # External-source notes are local maintenance data and must remain
        # available even when the cloud or bot database is temporarily down.
        self.populate_external()
        self.refresh_report()

    def refresh_report(self):
        if self.busy or self._closing:
            return
        import copy
        self.busy = True
        self.summary.setText("正在核对云端目录与群文件记录…")
        worker = Worker(lambda *, progress: snapshot(self.config, copy.deepcopy(self.settings)))
        worker.signals.result.connect(self.loaded)
        worker.signals.error.connect(lambda message: self.summary.setText("核对失败：" + message))
        worker.signals.finished.connect(lambda: setattr(self, "busy", False))
        self._start_worker(worker)

    def loaded(self, report):
        if self._closing:
            return
        self.report = report
        rows = report["rows"]
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        from PySide6.QtGui import QColor
        for row, values in enumerate(rows):
            for column, (key, _) in enumerate(COLUMNS):
                # Preserve notes edited while this refresh was running.
                if key in {"expected", "note"} and key in self.settings["notes"].get(values["key"], {}):
                    values[key] = self.settings["notes"][values["key"]][key]
                item = QTableWidgetItem(str(values.get(key, "")))
                if key not in {"expected", "note"}:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setToolTip(str(values.get(key, "")))
                if key == "status":
                    item.setForeground(QColor("#15803d" if values[key] == "已同步" else "#b45309"))
                self.table.setItem(row, column, item)
        self.table.blockSignals(False)
        self.problems.setRowCount(len(report["problems"]))
        for row, record in enumerate(report["problems"]):
            for col, key in enumerate(("id", "file_name", "status", "last_error")):
                self.problems.setItem(row, col, QTableWidgetItem(str(record[key] or "")))
        self.populate_external()
        scans = [r["last_scan_at"] for r in report["scans"] if r["last_scan_at"]]
        warning = "（云端离线缓存）" if report["cached"] else ""
        self.summary.setText(f"云端 {report['cloud_maps']} 张 · 待核对 {sum(r['status'] != '已同步' for r in rows)} 项 · "
                             f"群记录 {report['records']} 条 · 刷新 {report['generated_at']} {warning}\n"
                             f"最近群扫描（UTC）：{max(scans) if scans else '未知'}　{report['database_error']} {report['cloud_error']}")
        self.filter_rows()

    def filter_rows(self):
        query = self.search.text().casefold().strip()
        for table in (self.table, self.problems, self.external):
            for row in range(table.rowCount()):
                text = " ".join(table.item(row, col).text() for col in range(table.columnCount()) if table.item(row, col))
                table.setRowHidden(row, bool(query and query not in text.casefold()))

    def save_note(self, item):
        if not self.report:
            return
        key = self.report["rows"][item.row()]["key"]
        field = COLUMNS[item.column()][0]
        if field in {"expected", "note"}:
            self.settings["notes"].setdefault(key, {})[field] = item.text()
            save_json(settings_path(), self.settings)
            self.report["rows"][item.row()][field] = item.text()
            self.summary.setText("参考版本/备注已保存；点击刷新核对重新计算状态。")

    @staticmethod
    def _external_due(record):
        checked = str(record.get("last_checked", "")).strip()
        try:
            interval = max(1, int(str(record.get("interval_days", "14")).strip()))
        except ValueError:
            return "间隔填写有误"
        try:
            next_check = date.fromisoformat(checked) + timedelta(days=interval)
        except ValueError:
            return "待检查" if not checked else "日期填写有误"
        return "已到期" if next_check <= date.today() else f"下次 {next_check.isoformat()}"

    def populate_external(self):
        records = self.settings.get("external_authors", {})
        self.external.blockSignals(True)
        self.external.setRowCount(len(records))
        from PySide6.QtGui import QColor
        for row, (record_id, record) in enumerate(records.items()):
            values = dict(record)
            values["due"] = self._external_due(record)
            for column, (key, _) in enumerate(EXTERNAL_COLUMNS):
                item = QTableWidgetItem(str(values.get(key, "")))
                if column == 0:
                    item.setData(Qt.UserRole, record_id)
                if key == "due":
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    if values["due"] in {"待检查", "已到期", "日期填写有误", "间隔填写有误"}:
                        item.setForeground(QColor("#b45309"))
                item.setToolTip(str(values.get(key, "")))
                self.external.setItem(row, column, item)
        self.external.blockSignals(False)

    def _selected_external_ids(self):
        rows = sorted({index.row() for index in self.external.selectionModel().selectedRows()})
        return [(row, self.external.item(row, 0).data(Qt.UserRole)) for row in rows if self.external.item(row, 0)]

    def save_external(self, item):
        first = self.external.item(item.row(), 0)
        if first is None:
            return
        record_id = first.data(Qt.UserRole)
        field = EXTERNAL_COLUMNS[item.column()][0]
        if not record_id or field == "due":
            return
        record = self.settings["external_authors"].setdefault(record_id, {})
        record[field] = item.text().strip()
        save_json(settings_path(), self.settings)
        if field in {"last_checked", "interval_days"}:
            self.populate_external()
        self.summary.setText("外部作者检查记录已保存。")

    def add_external(self):
        record_id = "manual-" + uuid.uuid4().hex
        self.settings["external_authors"][record_id] = {
            "author": "", "map_name": "", "channel": "", "url": "",
            "current_version": "", "last_checked": "", "interval_days": "14", "note": "",
        }
        save_json(settings_path(), self.settings)
        self.populate_external()
        row = self.external.rowCount() - 1
        self.external.selectRow(row)
        self.external.editItem(self.external.item(row, 0))

    def delete_external(self):
        selected = self._selected_external_ids()
        if not selected:
            QMessageBox.information(self, "未选择记录", "请先选择需要删除的外部作者记录。")
            return
        if QMessageBox.question(self, "删除记录", f"确定删除选中的 {len(selected)} 条记录吗？") != QMessageBox.Yes:
            return
        for _, record_id in selected:
            self.settings["external_authors"].pop(record_id, None)
        save_json(settings_path(), self.settings)
        self.populate_external()

    def mark_external_checked(self):
        selected = self._selected_external_ids()
        if not selected:
            QMessageBox.information(self, "未选择记录", "请先选择刚刚检查过的作者。")
            return
        today = date.today().isoformat()
        for _, record_id in selected:
            self.settings["external_authors"][record_id]["last_checked"] = today
        save_json(settings_path(), self.settings)
        self.populate_external()
        self.summary.setText(f"已把 {len(selected)} 条记录标记为 {today} 检查。")

    def open_external_url(self):
        selected = self._selected_external_ids()
        if not selected:
            QMessageBox.information(self, "未选择记录", "请先选择一条外部作者记录。")
            return
        _, record_id = selected[0]
        url = str(self.settings["external_authors"][record_id].get("url", "")).strip()
        if not url or not QUrl(url).isValid():
            QMessageBox.warning(self, "地址无效", "这条记录还没有可用的检查地址。")
            return
        QDesktopServices.openUrl(QUrl(url))

    def choose_server(self):
        if self.busy:
            return
        path = QFileDialog.getExistingDirectory(self, "选择含 data/maps.db 的机器人目录", self.settings["server_directory"])
        if path:
            self.settings["server_directory"] = path
            save_json(settings_path(), self.settings)
            self.refresh_report()

    def export_report(self):
        if not self.report:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出版本核对表", "地图版本核对表.html", "HTML (*.html)")
        if not path:
            return
        head = "".join(f"<th>{html.escape(label)}</th>" for _, label in COLUMNS)
        body = "".join("<tr>" + "".join(f"<td>{html.escape(str(row.get(key, '')))}</td>" for key, _ in COLUMNS) + "</tr>" for row in self.report["rows"])
        external_head = "".join(f"<th>{html.escape(label)}</th>" for _, label in EXTERNAL_COLUMNS)
        external_body = "".join(
            "<tr>" + "".join(f"<td>{html.escape(str((self._external_due(record) if key == 'due' else record.get(key, ''))))}</td>"
                               for key, _ in EXTERNAL_COLUMNS) + "</tr>"
            for record in self.settings.get("external_authors", {}).values()
        )
        Path(path).write_text('<!doctype html><meta charset="utf-8"><title>地图版本核对表</title>'
                             '<style>body{font:14px system-ui;margin:24px}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:9px}th{background:#fff0cd}tr:nth-child(even){background:#f6f8fb}</style>'
                             '<h1>地图版本核对表</h1><p>' + html.escape(self.summary.text()) + '</p><table><tr>' + head + '</tr>' + body + '</table>'
                             '<h1>外部作者检查</h1><table><tr>' + external_head + '</tr>' + external_body + '</table>', encoding="utf-8")
        QMessageBox.information(self, "导出完成", path)

    def closeEvent(self, event):
        self._closing = True
        self.timer.stop()
        self.hide()
        if self.thread_pool.activeThreadCount():
            event.ignore()
            QTimer.singleShot(100, self.close)
        else:
            event.accept()
