from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Self

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.downloads import DownloadResult, _blocking, publish_file
from modmesh.core.hashing import hash_file, verify_file
from modmesh.domain.artifacts import Artifact, FileHash, HashAlgorithm
from modmesh.domain.models import ModFile
from modmesh.storage.metadata import MetadataStore

_OWNER = b"ModMesh verified cache v1\n"
_OBJECTS = "cache.objects"
_ALIASES = "cache.aliases"
_HASHES = "cache.hashes"


@dataclass(frozen=True)
class CacheEntry:
    path: Path
    artifact: Artifact
    cache_hit: bool
    source_id: str
    file_id: str
    download_source_id: str


@dataclass(frozen=True)
class CacheInfo:
    objects: int
    bytes: int
    limit: int


@dataclass(frozen=True)
class _Blob:
    sha: str
    size: int
    device: int
    inode: int
    used: float
    source: str

    @classmethod
    def read(cls, sha: str, record: dict[str, object]) -> _Blob:
        FileHash(HashAlgorithm.SHA256, sha)
        size, device, inode, used, source = (
            record.get(key) for key in ("size", "device", "inode", "used", "source")
        )
        if (
            not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in (size, device, inode)
            )
            or not isinstance(size, int)
            or not isinstance(device, int)
            or not isinstance(inode, int)
            or size < 0
            or inode < 1
            or not isinstance(used, (int, float))
            or not math.isfinite(used)
            or not isinstance(source, str)
        ):
            raise ModMeshError(ErrorCode.STORAGE, "Invalid cache ownership record")
        return cls(sha, size, device, inode, float(used), source)


