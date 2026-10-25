from __future__ import annotations

import hashlib
import ipaddress
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.hashing import verify_file
from modmesh.core.resolver import ResolvedMod
from modmesh.core.sources import (
    DownloadAccess,
    DownloadMode,
    SearchPage,
    SearchRequest,
    SourceAdapter,
    SourceCapability,
    SourceDescriptor,
    VersionQuery,
)
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.domain.models import ModFile, ModVersion, SourceProject, SourceRef
from modmesh.domain.validation import require_id
from modmesh.storage.installed import decode_mod, encode_mod
from modmesh.storage.metadata import MetadataStore


def public_url(value: str) -> str:
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.port not in {None, 443}
            or any(char.isspace() for char in value)
        ):
            raise ValueError("Unsupported URL")
        host = url.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or "." not in host:
            raise ValueError("Local hostname")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("Private address")
        return value
    except ValueError as exc:
        raise ModMeshError(
            ErrorCode.VALIDATION, "Custom files require a public HTTPS URL without secrets"
        ) from exc


class CustomSource(SourceAdapter):
    version_filters = True

    def __init__(self, database: Path) -> None:
        self.database = database

    def records(self) -> tuple[dict[str, object], ...]:
        with MetadataStore(self.database) as store:
            return tuple(value for _, value in store.list("custom.files"))

    @property
    def descriptor(self) -> SourceDescriptor:
        games = {"palworld", "minecraft"}
        for record in self.records():
            games.add(decode_mod(record["mod"]).project.game_id)
        return SourceDescriptor(
            "custom",
            "Custom files",
            frozenset(games),
            frozenset(
                {
                    SourceCapability.SEARCH,
                    SourceCapability.PROJECT,
                    SourceCapability.VERSIONS,
                    SourceCapability.FILES,
                    SourceCapability.HASHES,
                    SourceCapability.DEPENDENCIES,
                    SourceCapability.DOWNLOAD_DIRECT,
                }
            ),
        )

    def register(
        self,
        game: str,
        name: str,
        location: str,
        *,
        filename: str | None = None,
        sha256: str | None = None,
        game_version: str | None = None,
        loader: str | None = None,
        dependencies_known: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> str:
        require_id(game, "custom game")
        path, url = None, None
        if location.startswith("https://"):
            url = public_url(location)
            if not filename or not sha256:
                raise ModMeshError(
                    ErrorCode.VALIDATION, "Remote files require a filename and SHA-256"
                )
            checksum = FileHash(HashAlgorithm.SHA256, sha256)
            size = None
        else:
            path = Path(location).expanduser()
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024**3:
                raise ModMeshError(ErrorCode.VALIDATION, "Select a regular local file below 8 GiB")
            path = path.resolve()
            artifact = verify_file(path, cancelled=cancelled)
            checksum, size, filename = artifact.sha256, artifact.size, path.name
        if (
            not filename
            or len(filename) > 200
            or "/" in filename
            or "\\" in filename
            or filename in {".", ".."}
            or ":" in filename
        ):
            raise ModMeshError(ErrorCode.VALIDATION, "Custom filename must be a portable basename")
        identity = hashlib.sha256(f"{game}:{checksum.value}".encode()).hexdigest()
        reference = SourceRef("custom", identity)
        file = ModFile(reference, checksum.value, filename, size, hashes=(checksum,), primary=True)
        version = ModVersion(
            reference,
            checksum.value,
            checksum.value[:12],
            frozenset({game_version}) if game_version else frozenset(),
            frozenset({loader}) if loader else frozenset(),
            (file,),
            dependencies_known=dependencies_known,
        )
        project = SourceProject(reference, game, name, summary="User registered file")
        with MetadataStore(self.database) as store:
            store.put(
                "custom.files",
                identity,
                {
                    "mod": encode_mod(ResolvedMod(project, version, file, True)),
                    "path": str(path) if path else None,
                    "url": url,
                },
            )
        return identity

    def remove(self, identity: str) -> None:
        with MetadataStore(self.database) as store, store.transaction(write=True) as transaction:
            transaction.delete("custom.files", identity)

    def lookup(self, identity: str) -> dict[str, object]:
        with MetadataStore(self.database) as store:
            record = store.get("custom.files", identity)
        if record is None:
            raise ModMeshError(ErrorCode.NOT_FOUND, "Custom file registration is unavailable")
        return record

    async def project(self, game_id: str, project_id: str) -> SourceProject:
        mod = decode_mod(self.lookup(project_id)["mod"])
        if mod.project.game_id != game_id:
            raise ModMeshError(ErrorCode.NOT_FOUND, "Custom file is for another game")
        return mod.project

    async def versions(
        self, game_id: str, project_id: str, *, query: VersionQuery | None = None
    ) -> tuple[ModVersion, ...]:
        await self.project(game_id, project_id)
        version = decode_mod(self.lookup(project_id)["mod"]).version
        if query and (
            (
                query.game_version
                and version.game_versions
                and query.game_version not in version.game_versions
            )
            or (query.loader and version.loaders and query.loader not in version.loaders)
        ):
            return ()
        return (version,)

    async def search(self, request: SearchRequest) -> SearchPage:
        projects = []
        for record in self.records():
            mod = decode_mod(record["mod"])
            if (
                mod.project.game_id == request.game_id
                and request.query.casefold() in mod.project.name.casefold()
            ) and await self.versions(
                request.game_id,
                mod.project.reference.project_id,
                query=VersionQuery(request.game_version, request.loader),
            ):
                projects.append(mod.project)
        projects.sort(key=lambda item: (item.name.casefold(), item.reference.project_id))
        end = request.offset + request.limit
        return SearchPage(
            tuple(projects[request.offset : end]),
            len(projects),
            end if end < len(projects) else None,
        )

    def local_file(self, file: ModFile) -> Path | None:
        record = self.lookup(file.project.project_id)
        if file != decode_mod(record["mod"]).file:
            raise ModMeshError(ErrorCode.INTEGRITY, "Custom file identity changed")
        return Path(str(record["path"])) if record.get("path") else None

    def download_hosts(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    urlsplit(public_url(str(record["url"]))).hostname or ""
                    for record in self.records()
                    if record.get("url")
                }
            )
        )

    async def resolve_download(self, game_id: str, file: ModFile) -> DownloadAccess:
        await self.project(game_id, file.project.project_id)
        self.local_file(file)
        value = self.lookup(file.project.project_id).get("url")
        return (
            DownloadAccess(DownloadMode.DIRECT, public_url(str(value)))
            if value
            else DownloadAccess(DownloadMode.UNAVAILABLE, reason="Use the verified local file")
        )
