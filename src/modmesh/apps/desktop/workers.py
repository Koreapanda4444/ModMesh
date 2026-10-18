from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable

from PySide6.QtCore import QThread, Signal

from modmesh.core.diagnostics import ErrorCode, ModMeshError, redact_text


class Worker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(object)

    def __init__(self, work: Callable[[threading.Event], object]) -> None:
        super().__init__()
        self.work = work
        self.name = "작업"
        self.always_start = False
        self.cancelled = threading.Event()

    def run(self) -> None:
        try:
            if self.cancelled.is_set() and not self.always_start:
                raise ModMeshError(ErrorCode.CANCELLED, "Queued operation cancelled")
            self.succeeded.emit(self.work(self.cancelled))
        except ModMeshError as exc:
            self.failed.emit(json.dumps(exc.as_dict(), ensure_ascii=False, default=str))
        except Exception as exc:
            self.failed.emit(redact_text(f"{type(exc).__name__}: {exc}"))

    def cancel(self) -> None:
        self.cancelled.set()


def run_async[T](awaitable: Awaitable[T], cancelled: threading.Event) -> T:
    async def execute() -> T:
        task = asyncio.ensure_future(awaitable)
        try:
            while not task.done():
                if cancelled.is_set():
                    task.cancel()
                    break
                await asyncio.sleep(0.05)
            return await task
        except asyncio.CancelledError as exc:
            raise ModMeshError(ErrorCode.CANCELLED, "Operation cancelled") from exc
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return asyncio.run(execute())


def report_progress(event: object) -> None:
    worker = QThread.currentThread()
    if isinstance(worker, Worker):
        worker.progress.emit(event)
