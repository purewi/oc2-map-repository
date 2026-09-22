from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from . import __version__
from .config import app_data_directory, load_config
from .self_update import cleanup_previous_client, run_update_helper_if_requested
from .ui.main_window import MainWindow
from .variant import current_variant


def main() -> int:
    helper_result = run_update_helper_if_requested(sys.argv)
    if helper_result is not None:
        return helper_result
    directory = app_data_directory()
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=directory / "client.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        encoding="utf-8",
    )
    app = QApplication(sys.argv)
    variant = current_variant()
    app.setApplicationName(f"OC2 Map Manager - {variant.display_name}")
    app.setOrganizationName("OC2Community")
    app.setWindowIcon(QIcon(str(Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "assets" / "kitchen-updater.ico")))
    smoke_test = "--smoke-test" in sys.argv
    stability_test = "--stability-test" in sys.argv
    config = load_config()
    logging.info(
        "客户端启动 version=%s variant=%s cos_enabled=%s",
        __version__,
        variant.key,
        variant.cos_enabled,
    )
    if smoke_test:
        config.cloud_repository = ""
        config.auto_check_updates = False
    if "--maintenance" in sys.argv:
        from .ui.maintenance_window import MaintenanceWindow
        window = MaintenanceWindow(config)
    else:
        window = MainWindow(config)
    if smoke_test:
        window.close()
        app.processEvents()
        return 0
    window.show()
    if "--client-update-finished" in sys.argv:
        relocated = "--client-update-relocated" in sys.argv
        detail = (
            f"\n\n原文件所在目录不允许自动替换，已将客户端迁移到：\n{Path(sys.executable)}\n"
            "以后请从这个新位置启动。"
            if relocated
            else ""
        )
        def finish_update_notice() -> None:
            QMessageBox.information(
                window, "更新完成", f"客户端已更新到 v{__version__}。{detail}"
            )
            cleanup_previous_client()

        QTimer.singleShot(0, finish_update_notice)
    if stability_test:
        QTimer.singleShot(15_000, window.close)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
