from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable

from PySide6.QtCore import QThread, Signal

from modmesh.core.diagnostics import ErrorCode, ModMeshError, redact_text


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