def _indirect(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _alias(file: ModFile) -> str:
    value = json.dumps([file.project.source_id, file.project.project_id, file.file_id])
    return hashlib.sha256(value.encode()).hexdigest()


class _Lease:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.descriptor: int | None = None

    def acquire(self) -> bool:
        if _indirect(self.path):
            raise ModMeshError(ErrorCode.STORAGE, "Cache lock cannot be an indirect path")
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            try:
                descriptor = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                descriptor = os.open(self.path, flags)
            self.descriptor = descriptor
            identity = os.fstat(descriptor)
            if identity.st_size == 0:
                os.write(descriptor, b"\0")
                identity = os.fstat(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if (
                not stat.S_ISREG(identity.st_mode)
                or identity.st_size != 1
                or os.read(descriptor, 1) != b"\0"
            ):
                raise ModMeshError(ErrorCode.STORAGE, "Invalid cache lock file")
            os.lseek(descriptor, 0, os.SEEK_SET)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, PermissionError) as exc:
            opened = self.descriptor is not None
            self.close(unlock=False)
            if not opened:
                raise ModMeshError(ErrorCode.STORAGE, "Cannot open cache lock") from exc
            return False
        except OSError as exc:
            self.close(unlock=False)
            raise ModMeshError(ErrorCode.STORAGE, "Cannot acquire cache lock") from exc
        except BaseException:
            self.close(unlock=False)
            raise

    def close(self, *, unlock: bool = True) -> None:
        if self.descriptor is None:
            return
        descriptor, self.descriptor = self.descriptor, None
        try:
            if unlock:
                if sys.platform == "win32":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class ArtifactCache:
    def __init__(
        self, cache_dir: Path, *, max_bytes: int = 5 * 1024**3, evict: bool = True
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ModMeshError(ErrorCode.CONFIGURATION, "Cache limit must be a positive integer")
        self.max_bytes = max_bytes
        self.evict = evict
        self.root = cache_dir.expanduser().absolute() / "modmesh-cache-v1"
        self._closed = False
        try:
            self.root.parent.mkdir(parents=True, exist_ok=True)
            with suppress(FileExistsError):
                self.root.mkdir(mode=0o700)
            if _indirect(self.root) or not self.root.is_dir():
                raise ModMeshError(ErrorCode.STORAGE, "Cache namespace must be a real directory")
            marker = self.root / "owner"
            if not marker.exists():
                if any(self.root.iterdir()):
                    raise ModMeshError(
                        ErrorCode.CONFLICT, "Refusing to adopt a nonempty cache namespace"
                    )
                try:
                    with marker.open("xb") as output:
                        output.write(_OWNER)
                except FileExistsError:
                    pass
            self._layout()
            with self._guard():
                for name in ("objects", "locks"):
                    (self.root / name).mkdir(exist_ok=True, mode=0o700)
                self._layout()
                self._store = MetadataStore(self.root / "index.sqlite3")
        except OSError as exc:
            raise ModMeshError(ErrorCode.STORAGE, "Cannot initialize verified cache") from exc

    def _layout(self) -> None:
        if _indirect(self.root) or not self.root.is_dir():
            raise ModMeshError(ErrorCode.STORAGE, "Cache namespace changed")
        marker = self.root / "owner"
        if _indirect(marker) or not marker.is_file():
            raise ModMeshError(ErrorCode.STORAGE, "Cache ownership marker missing")
        with marker.open("rb") as stream:
            if stream.read(len(_OWNER) + 1) != _OWNER:
                raise ModMeshError(ErrorCode.STORAGE, "Unknown cache ownership marker")
        for name in (
            "objects",
            "locks",
            "index.sqlite3",
            "index.sqlite3-wal",
            "index.sqlite3-shm",
            "index.sqlite3-journal",
            "cache.lock",
        ):
            path = self.root / name
            if _indirect(path):
                raise ModMeshError(ErrorCode.STORAGE, "Indirect cache control path blocked")
            if path.exists() and (
                (name in {"objects", "locks"} and not path.is_dir())
                or (name not in {"objects", "locks"} and not path.is_file())
            ):
                raise ModMeshError(ErrorCode.STORAGE, "Invalid cache control path")

    @contextmanager
    def _guard(self) -> Iterator[None]:
        if self._closed:
            raise ModMeshError(ErrorCode.CONFLICT, "Cache is closed")
        self._layout()
        lease = _Lease(self.root / "cache.lock")
        deadline = time.monotonic() + 30
        try:
            while not lease.acquire():
                if time.monotonic() >= deadline:
                    raise ModMeshError(ErrorCode.CONFLICT, "Cache is busy; retry later")
                time.sleep(0.05)
            self._layout()
            yield
        except OSError as exc:
            raise ModMeshError(ErrorCode.STORAGE, "Cache filesystem operation failed") from exc
        finally:
            lease.close()

    def _path(self, sha: str) -> Path:
        return self.root / "objects" / FileHash(HashAlgorithm.SHA256, sha).value

    def _owned(self, blob: _Blob) -> bool:
        path = self._path(blob.sha)
        try:
            value = path.lstat()
        except FileNotFoundError:
            return False
        return (
            not _indirect(path)
            and stat.S_ISREG(value.st_mode)
            and (value.st_dev, value.st_ino) == (blob.device, blob.inode)
        )

    def _forget(self, sha: str) -> None:
        with self._store.transaction(write=True) as transaction:
            transaction.delete(_OBJECTS, sha)
            for namespace in (_ALIASES, _HASHES):
                for key, record in transaction.list(namespace):
                    if record.get("sha") == sha:
                        transaction.delete(namespace, key)

    def _remove(self, blob: _Blob) -> None:
        if not self.evict:
            raise ModMeshError(
                ErrorCode.INTEGRITY, "Durable artifact changed; retained for recovery"
            )
        if self._owned(blob):
            self._path(blob.sha).unlink()
        self._forget(blob.sha)

    def _blobs(self) -> list[_Blob]:
        result = []
        for sha, record in self._store.list(_OBJECTS):
            blob = _Blob.read(sha, record)
            if self._owned(blob):
                result.append(blob)
            else:
                self._forget(sha)
        return result

    def _remember(self, file: ModFile, blob: _Blob, artifact: Artifact) -> None:
        with self._store.transaction(write=True) as transaction:
            record = transaction.get(_OBJECTS, blob.sha)
            assert record is not None
            record["used"] = time.time()
            transaction.put(_OBJECTS, blob.sha, record)
            transaction.put(_ALIASES, _alias(file), {"sha": blob.sha})
            for value in (artifact.sha256, *artifact.verified_hashes):
                if value.strong:
                    transaction.put(
                        _HASHES, f"{value.algorithm.value}:{value.value}", {"sha": blob.sha}
                    )

    def lookup(
        self, file: ModFile, *, cancelled: threading.Event | None = None
    ) -> CacheEntry | None:
        with self._guard():
            records = [self._store.get(_ALIASES, _alias(file))]
            records += [
                self._store.get(_HASHES, f"{value.algorithm.value}:{value.value}")
                for value in file.hashes
                if value.strong
            ]
            for record in records:
                if record is None or not isinstance(sha := record.get("sha"), str):
                    continue
                stored = self._store.get(_OBJECTS, sha)
                if stored is None:
                    continue
                blob = _Blob.read(sha, stored)
                if not self._owned(blob):
                    self._forget(sha)
                    continue
                if self._path(sha).stat().st_size != blob.size:
                    self._remove(blob)
                    continue
                size, hashes = hash_file(
                    self._path(sha),
                    {HashAlgorithm.SHA256, *(value.algorithm for value in file.hashes)},
                    cancelled=cancelled.is_set if cancelled else None,
                )
                actual = {value.algorithm: value for value in hashes}
                if size != blob.size or actual[HashAlgorithm.SHA256].value != sha:
                    self._remove(blob)
                    continue
                if (file.size is not None and size != file.size) or any(
                    actual[value.algorithm] != value for value in file.hashes
                ):
                    continue
                if size > self.max_bytes:
                    raise ModMeshError(ErrorCode.VALIDATION, "Artifact exceeds the cache capacity")
                artifact = Artifact(actual[HashAlgorithm.SHA256], size, file.hashes)
                self._remember(file, blob, artifact)
                self._prune(self.max_bytes, protect=sha)
                return CacheEntry(
                    self._path(sha),
                    artifact,
                    True,
                    file.project.source_id,
                    file.file_id,
                    blob.source,
                )
        return None

    def _prune(
        self, target: int, *, protect: str | None = None, remove_all: bool = False
    ) -> CacheInfo:
        blobs = self._blobs()
        sizes = {blob.sha: self._path(blob.sha).lstat().st_size for blob in blobs}
        size = sum(sizes.values())
        count = len(blobs)
        if not self.evict:
            if size > target or remove_all:
                raise ModMeshError(
                    ErrorCode.CONFLICT, "Durable artifact capacity reached; files retained"
                )
            return CacheInfo(count, size, self.max_bytes)
        for blob in sorted(blobs, key=lambda value: (value.used, value.sha)):
            if size <= target and not remove_all:
                break
            if blob.sha != protect:
                self._remove(blob)
                size -= sizes[blob.sha]
                count -= 1
        return CacheInfo(count, size, self.max_bytes)

    def info(self) -> CacheInfo:
        with self._guard():
            blobs = self._blobs()
            return CacheInfo(
                len(blobs),
                sum(self._path(blob.sha).lstat().st_size for blob in blobs),
                self.max_bytes,
            )

    def verify(self) -> dict[str, object]:
        with self._guard():
            checked = removed = 0
            for blob in self._blobs():
                checked += 1
                try:
                    verify_file(
                        self._path(blob.sha),
                        (FileHash(HashAlgorithm.SHA256, blob.sha),),
                        expected_size=blob.size,
                    )
                except ModMeshError as exc:
                    if exc.code != ErrorCode.INTEGRITY:
                        raise
                    self._remove(blob)
                    removed += 1
            return {"checked": checked, "corrupt_removed": removed}

    def prune(self, *, clear: bool = False) -> CacheInfo:
        with self._guard():
            return self._prune(0 if clear else self.max_bytes, remove_all=clear)

    def _import(
        self, file: ModFile, result: DownloadResult, cancelled: threading.Event
    ) -> CacheEntry:
        with self._guard():
            expected = {
                value.algorithm: value for value in (*file.hashes, *result.artifact.verified_hashes)
            }
            for value in file.hashes:
                if expected[value.algorithm] != value:
                    raise ModMeshError(ErrorCode.INTEGRITY, "Conflicting cache import hashes")
            artifact = verify_file(
                result.path,
                tuple(expected.values()),
                expected_size=file.size,
                cancelled=cancelled.is_set,
            )
            if artifact.sha256 != result.artifact.sha256 or artifact.size != result.artifact.size:
                raise ModMeshError(ErrorCode.INTEGRITY, "Downloaded file changed before caching")
            if artifact.size > self.max_bytes:
                raise ModMeshError(ErrorCode.VALIDATION, "Artifact exceeds the cache capacity")
            sha = artifact.sha256.value
            target = self._path(sha)
            stored = self._store.get(_OBJECTS, sha)
            if stored is not None:
                blob = _Blob.read(sha, stored)
                if self._owned(blob):
                    try:
                        verify_file(
                            target,
                            (artifact.sha256,),
                            expected_size=artifact.size,
                            cancelled=cancelled.is_set,
                        )
                    except ModMeshError as exc:
                        if exc.code != ErrorCode.INTEGRITY:
                            raise
                        self._remove(blob)
                    else:
                        self._remember(file, blob, artifact)
                        self._prune(self.max_bytes, protect=sha)
                        return CacheEntry(
                            target,
                            artifact,
                            False,
                            file.project.source_id,
                            file.file_id,
                            blob.source,
                        )
            if target.exists() or target.is_symlink():
                raise ModMeshError(
                    ErrorCode.CONFLICT, "Unowned cache object blocks publication; preserved"
                )
            descriptor, name = tempfile.mkstemp(prefix=".incoming-", dir=self.root / "objects")
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as output, result.path.open("rb") as source:
                    copied = 0
                    while chunk := source.read(1024 * 1024):
                        if cancelled.is_set():
                            raise ModMeshError(ErrorCode.CANCELLED, "Cache import cancelled")
                        copied += len(chunk)
                        if copied > artifact.size:
                            raise ModMeshError(
                                ErrorCode.INTEGRITY, "Download grew during cache import"
                            )
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                verify_file(
                    temporary,
                    (artifact.sha256,),
                    expected_size=artifact.size,
                    cancelled=cancelled.is_set,
                )
                self._prune(self.max_bytes - artifact.size)
                identity = temporary.stat()

                self._store.put(
                    _OBJECTS,
                    sha,
                    {
                        "size": artifact.size,
                        "device": identity.st_dev,
                        "inode": identity.st_ino,
                        "used": time.time(),
                        "source": result.download_source_id,
                    },
                )
                blob = _Blob.read(sha, self._store.get(_OBJECTS, sha) or {})
                self._remember(file, blob, artifact)
                if cancelled.is_set():
                    self._forget(sha)
                    raise ModMeshError(ErrorCode.CANCELLED, "Cache import cancelled")
                publish_file(temporary, target)
                return CacheEntry(
                    target,
                    artifact,
                    False,
                    file.project.source_id,
                    file.file_id,
                    result.download_source_id,
                )
            finally:
                temporary.unlink(missing_ok=True)

    async def obtain(
        self, file: ModFile, fetch: Callable[[Path], Awaitable[DownloadResult]]
    ) -> CacheEntry:
        if file.size is not None and file.size > self.max_bytes:
            raise ModMeshError(ErrorCode.VALIDATION, "Artifact exceeds the cache capacity")
        self._layout()

        strong = sorted(
            (value for value in file.hashes if value.strong),
            key=lambda value: value.algorithm.value,
        )
        key = f"{strong[0].algorithm.value}:{strong[0].value}" if strong else _alias(file)
        lease = _Lease(self.root / "locks" / hashlib.sha256(key.encode()).hexdigest())
        cancelled = threading.Event()
        try:
            while not lease.acquire():
                await asyncio.sleep(0.05)
            hit = await _blocking(partial(self.lookup, file, cancelled=cancelled), cancelled)
            if hit is not None:
                return hit
            with tempfile.TemporaryDirectory(prefix="modmesh-download-") as directory:
                result = await fetch(Path(directory) / "artifact")
                return await _blocking(partial(self._import, file, result, cancelled), cancelled)
        finally:
            lease.close()

    def export(
        self, entry: CacheEntry, destination: Path, *, cancelled: threading.Event | None = None
    ) -> Path:
        with self._guard():
            sha = entry.artifact.sha256.value
            stored = self._store.get(_OBJECTS, sha)
            if stored is None or not self._owned(_Blob.read(sha, stored)):
                raise ModMeshError(ErrorCode.NOT_FOUND, "Cached artifact is no longer available")
            source = self._path(sha)
            verify_file(
                source,
                (entry.artifact.sha256,),
                expected_size=entry.artifact.size,
                cancelled=cancelled.is_set if cancelled else None,
            )
            destination = destination.expanduser().absolute()
            if destination.exists() or destination.is_symlink():
                raise ModMeshError(
                    ErrorCode.CONFLICT, "Export destination already exists; preserved"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix=".modmesh-", suffix=".part", dir=destination.parent
            )
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                    copied = 0
                    while chunk := input_file.read(1024 * 1024):
                        if cancelled is not None and cancelled.is_set():
                            raise ModMeshError(ErrorCode.CANCELLED, "Cache export cancelled")
                        copied += len(chunk)
                        if copied > entry.artifact.size:
                            raise ModMeshError(
                                ErrorCode.INTEGRITY, "Cached artifact grew during export"
                            )
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                verify_file(
                    temporary,
                    (entry.artifact.sha256,),
                    expected_size=entry.artifact.size,
                    cancelled=cancelled.is_set if cancelled else None,
                )
                publish_file(temporary, destination)
                return destination
            finally:
                temporary.unlink(missing_ok=True)

    async def export_async(self, entry: CacheEntry, destination: Path) -> Path:
        cancelled = threading.Event()
        return await _blocking(
            partial(self.export, entry, destination, cancelled=cancelled), cancelled
        )

    def close(self) -> None:
        self._closed = True
        self._store.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
