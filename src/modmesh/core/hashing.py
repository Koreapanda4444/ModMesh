import hashlib
import os
import stat
from collections.abc import Callable, Iterable
from pathlib import Path

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.domain.artifacts import Artifact, FileHash, HashAlgorithm


def hash_file(
    path: Path,
    algorithms: Iterable[HashAlgorithm] = (HashAlgorithm.SHA256,),
    *,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int], None] | None = None,
) -> tuple[int, tuple[FileHash, ...]]:
    selected = sorted(set(algorithms), key=str)
    if not selected:
        raise ModMeshError(ErrorCode.VALIDATION, "At least one hash algorithm is required")
    digests = {
        algorithm: hashlib.new(
            algorithm.value,
            usedforsecurity=algorithm not in {HashAlgorithm.MD5, HashAlgorithm.SHA1},
        )
        for algorithm in selected
    }
    descriptor: int | None = None
    try:
        if path.is_symlink():
            raise ModMeshError(ErrorCode.VALIDATION, "Refusing to hash a symbolic link")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ModMeshError(ErrorCode.VALIDATION, "Only regular files can be hashed")
            size = 0
            while True:
                if cancelled is not None and cancelled():
                    raise ModMeshError(ErrorCode.CANCELLED, "Hashing cancelled")
                chunk = stream.read(min(1024 * 1024, before.st_size - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > before.st_size:
                    raise ModMeshError(ErrorCode.INTEGRITY, "File grew while being hashed")
                for digest in digests.values():
                    digest.update(chunk)
                if progress is not None:
                    progress(size)
            after = os.fstat(stream.fileno())
            current = path.stat(follow_symlinks=False)
            if (
                size != before.st_size
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise ModMeshError(ErrorCode.INTEGRITY, "File changed while being hashed")
    except OSError as exc:
        raise ModMeshError(ErrorCode.STORAGE, "Cannot read file for hashing") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return size, tuple(
        FileHash(algorithm, digest.hexdigest()) for algorithm, digest in digests.items()
    )


def verify_file(
    path: Path,
    expected: tuple[FileHash, ...] = (),
    *,
    expected_size: int | None = None,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int], None] | None = None,
) -> Artifact:
    if len({value.algorithm for value in expected}) != len(expected):
        raise ModMeshError(ErrorCode.VALIDATION, "Duplicate expected hash algorithm")
    size, hashes = hash_file(
        path,
        {HashAlgorithm.SHA256, *(value.algorithm for value in expected)},
        cancelled=cancelled,
        progress=progress,
    )
    actual = {value.algorithm: value for value in hashes}
    if expected_size is not None and size != expected_size:
        raise ModMeshError(ErrorCode.INTEGRITY, "File size does not match expected size")
    for value in expected:
        if actual[value.algorithm] != value:
            raise ModMeshError(
                ErrorCode.INTEGRITY, f"File {value.algorithm.value} verification failed"
            )
    return Artifact(actual[HashAlgorithm.SHA256], size, expected)
