from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from modmesh.apps.desktop.search import WorkStarter
from modmesh.apps.desktop.widgets import fill_row, table
from modmesh.apps.desktop.workers import Worker, run_async
from modmesh.apps.operations import PreparedInstallation, prepare_resolution
from modmesh.apps.runtime import source_priorities, source_registry
from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.games import GameRegistry
from modmesh.core.identity import IdentityRegistry
from modmesh.core.installation import OperationResult
from modmesh.core.matching import ProjectGroup
from modmesh.core.resolver import DependencyResolver, ResolvedMod
from modmesh.core.sources import DownloadMode, VersionQuery
from modmesh.domain.models import GameInstallation, ModFile, ModVersion, SourceProject
from modmesh.storage.metadata import MetadataStore


@dataclass(frozen=True)
class ProjectDetails:
    project: SourceProject
    versions: tuple[ModVersion, ...]


class PlanDialog(QDialog):
    def __init__(self, prepared: PreparedInstallation, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("설치 변경 계획")
        self.resize(920, 680)
        layout = QVBoxLayout(self)
        plan = prepared.plan
        label = QLabel(f"대상: {plan.installation.path}\n승인 식별: {plan.fingerprint}")
        label.setWordWrap(True)
        layout.addWidget(label)
        changes = table(("동작", "상대 경로", "크기", "기존 파일"))
        changes.setRowCount(len(plan.changes))
        for row, change in enumerate(plan.changes):
            fill_row(
                changes,
                row,
                (
                    change.action,
                    change.destination,
                    str(change.size),
                    "백업 후 교체"
                    if change.action == "replace"
                    else "보존"
                    if change.before
                    else "없음",
                ),
            )
            item = changes.item(row, 1)
            assert item is not None
            item.setToolTip(change.sha256)
        layout.addWidget(changes, 1)
        notes = QTextEdit()
        notes.setReadOnly(True)
        messages = [f"{issue.code}: {issue.message} {issue.path or ''}" for issue in plan.issues]
        messages.extend(plan.warnings)
        for mod in plan.resolution.mods:
            algorithms = (
                ", ".join(value.algorithm.value for value in mod.file.hashes) or "출처 해시 없음"
            )
            messages.append(
                f"{mod.project.name} · "
                f"{mod.version.number or mod.version.version_id} · {algorithms}"
            )
        messages.append("해시 일치는 모드 코드의 안전성을 보장하지 않습니다.")
        notes.setPlainText("\n\n".join(messages))
        layout.addWidget(notes, 1)
        self.acknowledge = QCheckBox("변경 파일·백업·경고를 확인했고 게임을 종료했습니다.")
        layout.addWidget(self.acknowledge)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.confirm = buttons.addButton("승인하고 설치", QDialogButtonBox.ButtonRole.AcceptRole)
        self.confirm.setEnabled(False)
        self.acknowledge.toggled.connect(
            lambda checked: self.confirm.setEnabled(checked and plan.applicable)
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ModDetails(QWidget):
    def __init__(
        self,
        settings: ClientSettings,
        games: GameRegistry,
        start: WorkStarter,
        description: QTextEdit,
        changed: Callable[[], None],
    ) -> None:
        super().__init__()
        self.settings, self.games, self.start, self.changed = settings, games, start, changed
        self.installation: GameInstallation | None = None
        self.group: ProjectGroup | None = None
        self.project: SourceProject | None = None
        self.versions: tuple[ModVersion, ...] = ()
        self.generation = 0
        self.worker: Worker | None = None
        self.prepared: PreparedInstallation | None = None
        self.dialog: PlanDialog | None = None
        self.applying = False
        layout = QVBoxLayout(self)
        self.description = description
        layout.addWidget(description, 2)
        self.variant = QComboBox()
        self.variant.currentIndexChanged.connect(self.load_variant)
        layout.addWidget(self.variant)
        self.version = QComboBox()
        self.version.currentIndexChanged.connect(self._version_changed)
        layout.addWidget(self.version)
        self.file = QComboBox()
        self.file.currentIndexChanged.connect(self._rule_changed)
        layout.addWidget(self.file)
        self.dependencies = QTextEdit()
        self.dependencies.setReadOnly(True)
        self.dependencies.setMaximumHeight(90)
        layout.addWidget(self.dependencies)
        self.rule = QComboBox()
        self.rule.currentIndexChanged.connect(self.invalidate)
        layout.addWidget(self.rule)
        self.prefix = QLineEdit()
        self.prefix.textChanged.connect(self.invalidate)
        self.prefix.setPlaceholderText("압축 내부 폴더 · 필요한 경우만")
        layout.addWidget(self.prefix)
        self.replace = QCheckBox("외부 파일 교체를 계획에 포함 · 원본 백업")
        self.replace.toggled.connect(self.invalidate)
        layout.addWidget(self.replace)
        self.prepare = QPushButton("설치 변경 계획 준비")
        self.prepare.clicked.connect(self.prepare_selected)
        self.prepare.setEnabled(False)
        layout.addWidget(self.prepare)
        self.website = QPushButton("공식 페이지 열기")
        self.website.clicked.connect(self.open_website)
        self.website.setEnabled(False)
        layout.addWidget(self.website)
        self.status = QLabel("모드를 선택하면 버전과 파일을 확인할 수 있습니다.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def invalidate(self) -> None:
        self.generation += 1
        if self.worker is not None and not self.applying:
            self.worker.cancel()
        self.worker = None
        self.clear_plan()

    def clear_plan(self) -> None:
        if self.dialog is not None:
            dialog, self.dialog = self.dialog, None
            dialog.reject()
        if self.prepared is not None:
            self.prepared.close()
            self.prepared = None

    def set_context(self, installation: GameInstallation | None) -> None:
        if self.installation != installation:
            self.clear_plan()
            self.installation = installation
            if self.group is not None:
                self.load_variant()

    def select(self, value: object) -> None:
        self.clear_plan()
        self.generation += 1
        if self.worker is not None and not self.applying:
            self.worker.cancel()
        self.worker = None
        self.group = value if isinstance(value, ProjectGroup) else None
        self.project, self.versions = None, ()
        self.variant.blockSignals(True)
        self.variant.clear()
        if self.group:
            for project in self.group.projects:
                self.variant.addItem(project.reference.source_id, project)
        self.variant.blockSignals(False)
        self.load_variant()

    def load_variant(self) -> None:
        self.generation += 1
        generation = self.generation
        if self.worker is not None and not self.applying:
            self.worker.cancel()
        self.worker = None
        self.clear_plan()
        self.versions = ()
        self.version.clear()
        self.file.clear()
        self.dependencies.clear()
        self.prepare.setEnabled(False)
        selected = self.variant.currentData()
        self.project = selected if isinstance(selected, SourceProject) else None
        self.website.setEnabled(bool(self.project and self.project.web_url))
        if self.project is None:
            self.status.setText("모드를 선택하세요.")
            return
        project = self.project
        installation = self.installation
        self.status.setText("버전과 의존성 정보를 조회하고 있습니다…")

        async def execute() -> ProjectDetails:
            async with source_registry() as registry:
                source = registry.get(project.reference.source_id)
                detailed = await source.project(project.game_id, project.reference.project_id)
                query = (
                    VersionQuery(
                        installation.version.value,
                        installation.loader.name if installation.loader else None,
                    )
                    if installation and installation.game_id == project.game_id
                    else None
                )
                versions = await source.versions(
                    project.game_id,
                    project.reference.project_id,
                    query=query if source.version_filters else None,
                )
                return ProjectDetails(detailed, versions)

        self.worker = self.start(
            lambda cancelled: run_async(execute(), cancelled),
            partial(self._loaded, generation),
            partial(self._failed, generation),
        )

    def _loaded(self, generation: int, value: object) -> None:
        if generation != self.generation:
            return
        assert isinstance(value, ProjectDetails)
        self.worker = None
        self.project, self.versions = value.project, value.versions
        self.description.setPlainText(
            value.project.name + "\n\n" + (value.project.description or value.project.summary)
        )
        self.website.setEnabled(bool(value.project.web_url))
        self.version.clear()
        for version in self.versions:
            self.version.addItem(
                f"{version.number or version.version_id} · {version.channel.value}",
                version.version_id,
            )
        self.status.setText(
            f"{len(self.versions)}개 출처 버전 · 게임 설치를 선택해야 적용할 수 있습니다."
        )
        self._version_changed()

    def _failed(self, generation: int, message: str) -> None:
        if generation == self.generation:
            self.worker = None
            self.prepare.setEnabled(self.selected_mod is not None and self.installation is not None)
            self.status.setText(message)

    @property
    def selected_mod(self) -> ResolvedMod | None:
        index = self.version.currentIndex()
        file = self.file.currentData()
        if (
            self.project is None
            or not 0 <= index < len(self.versions)
            or not isinstance(file, ModFile)
        ):
            return None
        return ResolvedMod(self.project, self.versions[index], file, True)

    def _version_changed(self) -> None:
        self.invalidate()
        self.file.clear()
        index = self.version.currentIndex()
        if not 0 <= index < len(self.versions):
            return
        version = self.versions[index]
        for file in version.files:
            self.file.addItem(
                f"{file.filename} · {file.size if file.size is not None else '?'} bytes", file
            )
        primary = next((i for i, file in enumerate(version.files) if file.primary), 0)
        self.file.setCurrentIndex(primary)
        relationships = []
        for dep in version.dependencies:
            target = dep.target
            identity = target.project_id or target.version_id or target.canonical_id
            identity = identity or target.filename or "?"
            constraint = dep.constraint.expression or ", ".join(
                sorted(dep.constraint.exact_versions)
            )
            relationships.append(
                f"{dep.kind.value}: {target.source_id or ''}:{identity} {constraint}"
            )
        self.dependencies.setPlainText(
            "\n".join(relationships)
            or ("선언된 의존성 없음" if version.dependencies_known else "출처 의존성 정보 미확인")
        )
        self._rule_changed()

    def _rule_changed(self) -> None:
        self.invalidate()
        self.rule.clear()
        self.rule.addItem("규칙 자동 선택 · 모호하면 차단", "")
        mod = self.selected_mod
        install = self.installation
        valid = mod is not None and install is not None and mod.project.game_id == install.game_id
        if valid:
            assert install is not None and mod is not None
            try:
                rules = self.games.get(install.game_id).installation_rules(install, mod.file)
                for rule in rules:
                    self.rule.addItem(rule.rule_id, rule.rule_id)
            except ModMeshError as exc:
                self.status.setText(exc.message)
                valid = False
        self.prepare.setEnabled(valid and not self.applying)

    def open_website(self) -> None:
        if self.project and self.project.web_url:
            QDesktopServices.openUrl(QUrl(self.project.web_url))

    def prepare_selected(self) -> None:
        mod, installation = self.selected_mod, self.installation
        if mod is None or installation is None or not self.prepare.isEnabled():
            return
        self.clear_plan()
        self.generation += 1
        generation = self.generation
        rule, prefix, replace = (
            str(self.rule.currentData()) or None,
            self.prefix.text().strip(),
            self.replace.isChecked(),
        )
        self.prepare.setEnabled(False)
        self.status.setText("의존성 해결·다운로드·검증 후 계획을 준비합니다…")

        def work(cancelled: threading.Event) -> PreparedInstallation:
            async def execute() -> PreparedInstallation:
                async with source_registry() as registry:
                    access = await registry.get(mod.project.reference.source_id).resolve_download(
                        mod.project.game_id, mod.file
                    )
                    if access.mode != DownloadMode.DIRECT:
                        raise ModMeshError(
                            ErrorCode.UNSUPPORTED,
                            access.reason
                            or "직접 다운로드가 허용되지 않습니다. 공식 페이지에서 확인하세요.",
                        )
                    with MetadataStore(self.settings.paths.database) as store:
                        resolution = await DependencyResolver(
                            registry,
                            identities=IdentityRegistry(store),
                            priorities=source_priorities(self.settings.source_priorities),
                        ).resolve(
                            installation.game_id,
                            (mod,),
                            game_version=installation.version.value,
                            loader=installation.loader.name if installation.loader else None,
                        )
                    return await prepare_resolution(
                        self.settings,
                        registry,
                        installation,
                        resolution,
                        rule=rule,
                        prefix=prefix,
                        replace_existing=replace,
                        cancelled=cancelled,
                    )

            return run_async(execute(), cancelled)

        self.worker = self.start(
            work, partial(self._prepared, generation), partial(self._failed, generation)
        )

    def _prepared(self, generation: int, value: object) -> None:
        assert isinstance(value, PreparedInstallation)
        if generation != self.generation:
            value.close()
            return
        self.worker = None
        self.prepared = value
        self.prepare.setEnabled(True)
        self.status.setText("계획을 검토하고 승인해야 게임 파일에 적용됩니다.")
        dialog = PlanDialog(value, self)
        self.dialog = dialog
        dialog.accepted.connect(self.apply_prepared)
        dialog.rejected.connect(self.clear_plan)
        dialog.open()

    def apply_prepared(self) -> None:
        prepared = self.prepared
        if prepared is None:
            return
        self.prepared = None
        self.dialog = None
        self.applying = True
        self.prepare.setEnabled(False)
        self.status.setText("승인한 변경 계획을 적용하고 있습니다…")
        generation = self.generation

        def work(cancelled: threading.Event) -> OperationResult:
            try:
                return prepared.manager.apply(
                    prepared.plan,
                    prepared.plan.fingerprint,
                    replace_existing=prepared.replace_existing,
                    cancelled=cancelled.is_set,
                )
            finally:
                prepared.close()

        self.worker = self.start(
            work, partial(self._applied, generation), partial(self._failed, generation)
        )

    def _apply_failed(self, generation: int, message: str) -> None:
        self.applying = False
        self._failed(generation, message)
        self._rule_changed()

    def _applied(self, generation: int, value: object) -> None:
        self.applying = False
        assert isinstance(value, OperationResult)
        self.changed()
        if generation == self.generation:
            self.worker = None
            self.prepare.setEnabled(True)
            self.status.setText(f"설치 기록 완료 · {value.operation_id} · {value.state}")
