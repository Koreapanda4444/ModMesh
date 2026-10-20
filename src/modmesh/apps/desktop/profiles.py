from __future__ import annotations

from collections.abc import Callable
from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from modmesh.apps.desktop.search import WorkStarter
from modmesh.apps.desktop.widgets import fill_row, table
from modmesh.apps.desktop.workers import report_progress
from modmesh.core.games import GameRegistry
from modmesh.core.profiles import Profile, ProfileManager, ProfilePlan
from modmesh.domain.models import GameInstallation


class ProfileDialog(QDialog):
    def __init__(
        self, plan: ProfilePlan, parent: QWidget, source_notes: tuple[str, ...] = ()
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("프로필 전환 계획")
        self.resize(850, 610)
        layout = QVBoxLayout(self)
        title = QLabel(f"{plan.previous.name} → {plan.target.name}\n승인 식별: {plan.fingerprint}")
        title.setWordWrap(True)
        layout.addWidget(title)
        changes = table(("동작", "상대 경로", "크기"))
        changes.setRowCount(len(plan.changes))
        for row, change in enumerate(plan.changes):
            fill_row(changes, row, (change.action, change.path, str(change.size)))
        layout.addWidget(changes, 2)
        notes = QTextEdit()
        notes.setReadOnly(True)
        notes.setPlainText(
            "\n".join(
                [f"{item.code}: {item.message}" for item in plan.issues]
                + list(plan.warnings)
                + list(source_notes)
            )
        )
        layout.addWidget(notes, 1)
        self.acknowledge = QCheckBox(
            "파일 교체·원본 복원과 외부 모드 공유를 확인했고 게임을 종료했습니다."
        )
        layout.addWidget(self.acknowledge)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.confirm = buttons.addButton("승인하고 전환", QDialogButtonBox.ButtonRole.AcceptRole)
        self.confirm.setEnabled(False)
        self.acknowledge.toggled.connect(
            lambda checked: self.confirm.setEnabled(checked and plan.applicable)
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ProfilesPanel(QWidget):
    def __init__(
        self,
        games: GameRegistry,
        start: WorkStarter,
        changed: Callable[[], None],
        catalog: Callable[[tuple[Profile, ...], str | None], None],
    ) -> None:
        super().__init__()
        self.games, self.start, self.changed, self.catalog = games, start, changed, catalog
        self.installation: GameInstallation | None = None
        self.items: tuple[Profile, ...] = ()
        self.active: str | None = None
        self.generation = 0
        self.busy = False
        self.plan: ProfilePlan | None = None
        self.dialog: ProfileDialog | None = None
        layout = QVBoxLayout(self)
        title = QLabel("프로필")
        title.setObjectName("heading")
        layout.addWidget(title)
        note = QLabel("전환 시 관리 구성을 저장합니다. 외부 모드는 공유하고 세이브는 보존합니다.")
        note.setWordWrap(True)
        layout.addWidget(note)
        toolbar = QHBoxLayout()
        self.name = QLineEdit()
        self.name.setMaxLength(100)
        self.name.setPlaceholderText("새 프로필 이름")
        toolbar.addWidget(self.name)
        self.capture = QPushButton("현재 구성 저장")
        self.capture.clicked.connect(lambda: self.create_profile(True))
        toolbar.addWidget(self.capture)
        self.empty = QPushButton("빈 프로필")
        self.empty.clicked.connect(lambda: self.create_profile(False))
        toolbar.addWidget(self.empty)
        self.clone = QPushButton("선택 구성 복제")
        self.clone.clicked.connect(self.clone_selected)
        toolbar.addWidget(self.clone)
        layout.addLayout(toolbar)
        self.settings = QListWidget()
        self.settings.setMaximumHeight(75)
        layout.addWidget(QLabel("현재 구성 저장 시 포함할 설정 파일 · 직접 선택한 파일만 수집"))
        layout.addWidget(self.settings)
        split = QSplitter()
        self.list = table(("프로필", "상태", "모드", "파일"))
        self.list.itemSelectionChanged.connect(self.selected)
        split.addWidget(self.list)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        split.addWidget(self.details)
        split.setSizes([570, 350])
        layout.addWidget(split, 1)
        actions = QHBoxLayout()
        self.refresh_button = QPushButton("새로고침")
        self.refresh_button.clicked.connect(self.refresh)
        actions.addWidget(self.refresh_button)
        self.rename = QPushButton("입력한 이름으로 변경")
        self.rename.clicked.connect(self.rename_selected)
        actions.addWidget(self.rename)
        self.remove = QPushButton("선택 프로필 삭제")
        self.remove.clicked.connect(self.delete_selected)
        actions.addWidget(self.remove)
        self.preview = QPushButton("전환 계획 검토")
        self.preview.clicked.connect(lambda: self.preview_switch())
        actions.addWidget(self.preview)
        layout.addLayout(actions)
        self.status = QLabel("게임 설치를 선택하세요.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons()

    def buttons(self) -> None:
        enabled = self.installation is not None and not self.busy
        for button in (self.capture, self.empty, self.refresh_button):
            button.setEnabled(enabled)
        item = self.selected_profile()
        self.clone.setEnabled(enabled and item is not None)
        self.rename.setEnabled(enabled and item is not None)
        self.remove.setEnabled(enabled and item is not None and item.profile_id != self.active)
        self.preview.setEnabled(enabled and item is not None and item.profile_id != self.active)

    def set_context(self, installation: GameInstallation | None) -> None:
        if self.installation == installation:
            return
        self.generation += 1
        self.installation = installation
        self.items, self.active = (), None
        self.plan = None
        if self.dialog:
            self.dialog.reject()
            self.dialog = None
        self.settings.clear()
        if installation:
            for path in self.games.get(installation.game_id).profile_settings(installation):
                item = QListWidgetItem(path)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                self.settings.addItem(item)
        self.list.setRowCount(0)
        self.details.clear()
        self.catalog((), None)
        self.buttons()
        self.refresh()

    def manager(self) -> ProfileManager:
        assert self.installation is not None
        return ProfileManager(self.games, self.installation)

    def refresh(self) -> None:
        if self.installation is None or self.busy:
            return
        manager, generation = self.manager(), self.generation
        self.start(
            lambda cancelled: (manager.profiles(), manager.active_id()),
            partial(self.loaded, generation),
            partial(self.failed, generation),
            name="프로필 목록 조회",
        )

    def loaded(self, generation: int, value: object) -> None:
        if generation != self.generation:
            return
        assert isinstance(value, tuple)
        self.items, self.active = value
        self.list.setRowCount(len(self.items))
        for row, item in enumerate(self.items):
            fill_row(
                self.list,
                row,
                (
                    item.name,
                    "활성" if item.profile_id == self.active else "저장됨",
                    str(len(item.mods)),
                    str(len(item.files)),
                ),
            )
        if self.items:
            self.list.selectRow(
                next((i for i, item in enumerate(self.items) if item.profile_id == self.active), 0)
            )
        self.status.setText(f"{len(self.items)}개 프로필 · 전환 전에 실제 파일 변경을 검토하세요.")
        self.catalog(self.items, self.active)
        self.buttons()

    def selected_profile(self) -> Profile | None:
        row = self.list.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else None

    def selected(self) -> None:
        item = self.selected_profile()
        if item:
            self.details.setPlainText(
                item.name
                + "\n\n"
                + str(item.context)
                + "\n\n"
                + "\n".join(
                    mod.mod.project.name
                    + " · "
                    + (mod.mod.version.number or mod.mod.version.version_id)
                    for mod in item.mods
                )
                + "\n\n"
                + "\n".join(
                    ("설정: " if file.setting else "모드: ") + file.path for file in item.files
                )
            )
        self.buttons()

    def mutate(self, action: Callable[[ProfileManager], object], name: str) -> None:
        if self.installation is None or self.busy:
            return
        manager, generation = self.manager(), self.generation
        self.busy = True
        self.buttons()
        self.start(
            lambda cancelled: action(manager),
            partial(self.done, generation),
            partial(self.failed, generation),
            name=name,
        )

    def done(self, generation: int, value: object) -> None:
        self.busy = False
        self.changed()
        if generation == self.generation:
            self.buttons()
            self.refresh()

    def failed(self, generation: int, message: str) -> None:
        self.busy = False
        if generation == self.generation:
            self.status.setText(message)
            self.buttons()

    def create_profile(self, capture: bool) -> None:
        name = self.name.text().strip()
        settings = tuple(
            self.settings.item(index).text()
            for index in range(self.settings.count())
            if self.settings.item(index).checkState() == Qt.CheckState.Checked
        )
        self.mutate(
            lambda manager: manager.create(name, capture=capture, settings=settings), "프로필 저장"
        )

    def clone_selected(self) -> None:
        item = self.selected_profile()
        if item:
            name = self.name.text().strip()
            self.mutate(lambda manager: manager.create(name, clone=item.profile_id), "프로필 복제")

    def rename_selected(self) -> None:
        item = self.selected_profile()
        if item:
            name = self.name.text().strip()
            self.mutate(lambda manager: manager.rename(item.profile_id, name), "프로필 이름 변경")

    def delete_selected(self) -> None:
        item = self.selected_profile()
        if (
            item
            and item.profile_id != self.active
            and QMessageBox.question(
                self,
                "프로필 삭제",
                f"저장된 구성 '{item.name}'을 삭제합니까? 활성 게임 파일은 변경하지 않습니다.",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.mutate(lambda manager: manager.delete(item.profile_id), "프로필 삭제")

    def preview_switch(self, identity: str | None = None) -> None:
        if self.installation is None or self.busy:
            return
        item = self.selected_profile()
        identity = identity or (item.profile_id if item else None)
        if identity is None:
            return
        manager, generation = self.manager(), self.generation
        self.start(
            lambda cancelled: manager.plan_switch(identity),
            partial(self.prepared, generation),
            partial(self.failed, generation),
            name="프로필 전환 계획",
        )

    def prepared(self, generation: int, value: object) -> None:
        if generation != self.generation:
            return
        assert isinstance(value, ProfilePlan)
        if self.dialog:
            self.dialog.reject()
        self.plan = value
        self.dialog = ProfileDialog(value, self)
        self.dialog.rejected.connect(self.discard_plan)
        self.dialog.accepted.connect(self.apply)
        self.dialog.open()

    def discard_plan(self) -> None:
        self.plan = None
        self.dialog = None

    def apply(self) -> None:
        plan, self.plan = self.plan, None
        if plan is None or self.installation is None or self.busy:
            return
        manager, generation = self.manager(), self.generation
        self.dialog = None
        self.busy = True
        self.buttons()
        self.start(
            lambda cancelled: manager.switch(
                plan, plan.fingerprint, cancelled=cancelled.is_set, progress=report_progress
            ),
            partial(self.done, generation),
            partial(self.failed, generation),
            name="프로필 전환",
        )
