from __future__ import annotations

from collections.abc import Callable
from functools import partial

from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from modmesh.apps.desktop.search import WorkStarter
from modmesh.apps.desktop.widgets import fill_row, table
from modmesh.apps.desktop.workers import Worker, run_async
from modmesh.apps.runtime import source_registry
from modmesh.core.config import ClientSettings
from modmesh.core.games import GameRegistry
from modmesh.core.installation import OperationResult
from modmesh.core.inventory import Inventory, UpdateCheck, check_updates, inspect_inventory
from modmesh.core.matching import ProjectGroup
from modmesh.core.removal import RemovalManager, RemovalPlan
from modmesh.domain.models import GameInstallation, SourceProject, SourceRef
from modmesh.storage.installed import mod_key


class RemovalDialog(QDialog):
    def __init__(self, plan: RemovalPlan, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("제거 변경 계획")
        self.resize(900, 640)
        layout = QVBoxLayout(self)
        title = QLabel(f"대상: {plan.installation.path}\n승인 식별: {plan.fingerprint}")
        title.setWordWrap(True)
        layout.addWidget(title)
        changes = table(("동작", "상대 경로", "다른 소유자"))
        changes.setRowCount(len(plan.changes))
        selected = {mod_key(mod.mod.project.reference) for mod in plan.mods}
        for row, change in enumerate(plan.changes):
            fill_row(
                changes,
                row,
                (
                    change.action,
                    change.destination,
                    str(len(set(change.owners) - selected)),
                ),
            )
        layout.addWidget(changes, 1)
        notes = QTextEdit()
        notes.setReadOnly(True)
        notes.setPlainText(
            "\n\n".join(
                [f"{issue.code}: {issue.message} {issue.path or ''}" for issue in plan.issues]
                + list(plan.warnings)
            )
        )
        layout.addWidget(notes, 1)
        self.acknowledge = QCheckBox("제거·원본 복원·보존 파일을 확인했고 게임을 종료했습니다.")
        layout.addWidget(self.acknowledge)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.confirm = buttons.addButton("승인하고 제거", QDialogButtonBox.ButtonRole.AcceptRole)
        self.confirm.setEnabled(False)
        self.acknowledge.toggled.connect(
            lambda checked: self.confirm.setEnabled(checked and plan.applicable)
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class InstalledPanel(QWidget):
    def __init__(
        self,
        settings: ClientSettings,
        games: GameRegistry,
        start: WorkStarter,
        open_project: Callable[[ProjectGroup], None],
    ) -> None:
        super().__init__()
        self.settings, self.games, self.start, self.open_project = (
            settings,
            games,
            start,
            open_project,
        )
        self.installation: GameInstallation | None = None
        self.inventory: Inventory | None = None
        self.worker: Worker | None = None
        self.generation = 0
        self.dialog: RemovalDialog | None = None
        self.mutating = False
        self.updates: dict[SourceRef, UpdateCheck] = {}
        layout = QVBoxLayout(self)
        title = QLabel("설치된 모드")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(
            QLabel("관리 기록과 실제 파일을 비교합니다. 외부 파일은 자동 제거하지 않습니다.")
        )
        toolbar = QHBoxLayout()
        self.reload = QPushButton("파일 상태 새로고침")
        self.reload.clicked.connect(self.refresh)
        toolbar.addWidget(self.reload)
        self.check = QPushButton("다른 호환 버전 확인")
        self.check.clicked.connect(self.check_versions)
        toolbar.addWidget(self.check)
        self.edit = QPushButton("버전 선택 / 업데이트")
        self.edit.clicked.connect(self.edit_version)
        toolbar.addWidget(self.edit)
        self.remove = QPushButton("선택 모드 제거 계획")
        self.remove.clicked.connect(self.prepare_removal)
        toolbar.addWidget(self.remove)
        layout.addLayout(toolbar)
        split = QSplitter()
        self.mods = table(("모드", "버전", "관리 상태", "파일 상태", "다른 버전"))
        self.mods.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.mods.itemSelectionChanged.connect(self._selected)
        split.addWidget(self.mods)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        split.addWidget(self.details)
        split.setSizes([680, 300])
        layout.addWidget(split, 1)
        self.status = QLabel("관리할 게임 설치를 선택하세요.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._buttons(False)

    def _buttons(self, enabled: bool) -> None:
        self.reload.setEnabled(self.installation is not None and not self.mutating)
        self.check.setEnabled(enabled and not self.mutating)
        self.edit.setEnabled(False)
        self.remove.setEnabled(False)
        if enabled:
            self._selected()

    def set_context(self, installation: GameInstallation | None) -> None:
        if installation == self.installation:
            return
        self.generation += 1
        if self.worker is not None and not self.mutating:
            self.worker.cancel()
        self.worker = None
        if self.dialog:
            self.dialog.reject()
            self.dialog = None
        self.installation, self.inventory = installation, None
        self.updates.clear()
        self.mods.setRowCount(0)
        self.details.clear()
        self._buttons(False)
        self.refresh()

    def refresh(self) -> None:
        if self.installation is None or self.mutating:
            return
        self.generation += 1
        generation = self.generation
        installation = self.installation
        self._buttons(False)
        self.reload.setEnabled(False)
        self.status.setText("설치 기록·파일 해시·외부 모드를 확인하고 있습니다…")
        self.worker = self.start(
            lambda cancelled: inspect_inventory(
                RemovalManager(self.games, installation), cancelled.is_set
            ),
            partial(self._loaded, generation),
            partial(self._failed, generation),
        )

    def _failed(self, generation: int, message: str) -> None:
        if generation == self.generation:
            self.worker = None
            self.status.setText(message)
            self._buttons(self.inventory is not None)

    def _loaded(self, generation: int, value: object) -> None:
        if generation != self.generation:
            return
        assert isinstance(value, Inventory)
        self.worker = None
        self.inventory = value
        self.mods.setRowCount(len(value.managed) + len(value.external))
        for row, mod in enumerate(value.managed):
            installed = mod.installed.mod
            states = ", ".join(sorted({file.state for file in mod.files}))
            fill_row(
                self.mods,
                row,
                (
                    installed.project.name,
                    installed.version.number or installed.version.version_id,
                    "직접 선택" if installed.direct else "의존성",
                    states or "파일 없음",
                    "조회 전",
                ),
            )
        for row, external in enumerate(value.external, start=len(value.managed)):
            fill_row(
                self.mods,
                row,
                (
                    external.name or external.mod_id,
                    external.version or "미확인",
                    "외부 설치",
                    "보존",
                    "출처 미확인",
                ),
            )
        conflicts = "; ".join(issue.message for issue in value.conflicts.issues)
        self.status.setText(
            f"관리 {len(value.managed)}개 · 외부 {len(value.external)}개\n" + conflicts
        )
        self._buttons(bool(value.managed))
        self._selected()

    def selected_rows(self) -> tuple[int, ...]:
        return tuple(sorted({index.row() for index in self.mods.selectedIndexes()}))

    def _selected(self) -> None:
        rows = self.selected_rows()
        inventory = self.inventory
        valid = (
            inventory is not None
            and bool(rows)
            and all(row < len(inventory.managed) for row in rows)
        )
        self.remove.setEnabled(valid and not self.mutating)
        self.edit.setEnabled(valid and len(rows) == 1 and not self.mutating)
        if inventory is None or not rows:
            return
        texts = []
        for row in rows:
            if row < len(inventory.managed):
                mod = inventory.managed[row]
                texts.append(mod.installed.mod.project.name)
                texts.extend(
                    f"{file.path}\n{file.state} · 소유자 {file.owners} · "
                    f"{'관리 파일' if file.managed else '외부 파일 보존'}"
                    for file in mod.files
                )
                texts.extend(
                    f"필수 의존성: {ref.source_id}:{ref.project_id}"
                    for ref in mod.installed.dependencies
                )
            else:
                external = inventory.external[row - len(inventory.managed)]
                texts.append((external.name or external.mod_id) + " · 외부 설치")
                texts.extend(file.path for file in external.files)
        texts.append(
            "다른 파일명으로 변경되는 업데이트는 제거 계획을 먼저 적용한 뒤 새 설치를 검토하세요."
        )
        self.details.setPlainText("\n\n".join(texts))

    def check_versions(self) -> None:
        if not self.inventory or not self.installation or not self.check.isEnabled():
            return
        generation, installation = self.generation, self.installation
        mods = tuple(mod.installed for mod in self.inventory.managed)
        self.check.setEnabled(False)
        self.status.setText("출처별 버전을 확인하고 있습니다…")

        async def execute() -> tuple[UpdateCheck, ...]:
            async with source_registry() as registry:
                return await check_updates(
                    registry,
                    mods,
                    game_id=installation.game_id,
                    game_version=installation.version.value,
                    loader=installation.loader.name if installation.loader else None,
                )

        self.worker = self.start(
            lambda cancelled: run_async(execute(), cancelled),
            partial(self._updates, generation),
            partial(self._failed, generation),
        )

    def _updates(self, generation: int, value: object) -> None:
        if generation != self.generation or self.inventory is None:
            return
        assert isinstance(value, tuple)
        checks = tuple(item for item in value if isinstance(item, UpdateCheck))
        self.worker = None
        self.updates = {item.reference: item for item in checks}
        for row, mod in enumerate(self.inventory.managed):
            check = self.updates.get(mod.installed.mod.project.reference)
            item = self.mods.item(row, 4)
            if item is not None and check is not None:
                item.setText(
                    f"호환 후보 {len(check.alternatives)}개"
                    if check.state == "checked"
                    else "조회 불가"
                )
                item.setToolTip(check.message or "")
        self.check.setEnabled(True)
        self.status.setText(
            "다른 버전은 출처 목록 기준 후보입니다. 버전 선택 후 변경 계획을 검토하세요."
        )

    def edit_version(self) -> None:
        rows = self.selected_rows()
        if self.inventory is not None and len(rows) == 1 and rows[0] < len(self.inventory.managed):
            project: SourceProject = self.inventory.managed[rows[0]].installed.mod.project
            self.open_project(ProjectGroup(project.reference.project_id, None, (project,)))

    def prepare_removal(self) -> None:
        if not self.remove.isEnabled() or self.inventory is None or self.installation is None:
            return
        references = tuple(
            self.inventory.managed[row].installed.mod.project.reference
            for row in self.selected_rows()
        )
        manager = RemovalManager(self.games, self.installation)
        generation = self.generation
        self.remove.setEnabled(False)
        self.worker = self.start(
            lambda cancelled: manager.plan_removal(references),
            partial(self._removal_ready, generation, manager),
            partial(self._failed, generation),
        )

    def _removal_ready(self, generation: int, manager: RemovalManager, value: object) -> None:
        if generation != self.generation:
            return
        assert isinstance(value, RemovalPlan)
        self.worker = None
        dialog = RemovalDialog(value, self)
        self.dialog = dialog
        dialog.accepted.connect(partial(self.apply_removal, manager, value))
        dialog.rejected.connect(lambda: self._selected())
        dialog.open()

    def apply_removal(self, manager: RemovalManager, plan: RemovalPlan) -> None:
        self.dialog = None
        self.mutating = True
        self._buttons(False)
        self.status.setText("승인한 제거 계획을 적용하고 있습니다…")
        self.worker = self.start(
            lambda cancelled: manager.remove(plan, plan.fingerprint, cancelled=cancelled.is_set),
            self._removed,
            self._remove_failed,
        )

    def _removed(self, value: object) -> None:
        assert isinstance(value, OperationResult)
        self.mutating = False
        self.worker = None
        self.refresh()

    def _remove_failed(self, message: str) -> None:
        self.mutating = False
        self.worker = None
        self._buttons(self.inventory is not None)
        self.status.setText(message)
