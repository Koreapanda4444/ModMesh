from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6.QtCore import QThread, Signal

from modmesh.core.diagnostics import ModMeshError, redact_text


class Worker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, work: Callable[[threading.Event], object]) -> None:
        super().__init__()
        self.work = work
        self.cancelled = threading.Event()

    def run(self) -> None:
        try:
            self.succeeded.emit(self.work(self.cancelled))
        except ModMeshError as exc:
            self.failed.emit(redact_text(f"{exc.code.value}: {exc.message}"))
        except Exception as exc:
            self.failed.emit(redact_text(f"{type(exc).__name__}: {exc}"))

    def cancel(self) -> None:
        self.cancelled.set()
