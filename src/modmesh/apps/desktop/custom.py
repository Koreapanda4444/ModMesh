from __future__ import annotations

import threading
from functools import partial

from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from modmesh.adapters.sources.custom import CustomSource
from modmesh.apps.desktop.search import WorkStarter
from modmesh.apps.desktop.widgets import fill_row, table
from modmesh.apps.runtime import create_game_registry
from modmesh.core.config import ClientSettings
from modmesh.core.games import GameCapability, GameRegistry
from modmesh.domain.models import GameInstallation
from modmesh.storage.installed import decode_mod


class CustomPanel(QWidget):
    def __init__(
        self, settings: ClientSettings, start: WorkStarter, *, games: GameRegistry | None = None
    ) -> None:
        super().__init__()
        self.settings, self.start = settings, start
        self.games = games or create_game_registry(settings)
        self.installation: GameInstallation | None = None
        self.generation = 0
        self.identities: list[str] = []
        layout = QVBoxLayout(self)
        title = QLabel("커스텀 모드 출처")
        title.setObjectName("heading")
        layout.addWidget(title)
        note = QLabel(
            "사용 권한이 있는 파일을 등록하세요. 등록 후 모드 탐색의 custom 출처에서 설치합니다."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.name = QLineEdit()
        self.name.setPlaceholderText("모드 이름")
        layout.addWidget(self.name)
        row = QHBoxLayout()
        self.location = QLineEdit()
        self.location.setPlaceholderText("로컬 파일 또는 공개 HTTPS 파일 URL")
        row.addWidget(self.location, 1)
        choose = QPushButton("파일 선택")
        choose.clicked.connect(self.choose)
        row.addWidget(choose)
        layout.addLayout(row)
        self.filename = QLineEdit()
        self.filename.setPlaceholderText("HTTPS 파일명 (예: mod.jar)")
        layout.addWidget(self.filename)
        self.checksum = QLineEdit()
        self.checksum.setPlaceholderText("HTTPS 파일의 SHA-256 · 로컬 파일은 직접 계산합니다.")
        layout.addWidget(self.checksum)
        self.dependencies = QCheckBox("필수 의존성이 없음을 확인했습니다")
        layout.addWidget(self.dependencies)
        row = QHBoxLayout()
        self.register_button = QPushButton("선택 게임·버전·로더로 등록")
        self.register_button.clicked.connect(self.register)
        row.addWidget(self.register_button)
        refresh = QPushButton("목록 새로고침")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        remove = QPushButton("선택 등록 삭제")
        remove.clicked.connect(self.remove)
        row.addWidget(remove)
        layout.addLayout(row)
        self.list = table(("모드", "파일", "SHA-256"))
        layout.addWidget(self.list, 1)
        self.status = QLabel("게임을 선택하세요. 파일 해시는 전송 무결성 확인에 사용합니다.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def set_context(self, installation: GameInstallation | None) -> None:
        self.generation += 1
        self.installation = installation
        self.register_button.setEnabled(
            bool(
                installation
                and GameCapability.INSTALL_RULES
                in self.games.get(installation.game_id).capabilities
            )
        )
        self.refresh()

    def choose(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "권한 있는 모드 파일 선택")
        if path:
            self.location.setText(path)

    def refresh(self) -> None:
        self.list.setRowCount(0)
        self.identities = []
        if self.installation is None:
            return
        game, generation = self.installation.game_id, self.generation

        def loaded(value: object) -> None:
            if generation != self.generation:
                return
            assert isinstance(value, tuple)
            mods = [decode_mod(record["mod"]) for record in value]
            mods = [mod for mod in mods if mod.project.game_id == game]
            self.list.setRowCount(len(mods))
            self.identities = [mod.project.reference.project_id for mod in mods]
            for index, mod in enumerate(mods):
                fill_row(
                    self.list,
                    index,
                    (mod.project.name, mod.file.filename, mod.file.hashes[0].value),
                )

        self.start(
            lambda cancelled: CustomSource(self.settings.paths.database).records(),
            loaded,
            partial(self.failed, generation),
            name="커스텀 출처 목록",
        )

    def failed(self, generation: int, message: str) -> None:
        self.register_button.setEnabled(True)
        if generation == self.generation:
            self.status.setText(message)

    def register(self) -> None:
        if not self.installation or not self.register_button.isEnabled():
            return
        installation, generation = self.installation, self.generation
        name, location = self.name.text(), self.location.text()
        filename, checksum = self.filename.text() or None, self.checksum.text() or None
        known = self.dependencies.isChecked()
        self.register_button.setEnabled(False)

        def work(cancelled: threading.Event) -> str:
            return CustomSource(self.settings.paths.database).register(
                installation.game_id,
                name,
                location,
                filename=filename,
                sha256=checksum,
                game_version=installation.version.value,
                loader=installation.loader.name if installation.loader else None,
                dependencies_known=known,
                cancelled=cancelled.is_set,
            )

        def registered(value: object) -> None:
            self.register_button.setEnabled(True)
            if generation == self.generation:
                self.status.setText("등록 완료 · 모드 탐색에서 custom 출처로 검색하세요.")
                self.refresh()

        self.start(work, registered, partial(self.failed, generation), name="커스텀 파일 해시·등록")

    def remove(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self.identities):
            identity = self.identities[row]
            self.start(
                lambda cancelled: CustomSource(self.settings.paths.database).remove(identity),
                lambda _: self.refresh(),
                partial(self.failed, self.generation),
                name="커스텀 등록 삭제",
            )
