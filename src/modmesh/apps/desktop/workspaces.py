from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from modmesh.apps.desktop.search import WorkStarter
from modmesh.apps.desktop.widgets import fill_row, table
from modmesh.apps.desktop.workers import run_async
from modmesh.apps.workspace_artifacts import import_local
from modmesh.apps.workspace_legacy import adopt_profile
from modmesh.apps.workspace_lifecycle import freeze_workspace
from modmesh.core.config import ClientSettings
from modmesh.core.games import GameCapability, GameRegistry
from modmesh.core.packs import PackEntry
from modmesh.core.profiles import Profile
from modmesh.core.workspace_history import PackSnapshot, WorkspaceHistory
from modmesh.core.workspaces import Workspace, WorkspaceStore
from modmesh.domain.models import GameInstallation
from modmesh.storage.metadata import MetadataStore


class WorkspacePanel(QWidget):
    def __init__(self, settings: ClientSettings, games: GameRegistry, start: WorkStarter) -> None:
        super().__init__()
        self.settings, self.games, self.start = settings, games, start
        self.installation: GameInstallation | None = None
        self.records: tuple[Workspace, ...] = ()
        self.generation = 0
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        title = QLabel("팩 작업공간")
        title.setObjectName("heading")
        layout.addWidget(title)
        note = QLabel("게임마다 독립된 팩을 편집합니다. 파일 추가는 게임 폴더를 변경하지 않습니다.")
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        self.selection = QComboBox()
        self.selection.currentIndexChanged.connect(self.selected)
        row.addWidget(self.selection, 1)
        refresh = QPushButton("새로고침")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.name = QLineEdit()
        self.name.setMaxLength(100)
        self.name.setPlaceholderText("새 팩 이름")
        row.addWidget(self.name, 1)
        self.create_button = QPushButton("팩 만들기")
        self.create_button.clicked.connect(self.create_workspace)
        row.addWidget(self.create_button)
        self.add_button = QPushButton("로컬 모드 파일 추가…")
        self.add_button.clicked.connect(self.choose_files)
        row.addWidget(self.add_button)
        self.snapshot_button = QPushButton("잠금 버전 보존")
        self.snapshot_button.clicked.connect(self.capture_snapshot)
        row.addWidget(self.snapshot_button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.legacy_profile = QComboBox()
        row.addWidget(self.legacy_profile, 1)
        self.adopt_button = QPushButton("기존 프로필을 팩으로 가져오기")
        self.adopt_button.clicked.connect(self.adopt_legacy)
        row.addWidget(self.adopt_button)
        layout.addLayout(row)
        self.entries = table(("모드", "파일", "출처", "크기", "사용", "버전 정책"))
        self.entries.itemSelectionChanged.connect(self.selected_entry)
        layout.addWidget(self.entries, 1)
        row = QHBoxLayout()
        self.toggle = QPushButton("사용 전환")
        self.toggle.clicked.connect(self.toggle_enabled)
        row.addWidget(self.toggle)
        self.pin = QPushButton("버전 고정 전환")
        self.pin.clicked.connect(self.toggle_pin)
        row.addWidget(self.pin)
        self.strategy = QComboBox()
        row.addWidget(self.strategy, 1)
        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText("ZIP 내부 prefix · 선택 사항")
        row.addWidget(self.prefix, 1)
        self.recipe_button = QPushButton("배치 선택 저장")
        self.recipe_button.clicked.connect(self.save_recipe)
        row.addWidget(self.recipe_button)
        layout.addLayout(row)
        self.status = QLabel(
            "게임을 선택하고 팩을 만드세요. ZIP/JAR/DLL/Pak 파일을 끌어 놓을 수도 있습니다."
        )
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.selected()

    @property
    def workspace(self) -> Workspace | None:
        identity = self.selection.currentData()
        return next((item for item in self.records if item.pack_id == identity), None)

    @property
    def entry(self) -> PackEntry | None:
        workspace = self.workspace
        row = self.entries.currentRow()
        return workspace.entries[row] if workspace and 0 <= row < len(workspace.entries) else None

    def selected_entry(self) -> None:
        workspace, entry, installation = self.workspace, self.entry, self.installation
        self.strategy.clear()
        self.strategy.addItem("단일 배치는 자동 선택", None)
        for button in (self.toggle, self.pin, self.recipe_button):
            button.setEnabled(entry is not None)
        if entry is None or workspace is None or installation is None:
            return
        for rule in self.games.get(installation.game_id).installation_rules(
            installation, entry.mod.file
        ):
            self.strategy.addItem(f"{rule.rule_id} → {rule.destination}", rule.rule_id)
        self.strategy.setCurrentIndex(max(0, self.strategy.findData(entry.rule_id)))
        self.prefix.setText(entry.prefix)

    def edit(
        self, *, enabled: bool | None = None, pinned: bool | None = None, recipe: bool = False
    ) -> None:
        workspace, entry = self.workspace, self.entry
        if workspace is None or entry is None:
            return
        rule = self.strategy.currentData()
        prefix = self.prefix.text() if recipe else None

        def work(_cancelled: threading.Event) -> Workspace:
            with MetadataStore(self.settings.paths.database) as database:
                return WorkspaceStore(database).edit_entry(
                    workspace.pack_id,
                    entry.mod.project.reference,
                    workspace.digest,
                    enabled=enabled,
                    pinned=pinned,
                    rule_id=str(rule) if recipe and rule else None,
                    prefix=prefix,
                    set_recipe=recipe,
                )

        def edited(value: object) -> None:
            assert isinstance(value, Workspace)
            self.refresh(value.pack_id)
            self.status.setText("팩 정의를 저장했습니다. 게임에 적용하려면 변경 계획을 검토하세요.")

        self.start(work, edited, self.failed, name="팩 구성 편집")

    def toggle_enabled(self) -> None:
        if self.workspace and self.entry:
            self.edit(enabled=self.entry.mod.project.reference in self.workspace.disabled)

    def toggle_pin(self) -> None:
        if self.workspace and self.entry:
            self.edit(pinned=self.entry.mod.project.reference in self.workspace.unpinned)

    def save_recipe(self) -> None:
        self.edit(recipe=True)

    def selected(self) -> None:
        self.entries.setRowCount(0)
        workspace = self.workspace
        writable = (
            self.installation is not None
            and GameCapability.INSTALL_RULES
            in self.games.get(self.installation.game_id).capabilities
        )
        self.create_button.setEnabled(self.installation is not None)
        self.add_button.setEnabled(workspace is not None and writable)
        self.snapshot_button.setEnabled(workspace is not None and self.installation is not None)
        if workspace is not None:
            self.entries.setRowCount(len(workspace.entries))
            for index, entry in enumerate(workspace.entries):
                fill_row(
                    self.entries,
                    index,
                    (
                        entry.mod.project.name,
                        entry.mod.file.filename,
                        entry.mod.project.reference.source_id,
                        str(entry.mod.file.size or 0),
                        "꺼짐" if entry.mod.project.reference in workspace.disabled else "켜짐",
                        "업데이트 검토"
                        if entry.mod.project.reference in workspace.unpinned
                        else "고정",
                    ),
                )
        self.selected_entry()

    def set_context(self, installation: GameInstallation | None) -> None:
        self.generation += 1
        self.installation = installation
        self.refresh()

    def set_profiles(self, profiles: tuple[Profile, ...]) -> None:
        selected = self.legacy_profile.currentData()
        self.legacy_profile.clear()
        for profile in profiles:
            self.legacy_profile.addItem(profile.name, profile.profile_id)
        self.legacy_profile.setCurrentIndex(max(0, self.legacy_profile.findData(selected)))
        self.adopt_button.setEnabled(bool(profiles) and self.installation is not None)

    def adopt_legacy(self) -> None:
        installation = self.installation
        identity = self.legacy_profile.currentData()
        if installation is None or not identity:
            return

        def work(_cancelled: threading.Event) -> Workspace:
            return adopt_profile(self.settings, installation, str(identity))

        def adopted(value: object) -> None:
            assert isinstance(value, Workspace)
            self.refresh(value.pack_id)
            self.status.setText("기존 프로필과 파일을 보존하고 새 팩에 연결했습니다.")

        self.start(work, adopted, self.failed, name="기존 프로필 연결")

    def refresh(self, selected: str | None = None) -> None:
        self.generation += 1
        generation = self.generation
        selected = selected or str(self.selection.currentData() or "")
        installation = self.installation
        if installation is None:
            self.records = ()
            self.selection.clear()
            self.selected()
            return

        def work(_cancelled: threading.Event) -> tuple[Workspace, ...]:
            with MetadataStore(self.settings.paths.database) as database:
                return WorkspaceStore(database).list(installation.game_id)

        def loaded(value: object) -> None:
            if generation != self.generation:
                return
            assert isinstance(value, tuple)
            self.records = value
            self.selection.blockSignals(True)
            self.selection.clear()
            for workspace in self.records:
                self.selection.addItem(
                    f"{workspace.name} · r{workspace.revision}", workspace.pack_id
                )
            self.selection.setCurrentIndex(max(0, self.selection.findData(selected)))
            self.selection.blockSignals(False)
            self.selected()

        self.start(work, loaded, self.failed, name="팩 작업공간 조회")

    def create_workspace(self) -> None:
        if self.installation is None:
            return
        game, name = self.installation.game_id, self.name.text()

        def work(_cancelled: threading.Event) -> Workspace:
            with MetadataStore(self.settings.paths.database) as database:
                return WorkspaceStore(database).create(game, name)

        def created(value: object) -> None:
            assert isinstance(value, Workspace)
            self.name.clear()
            self.refresh(value.pack_id)

        self.start(work, created, self.failed, name="팩 만들기")

    def capture_snapshot(self) -> None:
        workspace, installation = self.workspace, self.installation
        if workspace is None or installation is None:
            return

        def work(_cancelled: threading.Event) -> PackSnapshot:
            lock = freeze_workspace(
                self.settings, workspace.pack_id, installation, workspace.digest
            )
            with MetadataStore(self.settings.paths.database) as database:
                return WorkspaceHistory(database).capture(lock)

        def captured(value: object) -> None:
            assert isinstance(value, PackSnapshot)
            self.status.setText(
                f"r{value.lock.workspace.revision} 잠금 버전을 보존했습니다. "
                f"검증되지 않은 조건 {len(value.lock.warnings)}개는 그대로 표시합니다."
            )

        self.start(work, captured, self.failed, name="팩 잠금 버전 보존")

    def choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "모드 파일 추가", filter="Mods (*.zip *.jar *.dll *.pak)"
        )
        if paths:
            self.import_files(tuple(Path(path) for path in paths))

    def import_files(self, paths: tuple[Path, ...]) -> None:
        workspace = self.workspace
        if workspace is None or not 1 <= len(paths) <= 16:
            self.status.setText("팩을 선택하고 한 번에 1–16개 파일을 추가하세요.")
            return

        async def work(cancelled: threading.Event) -> Workspace:
            current = workspace
            for path in paths:
                current = await import_local(
                    self.settings, current.pack_id, path, current.digest, cancelled=cancelled
                )
            return current

        def imported(value: object) -> None:
            assert isinstance(value, Workspace)
            self.refresh(value.pack_id)
            self.status.setText(
                "검증한 사본을 보존했습니다. 의존성·게임 로딩은 미검증이며 "
                "게임 파일은 변경하지 않았습니다."
            )

        self.start(
            lambda cancelled: run_async(work(cancelled), cancelled),
            imported,
            self.failed,
            name="팩 파일 가져오기",
        )

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        mime = event.mimeData()
        if (
            self.workspace is not None
            and mime is not None
            and mime.hasUrls()
            and all(url.isLocalFile() for url in mime.urls())
        ):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        mime = event.mimeData()
        if mime is not None and mime.hasUrls() and all(url.isLocalFile() for url in mime.urls()):
            self.import_files(tuple(Path(url.toLocalFile()) for url in mime.urls()))
            event.acceptProposedAction()

    def failed(self, message: str) -> None:
        self.status.setText(message)
