"""Qt entry point. Slow work belongs in workers, never in this event loop."""

import argparse
import json
import sys

from PySide6.QtCore import QTimer, qVersion
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QVBoxLayout, QWidget

from modmesh import __version__


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ModMesh")
        self.resize(1120, 740)
        self.setMinimumSize(800, 520)
        self.setStyleSheet(
            "QWidget { background: #171c24; color: #e9edf4; font-size: 14px; }"
            "QLabel#title { font-size: 36px; font-weight: bold; color: #89b4fa; }"
        )
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(36, 36, 36, 36)
        title = QLabel("ModMesh")
        title.setObjectName("title")
        layout.addWidget(title)
        layout.addWidget(QLabel("게임과 모드 배포처를 연결하는 모드 매니저"))
        layout.addWidget(QLabel("개발 초기 단계 · 게임 탐지와 모드 관리는 순서대로 추가됩니다."))
        layout.addStretch()
        self.setCentralWidget(content)
        self.statusBar().showMessage(f"개발판 {__version__} · 게스트")


def main() -> int:
    parser = argparse.ArgumentParser(description="ModMesh desktop")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--check", action="store_true", help="Create and exercise the Qt window, then exit"
    )
    args = parser.parse_args()
    app = QApplication([sys.argv[0]])
    app.setApplicationName("ModMesh")
    app.setApplicationVersion(__version__)
    window = MainWindow()
    window.show()
    if args.check:
        QTimer.singleShot(50, app.quit)
    result = app.exec()
    if args.check:
        print(json.dumps({"application": "desktop", "version": __version__, "qt": qVersion()}))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
