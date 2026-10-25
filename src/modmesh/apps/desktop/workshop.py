from __future__ import annotations

from functools import partial

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from modmesh.adapters.games.ark_evolved import WORKSHOP, ArkEvolvedAdapter, WorkshopState
from modmesh.apps.desktop.search import WorkStarter
from modmesh.apps.desktop.widgets import fill_row, table
from modmesh.domain.models import GameInstallation


class WorkshopPanel(QWidget):
    def __init__(self, start: WorkStarter) -> None:
        super().__init__()
        self.start, self.generation = start, 0
        self.installation: GameInstallation | None = None
        self.states: tuple[WorkshopState, ...] = ()
        layout = QVBoxLayout(self)
        title = QLabel("ARK Evolved · Steam Workshop")
        title.setObjectName("heading")
        layout.addWidget(title)
        note = QLabel(
            "로컬 파일·Steam 설치 기록·활성 설정을 읽습니다. "
            "구독·설치·업데이트는 Steam에서 진행하세요."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        refresh = QPushButton("설치 상태 새로고침")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        browse = QPushButton("ARK Workshop 열기")
        browse.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(WORKSHOP)))
        row.addWidget(browse)
        selected = QPushButton("선택 모드 · 구독 확인")
        selected.clicked.connect(self.open_selected)
        row.addWidget(selected)
        layout.addLayout(row)
        self.list = table(
            (
                "Workshop ID",
                ".mod",
                "게임 내용",
                "Steam 내용",
                "Steam 기록",
                "활성 설정",
                "기록 차이",
            )
        )
        layout.addWidget(self.list, 1)
        self.status = QLabel("ARK: Survival Evolved Steam Windows 설치를 선택하세요.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def set_context(self, installation: GameInstallation | None) -> None:
        self.generation += 1
        self.installation = (
            installation if installation and installation.game_id == "ark-evolved" else None
        )
        self.states = ()
        self.list.setRowCount(0)
        self.refresh()

    def refresh(self) -> None:
        if self.installation is None:
            self.status.setText("ARK: Survival Evolved Steam Windows 설치를 선택하세요.")
            return
        generation, installation = self.generation, self.installation

        def loaded(value: object) -> None:
            if generation != self.generation:
                return
            assert isinstance(value, tuple)
            self.states = value
            self.list.setRowCount(len(value))
            for index, item in enumerate(value):
                fill_row(
                    self.list,
                    index,
                    tuple(
                        str(value)
                        for value in (
                            item.item_id,
                            item.descriptor_present,
                            item.game_content_present,
                            item.workshop_content_present,
                            item.steam_install_record,
                            item.configured_active
                            if item.configured_active is not None
                            else "미확인",
                            item.manifest_mismatch,
                        )
                    ),
                )
            self.status.setText(
                "계정 구독 여부와 실제 게임 로딩은 Steam에서 확인하세요. "
                "로컬 파일만으로 구독을 단정하지 않습니다."
            )

        self.start(
            lambda cancelled: ArkEvolvedAdapter().states(installation),
            loaded,
            partial(self.failed, generation),
            name="ARK Workshop 상태 조회",
        )

    def failed(self, generation: int, message: str) -> None:
        if generation == self.generation:
            self.status.setText(message)

    def open_selected(self) -> None:
        index = self.list.currentRow()
        if 0 <= index < len(self.states):
            QDesktopServices.openUrl(QUrl(self.states[index].url))
