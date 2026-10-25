from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import asdict
from functools import partial
from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from modmesh.apps.desktop.accounts import AccountPanel
from modmesh.apps.desktop.details import PlanDialog
from modmesh.apps.desktop.profiles import ProfileDialog
from modmesh.apps.desktop.search import WorkStarter
from modmesh.apps.desktop.workers import report_progress, run_async
from modmesh.apps.operations import (
    PreparedInstallation,
    PreparedPackUpdate,
    prepare_pack,
    prepare_pack_update,
)
from modmesh.apps.runtime import source_registry
from modmesh.core.config import ClientSettings
from modmesh.core.games import GameRegistry
from modmesh.core.installation import OperationResult
from modmesh.core.pack_io import export_profile, read_manifest, write_manifest
from modmesh.core.pack_versions import PackDiff, diff_manifests
from modmesh.core.packs import PackManifest
from modmesh.core.profiles import ProfileManager
from modmesh.domain.models import GameInstallation
from modmesh.remote.sync import RemotePacks, RemoteSnapshot
from modmesh.storage.metadata import MetadataStore


class PacksPanel(QWidget):
    def __init__(
        self,
        settings: ClientSettings,
        games: GameRegistry,
        start: WorkStarter,
        changed: Callable[[], None],
    ) -> None:
        super().__init__()
        self.settings, self.games, self.start, self.changed = settings, games, start, changed
        self.installation: GameInstallation | None = None
        self.generation = 0
        self.manifest: PackManifest | None = None
        self.prepared: PreparedInstallation | None = None
        self.dialog: PlanDialog | ProfileDialog | None = None
        self.prepared_update: PreparedPackUpdate | None = None
        self.applying = False
        self.accounts: AccountPanel | None = None
        self.remote_snapshot: RemoteSnapshot | None = None
        layout = QVBoxLayout(self)
        title = QLabel("모드팩")
        title.setObjectName("heading")
        layout.addWidget(title)
        note = QLabel(
            "바이너리와 개인 정보는 제외합니다. Import는 출처 확인과 변경 계획 검토가 필요합니다."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        export_row = QHBoxLayout()
        self.profile = QComboBox()
        export_row.addWidget(self.profile, 1)
        self.refresh_button = QPushButton("프로필 새로고침")
        self.refresh_button.clicked.connect(self.refresh)
        export_row.addWidget(self.refresh_button)
        self.export_button = QPushButton("선택 프로필 Export")
        self.export_button.clicked.connect(self.choose_export)
        export_row.addWidget(self.export_button)
        layout.addLayout(export_row)
        import_row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText("로컬 .modmesh.json 경로")
        self.path.textChanged.connect(self.invalidate)
        import_row.addWidget(self.path, 1)
        choose = QPushButton("파일 선택")
        choose.clicked.connect(self.choose_import)
        import_row.addWidget(choose)
        self.read_button = QPushButton("Manifest 검증·미리보기")
        self.read_button.clicked.connect(self.read)
        import_row.addWidget(self.read_button)
        layout.addLayout(import_row)
        remote_row = QHBoxLayout()
        self.remote_code = QLineEdit()
        self.remote_code.setPlaceholderText("선택 서버의 공유 코드 · 비공개 코드는 매번 입력")
        self.remote_code.textChanged.connect(self.invalidate)
        remote_row.addWidget(self.remote_code, 1)
        remote_fetch = QPushButton("공유 코드 Import")
        remote_fetch.clicked.connect(self.fetch_remote)
        remote_row.addWidget(remote_fetch)
        remote_check = QPushButton("적용한 원격 팩 업데이트 확인")
        remote_check.clicked.connect(lambda: self.fetch_remote(True))
        remote_row.addWidget(remote_check)
        layout.addLayout(remote_row)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview, 1)
        self.install = QPushButton("현재 프로필에 Import 계획")
        self.install.clicked.connect(self.prepare_import)
        self.install.setEnabled(False)
        layout.addWidget(self.install)
        compare_row = QHBoxLayout()
        self.previous_path = QLineEdit()
        self.previous_path.setPlaceholderText("비교할 이전 Manifest JSON 경로")
        compare_row.addWidget(self.previous_path, 1)
        compare_button = QPushButton("버전 차이 비교")
        compare_button.clicked.connect(self.compare)
        compare_row.addWidget(compare_button)
        self.update_button = QPushButton("팩 변경·버전 계획")
        self.update_button.clicked.connect(self.prepare_update)
        compare_row.addWidget(self.update_button)
        layout.addLayout(compare_row)
        self.status = QLabel("게임 설치와 로컬 Manifest를 선택하세요.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def invalidate(self) -> None:
        self.generation += 1
        self.manifest = None
        self.remote_snapshot = None
        self.install.setEnabled(False)
        self.clear_plan()

    def bind_accounts(self, accounts: AccountPanel) -> None:
        self.accounts = accounts
        accounts.state_changed.connect(self.server_changed)

    def server_changed(self, label: str) -> None:
        self.invalidate()

    def remote_key(self) -> str:
        assert self.installation is not None
        profile = ProfileManager(self.games, self.installation).active_id() or "default"
        return self.installation.installation_id + ":" + profile

    def fetch_remote(self, check: bool = False) -> None:
        if self.applying or not self.accounts or not self.accounts.client:
            self.status.setText(
                "계정 화면에서 서버를 선택하세요. 공개 코드는 게스트도 사용할 수 있습니다."
            )
            return
        self.clear_plan()
        self.generation += 1
        generation, client = self.generation, self.accounts.client
        code, authenticated = self.remote_code.text().strip(), self.accounts.account is not None
        local_key = self.remote_key() if self.installation else None

        def work(cancelled: threading.Event) -> RemoteSnapshot:
            with MetadataStore(self.settings.paths.database) as store:
                remote = RemotePacks(client, store)
                previous = remote.receipt(local_key) if check and local_key else None
                if check and previous is None:
                    raise ValueError("No remote pack is recorded for the selected profile")
                return remote.fetch(code, previous=previous, authenticated=authenticated)

        def loaded(value: object) -> None:
            if generation != self.generation:
                return
            assert isinstance(value, RemoteSnapshot)
            self.loaded(generation, value.manifest)
            self.remote_snapshot = value
            if value.diff:
                self.preview.setPlainText(
                    json.dumps(asdict(value.diff), ensure_ascii=False, indent=2)
                )
            self.status.setText("원격 Manifest 확인 완료 · 변경 계획을 검토하고 승인하세요.")

        self.start(work, loaded, partial(self.failed, generation), name="원격 팩 조회")

    def clear_plan(self) -> None:
        self.prepared_update = None
        if self.dialog:
            dialog, self.dialog = self.dialog, None
            dialog.reject()
        if self.prepared:
            self.prepared.close()
            self.prepared = None

    def set_context(self, installation: GameInstallation | None) -> None:
        if installation != self.installation:
            self.invalidate()
            self.installation = installation
            self.profile.clear()
            self.refresh()

    def refresh(self) -> None:
        if self.installation is None:
            return
        manager, generation = ProfileManager(self.games, self.installation), self.generation
        self.start(
            lambda cancelled: manager.profiles(),
            partial(self.profiles_loaded, generation),
            partial(self.failed, generation),
            name="팩 프로필 조회",
        )

    def profiles_loaded(self, generation: int, value: object) -> None:
        if generation != self.generation:
            return
        assert isinstance(value, tuple)
        self.profile.clear()
        for profile in value:
            self.profile.addItem(profile.name, profile.profile_id)

    def choose_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "모드팩 Export", "pack.modmesh.json", "ModMesh (*.json)"
        )
        if path:
            self.export_to(Path(path))

    def export_to(self, destination: Path) -> None:
        if self.installation is None or not self.profile.currentData() or self.applying:
            return
        manager = ProfileManager(self.games, self.installation)
        identity, generation = str(self.profile.currentData()), self.generation

        def work(cancelled: threading.Event) -> PackManifest:
            manifest = export_profile(manager, identity)
            write_manifest(destination, manifest)
            return manifest

        self.start(
            work,
            partial(self.exported, generation),
            partial(self.failed, generation),
            name="로컬 팩 Export",
        )

    def exported(self, generation: int, value: object) -> None:
        if generation == self.generation:
            assert isinstance(value, PackManifest)
            self.preview.setPlainText(json.dumps(value.document(), ensure_ascii=False, indent=2))
            self.status.setText("Export 완료 · 바이너리와 설정 내용은 포함하지 않았습니다.")

    def choose_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "모드팩 Import", "", "ModMesh (*.json)")
        if path:
            self.path.setText(path)
            self.read()

    def read(self) -> None:
        path, generation = Path(self.path.text()), self.generation
        self.start(
            lambda cancelled: read_manifest(path),
            partial(self.loaded, generation),
            partial(self.failed, generation),
            name="로컬 팩 검증",
        )

    def loaded(self, generation: int, value: object) -> None:
        if generation != self.generation:
            return
        assert isinstance(value, PackManifest)
        self.remote_snapshot = None
        self.manifest = value
        self.preview.setPlainText(json.dumps(value.document(), ensure_ascii=False, indent=2))
        self.install.setEnabled(self.installation is not None and not self.applying)
        self.status.setText(
            "\n".join(value.warnings)
            or "Manifest 검증 완료 · 출처·파일은 Import 계획에서 재검증합니다."
        )

    def prepare_import(self) -> None:
        if self.installation is None or self.manifest is None or self.applying:
            return
        self.clear_plan()
        installation, manifest, generation = self.installation, self.manifest, self.generation
        self.install.setEnabled(False)

        def work(cancelled: threading.Event) -> PreparedInstallation:
            async def execute() -> PreparedInstallation:
                async with source_registry() as registry:
                    return await prepare_pack(
                        self.settings,
                        registry,
                        installation,
                        manifest,
                        cancelled=cancelled,
                        progress=report_progress,
                    )

            return run_async(execute(), cancelled)

        self.start(
            work,
            partial(self.ready, generation),
            partial(self.failed, generation),
            name="로컬 팩 Import 계획",
        )

    def ready(self, generation: int, value: object) -> None:
        assert isinstance(value, PreparedInstallation)
        if generation != self.generation:
            value.close()
            return
        self.prepared = value
        self.dialog = PlanDialog(value, self)
        self.dialog.accepted.connect(self.apply)
        self.dialog.rejected.connect(self.clear_plan)
        self.dialog.open()
        self.install.setEnabled(True)

    def apply(self) -> None:
        prepared, self.prepared = self.prepared, None
        self.dialog = None
        if prepared is None:
            return
        self.applying = True
        self.install.setEnabled(False)
        generation = self.generation

        def work(cancelled: threading.Event) -> OperationResult:
            try:
                return prepared.manager.apply(
                    prepared.plan,
                    prepared.plan.fingerprint,
                    cancelled=cancelled.is_set,
                    progress=report_progress,
                )
            finally:
                prepared.close()

        self.start(
            work,
            partial(self.applied, generation),
            partial(self.failed, generation),
            name="팩 설치 적용",
        )

    def applied(self, generation: int, value: object) -> None:
        self.applying = False
        self.changed()
        if generation == self.generation:
            if (
                self.remote_snapshot
                and self.accounts
                and self.accounts.client
                and self.installation
            ):
                try:
                    with MetadataStore(self.settings.paths.database) as store:
                        RemotePacks(self.accounts.client, store).applied(
                            self.remote_key(), self.remote_snapshot
                        )
                except Exception:
                    self.status.setText("파일 적용 완료 · 원격 연결 기록 저장에 실패했습니다.")
                    return
            self.status.setText("모드팩 설치 적용 완료 · 실제 게임 로딩은 별도 확인이 필요합니다.")
            self.install.setEnabled(self.manifest is not None)

    def compare(self) -> None:
        if self.manifest is None:
            return
        target, generation, path = self.manifest, self.generation, Path(self.previous_path.text())
        self.start(
            lambda cancelled: diff_manifests(read_manifest(path), target),
            partial(self.compared, generation),
            partial(self.failed, generation),
            name="팩 버전 비교",
        )

    def compared(self, generation: int, value: object) -> None:
        if generation == self.generation:
            assert isinstance(value, PackDiff)
            self.preview.setPlainText(json.dumps(asdict(value), ensure_ascii=False, indent=2))
            self.status.setText(
                "추가·제거·업데이트와 설정 차이입니다. 실제 파일은 변경 계획에서 확인하세요."
            )

    def prepare_update(self) -> None:
        if self.installation is None or self.manifest is None or self.applying:
            return
        self.clear_plan()
        installation, manifest, generation = self.installation, self.manifest, self.generation

        def work(cancelled: threading.Event) -> PreparedPackUpdate:
            async def execute() -> PreparedPackUpdate:
                async with source_registry() as registry:
                    return await prepare_pack_update(
                        self.settings,
                        registry,
                        installation,
                        manifest,
                        cancelled=cancelled,
                        progress=report_progress,
                    )

            return run_async(execute(), cancelled)

        self.start(
            work,
            partial(self.update_ready, generation),
            partial(self.failed, generation),
            name="팩 변경 계획",
        )

    def update_ready(self, generation: int, value: object) -> None:
        if generation != self.generation:
            return
        assert isinstance(value, PreparedPackUpdate)
        self.prepared_update = value
        self.dialog = ProfileDialog(value.plan, self, value.notes)
        self.dialog.accepted.connect(self.apply_update)
        self.dialog.rejected.connect(self.clear_plan)
        self.dialog.open()
        self.status.setText(
            "새 팩 버전과 비활성 프로필을 저장했습니다. 승인하면 해당 구성으로 전환합니다."
        )

    def apply_update(self) -> None:
        prepared, self.prepared_update = self.prepared_update, None
        self.dialog = None
        if prepared is None:
            return
        self.applying = True
        self.install.setEnabled(False)
        generation = self.generation
        self.start(
            lambda cancelled: prepared.manager.switch(
                prepared.plan,
                prepared.plan.fingerprint,
                cancelled=cancelled.is_set,
                progress=report_progress,
            ),
            partial(self.applied, generation),
            partial(self.failed, generation),
            name="팩 버전 적용",
        )

    def failed(self, generation: int, message: str) -> None:
        self.applying = False
        if generation == self.generation:
            self.status.setText(message)
            self.install.setEnabled(self.manifest is not None and self.installation is not None)
