from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from modmesh.apps.desktop.widgets import fill_row, table
from modmesh.apps.desktop.workers import Worker, run_async
from modmesh.apps.runtime import source_priorities, source_registry
from modmesh.core.config import ClientSettings
from modmesh.core.identity import IdentityRegistry
from modmesh.core.matching import MatchingResult, ProjectGroup, ProjectMatcher
from modmesh.core.search import UnifiedSearch, UnifiedSearchResult
from modmesh.core.sources import SearchRequest, SearchSort
from modmesh.domain.models import GameInstallation, SourceProject
from modmesh.storage.metadata import MetadataStore

type WorkStarter = Callable[
    [Callable[[threading.Event], object], Callable[[object], None], Callable[[str], None] | None],
    Worker,
]


@dataclass(frozen=True)
class SearchOutput:
    result: UnifiedSearchResult
    matching: MatchingResult


class SearchPanel(QWidget):
    selected = Signal(object)

    def __init__(self, settings: ClientSettings, store: MetadataStore, start: WorkStarter) -> None:
        super().__init__()
        self.settings, self.store, self.start = settings, store, start
        self.groups: tuple[ProjectGroup, ...] = ()
        self.projects: tuple[SourceProject, ...] = ()
        self.offsets: dict[str, int] = {}
        self.request: SearchRequest | None = None
        self.worker: Worker | None = None
        self.generation = 0
        self.installation: GameInstallation | None = None
        layout = QVBoxLayout(self)
        title = QLabel("모드 탐색")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(QLabel("출처별 권한과 호환 조건을 확인하며 탐색합니다."))
        query = QHBoxLayout()
        self.game = QComboBox()
        self.game.addItem("Palworld", "palworld")
        self.game.addItem("Minecraft Java", "minecraft")
        self.game.currentIndexChanged.connect(self._game_changed)
        query.addWidget(self.game)
        self.query = QLineEdit()
        self.query.setMaxLength(256)
        self.query.setPlaceholderText("모드 이름 또는 검색어")
        self.query.returnPressed.connect(self.search)
        query.addWidget(self.query, 1)
        self.submit = QPushButton("검색")
        self.submit.setObjectName("primary")
        self.submit.clicked.connect(self.search)
        query.addWidget(self.submit)
        layout.addLayout(query)
        filters = QHBoxLayout()
        self.source = QComboBox()
        for label, key in (
            ("모든 출처", ""),
            ("Nexus Mods", "nexus"),
            ("Modrinth", "modrinth"),
            ("CurseForge", "curseforge"),
        ):
            self.source.addItem(label, key)
        filters.addWidget(self.source)
        self.version = QLineEdit()
        self.version.setPlaceholderText("게임 버전 · 미입력 시 전체")
        filters.addWidget(self.version)
        self.loader = QComboBox()
        for loader in ("", "fabric", "forge", "neoforge", "quilt"):
            self.loader.addItem(loader or "모든 로더", loader)
        filters.addWidget(self.loader)
        self.sort = QComboBox()
        for label, key in (
            ("관련도", "relevance"),
            ("다운로드", "downloads"),
            ("최근 변경", "updated"),
        ):
            self.sort.addItem(label, key)
        filters.addWidget(self.sort)
        layout.addLayout(filters)
        self.context_label = QLabel("게임 설치 없이 출처 목록을 탐색할 수 있습니다.")
        self.context_label.setWordWrap(True)
        layout.addWidget(self.context_label)
        split = QSplitter()
        self.split = split
        self.results = table(("모드", "출처", "제작자"))
        self.results.itemSelectionChanged.connect(self._selected)
        split.addWidget(self.results)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        split.addWidget(self.details)
        split.setSizes([600, 300])
        layout.addWidget(split, 1)
        self.status = QTextEdit()
        self.status.setReadOnly(True)
        self.status.setMaximumHeight(145)
        self.status.setPlainText("검색 후 출처별 상태와 제한을 표시합니다.")
        layout.addWidget(self.status)
        paging = QHBoxLayout()
        self.count = QLabel("검색 결과 없음")
        paging.addWidget(self.count, 1)
        self.more = QPushButton("다음 결과")
        self.more.setEnabled(False)
        self.more.clicked.connect(self.search_more)
        paging.addWidget(self.more)
        layout.addLayout(paging)
        self._game_changed()

    def _game_changed(self) -> None:
        self.generation += 1
        if self.worker is not None:
            self.worker.cancel()
        self.worker = None
        self.submit.setEnabled(True)
        self.groups, self.projects, self.offsets = (), (), {}
        self.results.setRowCount(0)
        self.details.clear()
        self.more.setEnabled(False)
        saved = self.store.get("desktop.search", str(self.game.currentData())) or {}
        self.query.setText(str(saved.get("query", "")))
        self.version.setText(str(saved.get("game_version", "")))
        for widget, field in (
            (self.loader, "loader"),
            (self.source, "source"),
            (self.sort, "sort"),
        ):
            index = widget.findData(str(saved.get(field, "")))
            widget.setCurrentIndex(max(0, index))
        self.selected.emit(None)

    def set_context(self, installation: GameInstallation | None) -> None:
        if installation == self.installation:
            return
        self.installation = installation
        if installation is not None:
            index = self.game.findData(installation.game_id)
            self.game.setCurrentIndex(index)
            self.version.setText(installation.version.value or "")
            loader = installation.loader.name if installation.loader else ""
            self.loader.setCurrentIndex(max(0, self.loader.findData(loader)))
            self.context_label.setText(
                f"선택 설치: {installation.path}\n설치 계획은 실제 게임 환경으로 다시 검증합니다."
            )
        else:
            self.context_label.setText("설치 경로가 선택되지 않았습니다. 출처 탐색만 가능합니다.")

    def search(self) -> None:
        if not self.submit.isEnabled():
            return
        self.request = SearchRequest(
            str(self.game.currentData()),
            self.query.text().strip(),
            self.version.text().strip() or None,
            str(self.loader.currentData()) or None,
            sort=SearchSort(str(self.sort.currentData())),
        )
        self.store.put(
            "desktop.search",
            self.request.game_id,
            {
                "query": self.request.query,
                "game_version": self.request.game_version or "",
                "loader": self.request.loader or "",
                "source": str(self.source.currentData()),
                "sort": self.request.sort.value,
            },
        )
        self._start(False)

    def search_more(self) -> None:
        if self.request is not None and self.offsets and self.submit.isEnabled():
            self._start(True)

    def _start(self, append: bool) -> None:
        assert self.request is not None
        self.generation += 1
        generation = self.generation
        request = self.request
        offsets = dict(self.offsets) if append else None
        source = str(self.source.currentData())
        sources = tuple(self.offsets) if append else ((source,) if source else None)
        previous = self.projects if append else ()
        self.submit.setEnabled(False)
        self.more.setEnabled(False)
        self.status.setPlainText("출처를 조회하고 있습니다…")

        async def execute() -> SearchOutput:
            async with source_registry() as registry:
                result = await UnifiedSearch(
                    registry, priorities=source_priorities(self.settings.source_priorities)
                ).search(request, source_ids=sources, offsets=offsets)
            projects = tuple(
                {project.reference: project for project in (*previous, *result.projects)}.values()
            )
            with MetadataStore(self.settings.paths.database) as store:
                matching = ProjectMatcher(IdentityRegistry(store)).match(projects)
            return SearchOutput(result, matching)

        self.worker = self.start(
            lambda cancelled: run_async(execute(), cancelled),
            partial(self._loaded, generation),
            partial(self._failed, generation),
        )

    def _failed(self, generation: int, message: str) -> None:
        if generation == self.generation:
            self.worker = None
            self.submit.setEnabled(True)
            self.more.setEnabled(bool(self.offsets))
            self.status.setPlainText(message)

    def _loaded(self, generation: int, value: object) -> None:
        if generation != self.generation:
            return
        assert isinstance(value, SearchOutput)
        self.worker = None
        self.submit.setEnabled(True)
        self.groups = value.matching.groups
        self.projects = tuple(project for group in self.groups for project in group.projects)
        self.offsets = value.result.next_offsets
        self.results.setRowCount(len(self.groups))
        for row, group in enumerate(self.groups):
            project = group.projects[0]
            fill_row(
                self.results,
                row,
                (
                    project.name,
                    ", ".join(item.reference.source_id for item in group.projects),
                    ", ".join(project.authors) or "미확인",
                ),
            )
        messages = []
        for source in value.result.sources:
            text = f"{source.source_id}: {source.state.value}"
            if source.error:
                text += " · " + str(source.error.get("message", ""))
            if source.page:
                text += f" · 범위 {source.page.scope.value} · {len(source.page.projects)}개"
                text += "\n" + "\n".join(source.page.warnings)
            if source.handoff_url:
                text += "\n공식 목록: " + source.handoff_url
            messages.append(text)
        if value.matching.candidates:
            messages.append(
                f"동일성 미확인 후보 {len(value.matching.candidates)}쌍 · 별도 결과로 유지"
            )
        self.status.setPlainText("\n\n".join(messages))
        self.count.setText(f"{len(self.groups)}개 결과 · 호환성과 설치 가능 여부는 상세에서 확인")
        self.more.setEnabled(bool(self.offsets) and len(self.projects) <= 900)
        if self.groups:
            self.results.selectRow(0)
        else:
            self.details.setPlainText("조건에 맞는 결과가 없습니다. 출처 상태와 필터를 확인하세요.")
            self.selected.emit(None)

    @property
    def selected_group(self) -> ProjectGroup | None:
        row = self.results.currentRow()
        return self.groups[row] if 0 <= row < len(self.groups) else None

    def _selected(self) -> None:
        group = self.selected_group
        if group is None:
            return
        project = group.projects[0]
        self.details.setPlainText(
            "\n\n".join(
                (
                    project.name,
                    project.summary,
                    "제작자: " + (", ".join(project.authors) or "미확인"),
                    "출처 ID: " + project.reference.source_id + ":" + project.reference.project_id,
                    "확인된 매핑"
                    if len(group.projects) > 1
                    else "이름 유사성만으로 다른 출처와 병합하지 않습니다.",
                    project.web_url or "공식 페이지 정보 없음",
                )
            )
        )
        self.selected.emit(group)
