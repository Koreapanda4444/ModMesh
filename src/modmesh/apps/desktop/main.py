from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QTimer, qVersion
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from modmesh import __version__
from modmesh.apps.desktop.accounts import AccountPanel
from modmesh.apps.desktop.custom import CustomPanel
from modmesh.apps.desktop.details import ModDetails
from modmesh.apps.desktop.installed import InstalledPanel
from modmesh.apps.desktop.jobs import JobsPanel
from modmesh.apps.desktop.packs import PacksPanel
from modmesh.apps.desktop.profiles import ProfilesPanel
from modmesh.apps.desktop.publishing import PublishingPanel
from modmesh.apps.desktop.search import SearchPanel
from modmesh.apps.desktop.widgets import fill_row, table
from modmesh.apps.desktop.workers import Worker
from modmesh.apps.desktop.workshop import WorkshopPanel
from modmesh.apps.library import GameLibrary
from modmesh.apps.runtime import create_game_registry
from modmesh.core.config import ClientSettings, ConfigurationError, load_client_settings
from modmesh.core.diagnostics import configure_logging, install_exception_hook
from modmesh.core.discovery import (
    DiscoveryRecord,
    DiscoveryResult,
    current_platform,
)
from modmesh.core.games import DiscoveryContext, GameRegistry
from modmesh.core.profiles import Profile
from modmesh.storage.metadata import MetadataStore


