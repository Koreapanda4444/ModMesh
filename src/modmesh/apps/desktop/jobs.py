from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import partial
from uuid import uuid4

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from modmesh.apps.desktop.search import WorkStarter
from modmesh.apps.desktop.widgets import fill_row, table
from modmesh.apps.desktop.workers import Worker
from modmesh.core.diagnostics import redact
from modmesh.core.downloads import DownloadProgress
from modmesh.core.games import GameRegistry
from modmesh.core.installation import InstallationManager, OperationProgress, OperationResult
from modmesh.domain.models import GameInstallation


@dataclass
class Task:
    name: str
    worker: Worker | None
    retry: Callable[[], None] | None
    task_id: str = field(default_factory=lambda: str(uuid4()))
    state: str = "queued"
    completed: int = 0
    total: int | None = None
    logs: list[str] = field(default_factory=list)


class JobsPanel(QWidget):
    def __init__(self, games: GameRegistry, start: WorkStarter) -> None:
        super().__init__()
        self.games, self.start = games, start
        self.tasks: list[Task] = []
        self.pending: list[Task] = []
        self.running: set[str] = set()
        self.installation: GameInstallation | None = None
        self.generation = 0
        layout = QVBoxLayout(self)
        title = QLabel("다운로드 / 작업")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(QLabel("준비·검증·적용 단계와 오류를 확인하고 작업을 취소할 수 있습니다."))
        self.list = table(("작업", "상태", "진행", "식별"))
        self.list.itemSelectionChanged.connect(self._selected)
        layout.addWidget(self.list, 2)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        self.cancel = QPushButton("선택 작업 취소")
        self.cancel.clicked.connect(self.cancel_selected)
        buttons.addWidget(self.cancel)
        self.retry = QPushButton("현재 조건으로 재시도")
        self.retry.clicked.connect(self.retry_selected)
        buttons.addWidget(self.retry)
        layout.addLayout(buttons)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 1)
        recovery = QHBoxLayout()
        recovery.addWidget(QLabel("선택 게임의 영속 작업 저널"), 1)
        refresh = QPushButton("저널 새로고침")
        refresh.clicked.connect(self.refresh_history)
        recovery.addWidget(refresh)
        self.recover = QPushButton("중단 작업 복구")
        self.recover.setEnabled(False)
        self.recover.clicked.connect(self.recover_pending)
        recovery.addWidget(self.recover)
        layout.addLayout(recovery)
        self.history = QTextEdit()
        self.history.setReadOnly(True)
        self.history.setMaximumHeight(120)
        layout.addWidget(self.history)
        self._selected()

    def enqueue(self, worker: Worker, name: str, retry: Callable[[], None] | None) -> None:
        task = Task(name, worker, retry)
        self.tasks.append(task)
        self.pending.append(task)
        worker.progress.connect(partial(self._progress, task))
        worker.succeeded.connect(partial(self._succeeded, task))
        worker.failed.connect(partial(self._failed, task))
        worker.finished.connect(partial(self._finished, task))
        self._draw(task)
        QTimer.singleShot(0, self._dispatch)

    def _dispatch(self) -> None:
        while self.pending and len(self.running) < 3:
            task = self.pending.pop(0)
            assert task.worker is not None
            self.running.add(task.task_id)
            task.state = "running"
            self._draw(task)
            task.worker.start()

    def _progress(self, task: Task, event: object) -> None:
        if isinstance(event, DownloadProgress):
            task.state = event.state.value
            task.completed, task.total = event.downloaded, event.total
            message = json.dumps(redact(asdict(event)), ensure_ascii=False, default=str)
        elif isinstance(event, OperationProgress):
            task.state = event.state
            task.completed, task.total = event.completed, event.total
            message = json.dumps(asdict(event), ensure_ascii=False)
        else:
            task.state = "preparing"
            task.total = None
            message = str(redact(event))
        if len(task.logs) >= 200:
            del task.logs[0]
        task.logs.append(message)
        self._draw(task)

    def _succeeded(self, task: Task, result: object) -> None:
        task.state = "completed"
        if isinstance(result, OperationResult):
            task.state = result.state
            task.logs.append(json.dumps(asdict(result), ensure_ascii=False))
        elif isinstance(result, tuple) and all(
            isinstance(item, OperationResult) for item in result
        ):
            task.logs.extend(json.dumps(asdict(item), ensure_ascii=False) for item in result)
            if any(item.state == "recovery_required" for item in result):
                task.state = "recovery_required"
        task.completed, task.total = 1, 1
        self._draw(task)

    def _failed(self, task: Task, message: str) -> None:
        task.state = "cancelled" if '"code": "cancelled"' in message else "failed"
        task.logs.append(message)
        self._draw(task)

    def _finished(self, task: Task) -> None:
        task.worker = None
        self.running.discard(task.task_id)
        self._dispatch()
        self._selected()

    def _draw(self, task: Task) -> None:
        row = self.tasks.index(task)
        self.list.setRowCount(len(self.tasks))
        progress = (
            f"{task.completed:,} / {task.total:,}" if task.total is not None else "대기 / 진행 중"
        )
        fill_row(self.list, row, (task.name, task.state, progress, task.task_id[:8]))
        if self.list.currentRow() == row:
            self._selected()

    def selected_task(self) -> Task | None:
        row = self.list.currentRow()
        return self.tasks[row] if 0 <= row < len(self.tasks) else None

    def _selected(self) -> None:
        task = self.selected_task()
        active = task is not None and task.worker is not None
        self.cancel.setEnabled(active and task is not None and task.name != "중단 작업 복구")
        self.retry.setEnabled(
            task is not None
            and not active
            and task.state in {"failed", "cancelled"}
            and task.retry is not None
        )
        if task is None:
            self.progress.setValue(0)
            self.details.setPlainText(
                "작업을 선택하세요. 파일 변경 실패는 새 변경 계획을 검토해 다시 적용합니다."
            )
            return
        if task.total:
            self.progress.setRange(0, 100)
            self.progress.setValue(min(100, task.completed * 100 // task.total))
        elif active:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
        self.details.setPlainText("\n".join(task.logs) or f"{task.name}: {task.state}")
        self.details.verticalScrollBar().setValue(self.details.verticalScrollBar().maximum())

    def cancel_selected(self) -> None:
        task = self.selected_task()
        if task and task.worker is not None:
            task.worker.cancel()
            task.logs.append("취소 요청 · 파일 적용 중이면 복구가 끝난 뒤 종료됩니다.")
            self._draw(task)

    def retry_selected(self) -> None:
        task = self.selected_task()
        if task and self.retry.isEnabled() and task.retry:
            task.retry()

    def set_context(self, installation: GameInstallation | None) -> None:
        if self.installation != installation:
            self.installation = installation
            self.generation += 1
            self.history.clear()
            self.recover.setEnabled(False)
            self.refresh_history()

    def refresh_history(self) -> None:
        if self.installation is None:
            return
        installation, generation = self.installation, self.generation
        self.start(
            lambda cancelled: InstallationManager(self.games, installation).history(),
            partial(self._history_loaded, generation),
            name="작업 저널 조회",
            retry=self.refresh_history,
        )

    def _history_loaded(self, generation: int, value: object) -> None:
        if generation != self.generation:
            return
        assert isinstance(value, tuple)
        records = tuple(item for item in value if isinstance(item, OperationResult))
        self.history.setPlainText(
            "\n".join(
                f"{item.operation_id} · {item.state} · {'; '.join(item.issues)}" for item in records
            )
            or "기록된 파일 변경 작업이 없습니다."
        )
        self.recover.setEnabled(
            any(item.state not in {"committed", "rolled_back", "failed"} for item in records)
        )

    def recover_pending(self) -> None:
        if self.installation is None or not self.recover.isEnabled():
            return
        answer = QMessageBox.question(
            self,
            "중단 작업 복구",
            "게임을 종료한 뒤 진행하세요. 저널과 소유 근거로 중단 전 상태를 복구합니다. "
            "사용자 변경으로 막히면 파일을 보존하고 남은 문제를 표시합니다.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.apply_recovery()

    def apply_recovery(self) -> None:
        if self.installation is None:
            return
        installation = self.installation
        self.recover.setEnabled(False)
        self.start(
            lambda cancelled: InstallationManager(self.games, installation).recover(),
            lambda value: self.refresh_history(),
            name="중단 작업 복구",
        )
