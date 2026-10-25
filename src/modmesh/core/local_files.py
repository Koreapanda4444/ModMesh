from __future__ import annotations

import os
import stat
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.downloads import DownloadResult, publish_file
from modmesh.core.hashing import verify_file
from modmesh.domain.models import ModFile


def copy_local(
    file: ModFile, source: Path, destination: Path, cancelled: threading.Event, max_bytes: int
) -> DownloadResult:
    if source.is_symlink():
        raise ModMeshError(ErrorCode.VALIDATION, "Local source became a link")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(source, flags | getattr(os, "O_NONBLOCK", 0)), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                raise ModMeshError(ErrorCode.VALIDATION, "Local source exceeds file policy")
            descriptor, name = tempfile.mkstemp(prefix=".modmesh-", dir=destination.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as output:
                total = 0
                while chunk := stream.read(1024 * 1024):
                    if cancelled.is_set():
                        raise ModMeshError(ErrorCode.CANCELLED, "Local copy cancelled")
                    total += len(chunk)
                    if total > before.st_size or total > max_bytes:
                        raise ModMeshError(ErrorCode.INTEGRITY, "Local source grew while copying")
                    if output.write(chunk) != len(chunk):
                        raise ModMeshError(ErrorCode.STORAGE, "Incomplete local copy")
                output.flush()
                os.fsync(output.fileno())
            after, current = os.fstat(stream.fileno()), source.stat(follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
            ) or before.st_mtime_ns != after.st_mtime_ns:
                raise ModMeshError(ErrorCode.INTEGRITY, "Local source changed while copying")
        artifact = verify_file(
            temporary, file.hashes, expected_size=file.size, cancelled=cancelled.is_set
        )
        if cancelled.is_set():
            raise ModMeshError(ErrorCode.CANCELLED, "Local copy cancelled")
        publish_file(temporary, destination)
        return DownloadResult(
            destination,
            artifact,
            file.project.source_id,
            file.file_id,
            file.project.source_id,
            str(uuid4()),
        )
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