class MainWindow(QMainWindow):
    def __init__(
        self,
        settings: ClientSettings,
        store: MetadataStore,
        *,
        games: GameRegistry | None = None,
        context: DiscoveryContext | None = None,
        auto_discover: bool = True,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.store = store
        self.games = games or create_game_registry()
        self.context = context or DiscoveryContext(current_platform(), environment=os.environ)
        self.records: tuple[DiscoveryRecord, ...] = ()
        self.workers: set[Worker] = set()
        self.closing = False
        self.discovery_done = False
        self.setWindowTitle("ModMesh")
        self.menuBar().addMenu("도움말").addAction("정보 / 라이선스…", self.show_about)
        self.resize(1240, 780)
        self.setMinimumSize(1000, 620)
        self.setStyleSheet(
            "QWidget {background:#151b25;color:#e5eaf3;font-size:14px;}"
            "QLabel#brand {font-size:28px;font-weight:700;color:#82e2c2;}"
            "QLabel#heading {font-size:24px;font-weight:600;}"
            "QLabel#muted {color:#a1aec2;}"
            "QPushButton,QComboBox,QLineEdit {background:#263245;border:1px solid #39485f;"
            "border-radius:5px;padding:9px;} QPushButton:hover {background:#35465e;}"
            "QPushButton:disabled {color:#73829a;} QPushButton#primary {background:#286956;}"
            "QListWidget {border:0;background:#101620;padding:12px;min-width:145px;}"
            "QListWidget::item {padding:14px;} QListWidget::item:selected {background:#26394a;}"
            "QTableWidget {border:1px solid #2b3749;background:#192230;"
            "alternate-background-color:#1d2838;selection-background-color:#304b63;}"
            "QHeaderView::section {background:#253245;border:0;padding:12px;color:#b7c7dc;}"
            "QTextEdit {border:1px solid #2b3749;background:#192230;padding:12px;}"
            "QStatusBar {color:#a1aec2;}"
        )
        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(20, 16, 20, 12)
        header = QHBoxLayout()
        brand = QLabel("ModMesh")
        brand.setObjectName("brand")
        header.addWidget(brand)
        header.addSpacing(28)
        self.game_selector = QComboBox()
        self.game_selector.setMinimumWidth(270)
        self.game_selector.addItem("게임을 선택하세요")
        self.game_selector.currentIndexChanged.connect(self._select_game)
        header.addWidget(self.game_selector)
        self.profile_selector = QComboBox()
        self.profile_selector.setMinimumWidth(190)
        self.profile_selector.addItem("프로필을 선택하세요", None)
        self.profile_selector.currentIndexChanged.connect(self._profile_choice)
        header.addWidget(self.profile_selector)
        header.addStretch()
        self.account_label = QLabel("게스트 · 로컬 모드 관리")
        header.addWidget(self.account_label)
        outer.addLayout(header)
        body = QHBoxLayout()
        self.navigation = QListWidget()
        self.navigation.addItem("내 게임")
        self.navigation.setMaximumWidth(180)
        body.addWidget(self.navigation)
        self.pages = QStackedWidget()
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        body.addWidget(self.pages, 1)
        self.library = QWidget()
        library = QVBoxLayout(self.library)
        heading = QLabel("내 게임")
        heading.setObjectName("heading")
        library.addWidget(heading)
        subtitle = QLabel("설치 경로와 환경을 확인한 뒤 관리할 게임을 선택하세요.")
        subtitle.setObjectName("muted")
        library.addWidget(subtitle)
        toolbar = QHBoxLayout()
        self.rescan = QPushButton("다시 탐색")
        self.rescan.clicked.connect(self.discover)
        toolbar.addWidget(self.rescan)
        self.manual_game = QComboBox()
        for adapter in self.games.all():
            self.manual_game.addItem(adapter.game.name, adapter.game.game_id)
        toolbar.addWidget(self.manual_game)
        self.manual_path = QLineEdit()
        self.manual_path.setPlaceholderText("게임 설치 폴더")
        self.manual_path.returnPressed.connect(self.register_manual)
        toolbar.addWidget(self.manual_path, 1)
        browse = QPushButton("찾기…")
        browse.clicked.connect(self.browse)
        toolbar.addWidget(browse)
        self.add_path = QPushButton("경로 등록")
        self.add_path.clicked.connect(self.register_manual)
        toolbar.addWidget(self.add_path)
        library.addLayout(toolbar)
        split = QSplitter()
        self.game_table = table(("게임", "환경", "상태"))
        self.game_table.itemSelectionChanged.connect(self._table_game)
        split.addWidget(self.game_table)
        self.game_details = QTextEdit()
        self.game_details.setReadOnly(True)
        self.game_details.setMinimumWidth(270)
        split.addWidget(self.game_details)
        split.setSizes([600, 300])
        library.addWidget(split, 1)
        self.discovery_status = QLabel("게임 설치를 탐색할 준비가 되었습니다.")
        self.discovery_status.setWordWrap(True)
        library.addWidget(self.discovery_status)
        self.pages.addWidget(self.library)
        self.search_panel = SearchPanel(settings, store, self.start_work)
        self.mod_details = ModDetails(
            settings,
            self.games,
            self.start_work,
            self.search_panel.details,
            self._installation_changed,
            self.search_panel.rematch,
        )
        details_scroll = QScrollArea()
        details_scroll.setWidgetResizable(True)
        details_scroll.setWidget(self.mod_details)
        self.search_panel.split.addWidget(details_scroll)
        self.search_panel.split.setSizes([570, 420])
        self.search_panel.selected.connect(self.mod_details.select)
        self.pages.addWidget(self.search_panel)
        self.navigation.addItem("모드 탐색")
        self.installed_panel = InstalledPanel(
            settings, self.games, self.start_work, self._open_installed_project
        )
        self.pages.addWidget(self.installed_panel)
        self.navigation.addItem("설치된 모드")
        self.jobs = JobsPanel(self.games, self.start_work)
        self.pages.addWidget(self.jobs)
        self.navigation.addItem("다운로드 / 작업")
        self.profiles_panel = ProfilesPanel(
            self.games, self.start_work, self._installation_changed, self._profiles_loaded
        )
        self.pages.addWidget(self.profiles_panel)
        self.navigation.addItem("프로필")
        self.packs_panel = PacksPanel(
            settings, self.games, self.start_work, self._installation_changed
        )
        self.pages.addWidget(self.packs_panel)
        self.navigation.addItem("모드팩")
        self.account_panel = AccountPanel(settings, store, self.start_work)
        self.packs_panel.bind_accounts(self.account_panel)
        self.account_panel.state_changed.connect(self.account_label.setText)
        self.pages.addWidget(self.account_panel)
        self.navigation.addItem("계정")
        self.publishing_panel = PublishingPanel(self.account_panel, self.start_work)
        self.pages.addWidget(self.publishing_panel)
        self.navigation.addItem("온라인 팩")
        self.custom_panel = CustomPanel(settings, self.start_work)
        self.pages.addWidget(self.custom_panel)
        self.navigation.addItem("커스텀 출처")
        self.workshop_panel = WorkshopPanel(self.start_work)
        self.pages.addWidget(self.workshop_panel)
        self.navigation.addItem("Steam Workshop")
        outer.addLayout(body, 1)
        self.setCentralWidget(content)
        self.navigation.setCurrentRow(0)
        self.statusBar().showMessage(
            f"릴리스 후보 {__version__} · 파일 변경은 계획 검토 후 적용됩니다."
        )
        if auto_discover:
            QTimer.singleShot(0, self.discover)

    def show_about(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("ModMesh 정보 / 라이선스")
        dialog.resize(720, 520)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"ModMesh {__version__} · Qt {qVersion()}"))
        text = QTextEdit()
        text.setReadOnly(True)
        notices = Path(sys.executable).parent / "THIRD_PARTY_NOTICES.txt"
        text.setPlainText(
            notices.read_text(encoding="utf-8")
            if getattr(sys, "frozen", False) and notices.is_file()
            else "Qt / PySide6 / shiboken6: LGPL-3.0-only\n"
            "Copyright The Qt Company Ltd. and other contributors.\n\n"
            "의존성 라이선스와 소스는 설치한 Python 패키지와 공식 배포처에서 확인하세요.\n"
            "https://doc.qt.io/qt-6/licensing.html"
        )
        layout.addWidget(text)
        close = QPushButton("닫기")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    @property
    def selected_game(self) -> DiscoveryRecord | None:
        index = self.game_selector.currentIndex()
        return self.records[index] if 0 <= index < len(self.records) else None

    def start_work(
        self,
        work: Callable[[threading.Event], object],
        completed: Callable[[object], None],
        failed: Callable[[str], None] | None = None,
        *,
        name: str = "작업",
        retry: Callable[[], None] | None = None,
    ) -> Worker:
        worker = Worker(work)
        worker.name = name
        worker.always_start = name in {"설치 적용", "제거 적용", "팩 설치 적용"}
        if self.closing:
            worker.cancel()
        self.workers.add(worker)
        worker.succeeded.connect(completed)
        worker.failed.connect(failed or self.show_error)
        worker.finished.connect(lambda: self._worker_finished(worker))
        self.jobs.enqueue(worker, name, retry)
        return worker

    def _worker_finished(self, worker: Worker) -> None:
        self.workers.discard(worker)
        if not self.closing and worker.name in {
            "설치 적용",
            "제거 적용",
            "중단 작업 복구",
            "프로필 전환",
            "팩 설치 적용",
            "팩 버전 적용",
        }:
            self.jobs.refresh_history()
        worker.deleteLater()

    def show_error(self, message: str) -> None:
        self.statusBar().showMessage(message)
        self.discovery_status.setText(message)

    def browse(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "게임 설치 폴더")
        if value:
            self.manual_path.setText(value)

    def register_manual(self) -> None:
        path = self.manual_path.text().strip()
        game_id = str(self.manual_game.currentData())
        if path:
            self.discover(manual=(game_id, Path(path)))

    def discover(self, *, manual: tuple[str, Path] | None = None) -> None:
        if not self.rescan.isEnabled():
            return
        self.rescan.setEnabled(False)
        self.add_path.setEnabled(False)
        self.discovery_done = False
        self.discovery_status.setText("게임 경로와 환경을 확인하고 있습니다…")

        def work(cancelled: threading.Event) -> DiscoveryResult:
            return GameLibrary(self.games, self.settings.paths.database).discover(
                self.context, manual=manual, cancelled=cancelled.is_set
            )

        self.start_work(
            work, self._discovered, self._discovery_failed, name="게임 탐색", retry=self.discover
        )

    def _discovery_failed(self, message: str) -> None:
        self.rescan.setEnabled(True)
        self.add_path.setEnabled(True)
        self.discovery_done = True
        self.show_error(message)

    def _discovered(self, value: object) -> None:
        assert isinstance(value, DiscoveryResult)
        previous = self.selected_game
        saved = self.store.get("desktop", "last_game") or {}
        selected = str(previous.installation.path) if previous else saved.get("path")
        selected_id = previous.installation.game_id if previous else saved.get("game_id")
        self.records = value.records
        self.game_selector.blockSignals(True)
        self.game_selector.clear()
        for record in self.records:
            installation = record.installation
            name = self.games.get(installation.game_id).game.name
            self.game_selector.addItem(f"{name} · {installation.path.name}")
        if not self.records:
            self.game_selector.addItem("탐지된 게임이 없습니다")
        index = next(
            (
                i
                for i, record in enumerate(self.records)
                if str(record.installation.path) == selected
                and record.installation.game_id == selected_id
            ),
            0,
        )
        self.game_selector.setCurrentIndex(index)
        self.game_selector.blockSignals(False)
        missing = [
            adapter
            for adapter in self.games.all()
            if not any(
                record.installation.game_id == adapter.game.game_id for record in self.records
            )
        ]
        self.game_table.setRowCount(len(self.records) + len(missing))
        for row, record in enumerate(self.records):
            install = record.installation
            fill_row(
                self.game_table,
                row,
                (
                    self.games.get(install.game_id).game.name,
                    install.edition,
                    "확인 필요" if record.validation.issues else "경로 확인됨",
                ),
            )
        for row, adapter in enumerate(missing, start=len(self.records)):
            status = (
                "미탐지" if self.context.platform in adapter.game.platforms else "현재 OS 미지원"
            )
            fill_row(
                self.game_table,
                row,
                (adapter.game.name, ", ".join(sorted(adapter.game.editions)), status),
            )
        self.discovery_status.setText(
            f"{len(self.records)}개 설치 경로 탐지"
            + (" · " + " / ".join(issue.message for issue in value.issues) if value.issues else "")
        )
        self.rescan.setEnabled(True)
        self.add_path.setEnabled(True)
        self.discovery_done = True
        self._select_game(index)

    def _table_game(self) -> None:
        row = self.game_table.currentRow()
        if row < len(self.records) and row >= 0:
            self.game_selector.setCurrentIndex(row)
        elif row >= len(self.records):
            self.game_details.setPlainText(
                "설치가 탐지되지 않았습니다. 지원 OS에서 게임 폴더를 등록하세요."
            )

    def _select_game(self, index: int) -> None:
        record = self.selected_game
        self.search_panel.set_context(record.installation if record else None)
        self.mod_details.set_context(record.installation if record else None)
        self.installed_panel.set_context(record.installation if record else None)
        self.jobs.set_context(record.installation if record else None)
        self.profiles_panel.set_context(record.installation if record else None)
        self.packs_panel.set_context(record.installation if record else None)
        self.custom_panel.set_context(record.installation if record else None)
        self.workshop_panel.set_context(record.installation if record else None)
        if record is None:
            self.game_details.setPlainText(
                "탐지된 설치 경로가 없습니다. 게임과 OS 지원 범위를 확인하고 폴더를 등록하세요."
            )
            return
        install = record.installation
        self.store.put(
            "desktop", "last_game", {"path": str(install.path), "game_id": install.game_id}
        )
        details = [
            self.games.get(install.game_id).game.name,
            str(install.path),
            f"판본: {install.edition}",
            f"플랫폼: {install.platform.value}",
            f"게임 버전: {install.version.value or '미확인'}",
            f"Build ID: {install.version.build_id or '미확인'}",
            f"로더: {install.loader.name if install.loader else '미확인'}",
            f"탐지: {record.origin}",
        ]
        details.extend(issue.message for issue in record.validation.issues)
        details.append("게임 실행과 실제 모드 로딩은 별도 확인이 필요합니다.")
        self.game_details.setPlainText("\n\n".join(details))

    def _profiles_loaded(self, profiles: tuple[Profile, ...], active: str | None) -> None:
        self.profile_selector.blockSignals(True)
        self.profile_selector.clear()
        if not profiles:
            self.profile_selector.addItem("프로필을 생성하세요", None)
        for profile in profiles:
            self.profile_selector.addItem(profile.name, profile.profile_id)
        self.profile_selector.setCurrentIndex(max(0, self.profile_selector.findData(active)))
        self.profile_selector.blockSignals(False)

    def _profile_choice(self, index: int) -> None:
        identity = self.profile_selector.itemData(index)
        if identity and identity != self.profiles_panel.active:
            self._profiles_loaded(self.profiles_panel.items, self.profiles_panel.active)
            self.profiles_panel.preview_switch(str(identity))

    def _installation_changed(self) -> None:
        self.statusBar().showMessage("설치 기록이 갱신되었습니다.")
        self.installed_panel.refresh()
        self.profiles_panel.refresh()
        self.packs_panel.refresh()

    def _open_installed_project(self, group: object) -> None:
        self.navigation.setCurrentRow(1)
        self.mod_details.select(group)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.closing = True
        if self.workers:
            for worker in self.workers:
                worker.cancel()
            event.ignore()
            self.statusBar().showMessage("진행 중인 작업을 안전하게 종료하고 있습니다…")
            QTimer.singleShot(100, self.close)
        else:
            self.mod_details.clear_plan()
            self.packs_panel.clear_plan()
            event.accept()


def main() -> int:
    parser = argparse.ArgumentParser(description="ModMesh desktop")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, help="Client TOML configuration")
    parser.add_argument(
        "--check", action="store_true", help="Exercise game discovery and the Qt event loop"
    )
    parser.add_argument("--check-report", type=Path, help="Write a new JSON check report")
    args = parser.parse_args()
    if args.check_report is not None and not args.check:
        parser.error("--check-report requires --check")
    try:
        settings = load_client_settings(args.config)
    except ConfigurationError as exc:
        parser.error(str(exc))
    logger = configure_logging(
        level=settings.log_level, directory=None if args.check else settings.paths.logs
    )
    logger.info("Desktop starting", extra={"context": {"version": __version__}})
    install_exception_hook(logger)
    app = QApplication([sys.argv[0]])
    app.setApplicationName("ModMesh")
    app.setApplicationVersion(__version__)
    checked = False
    with (
        TemporaryDirectory(prefix="modmesh-check-")
        if args.check
        else nullcontext(settings.paths.data_dir)
    ) as data:
        settings = replace(settings, paths=replace(settings.paths, data_dir=Path(data)))
        with MetadataStore(settings.paths.database) as store:
            window = MainWindow(settings, store)
            window.show()
            if args.check:
                started = time.monotonic()
                timer = QTimer()

                def finish_check() -> None:
                    nonlocal checked
                    if window.discovery_done and not window.workers:
                        checked = window.game_table.rowCount() >= len(window.games.all())
                        timer.stop()
                        window.close()
                    elif time.monotonic() - started > 15:
                        timer.stop()
                        window.close()

                timer.timeout.connect(finish_check)
                timer.start(20)
            result = app.exec()
            logger.info(
                "Desktop stopped", extra={"context": {"schema_version": store.schema_version}}
            )
    if args.check:
        report = json.dumps(
            {
                "application": "desktop",
                "version": __version__,
                "qt": qVersion(),
                "platform": app.platformName(),
                "packaged": bool(getattr(sys, "frozen", False)),
                "discovery": checked,
            }
        )
        if args.check_report is not None:
            with args.check_report.open("x", encoding="utf-8") as stream:
                stream.write(report + "\n")
        if sys.stdout is not None:
            print(report)
        return 0 if checked else 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
