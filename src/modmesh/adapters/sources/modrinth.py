import json
import re
from urllib.parse import quote, urlsplit

from modmesh.adapters.jsondata import array_data, integer, object_data, optional_text, text
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.network import ApiClient
from modmesh.core.sources import (
    DownloadAccess,
    DownloadMode,
    SearchPage,
    SearchRequest,
    SearchSort,
    SourceAdapter,
    SourceCapability,
    SourceDescriptor,
    VersionQuery,
)
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.domain.dependencies import Dependency, DependencyKind, DependencyTarget
from modmesh.domain.models import ModFile, ModVersion, ReleaseChannel, SourceProject, SourceRef


def _native(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid Modrinth native ID or slug")
    return quote(value, safe="")


def _strings(value: object) -> frozenset[str]:
    values = array_data(value)
    if any(not isinstance(item, str) for item in values):
        raise ModMeshError(ErrorCode.NETWORK, "Modrinth returned a malformed string list")
    return frozenset(str(item) for item in values)


def _filter_value(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._+-]{1,128}", value):
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid Modrinth version/loader filter")
    return value


def _version(payload: object) -> ModVersion:
    data = object_data(payload)
    reference = SourceRef("modrinth", text(data, "project_id"))
    files: list[ModFile] = []
    for item in array_data(data.get("files")):
        file = object_data(item)
        raw_hashes = object_data(file.get("hashes"))
        hashes = tuple(
            FileHash(algorithm, text(raw_hashes, algorithm.value))
            for algorithm in (HashAlgorithm.SHA512, HashAlgorithm.SHA1)
            if raw_hashes.get(algorithm.value)
        )
        if not hashes:
            raise ModMeshError(ErrorCode.NETWORK, "Modrinth file has no content identifier")
        identity = hashes[0]
        url = text(file, "url")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "cdn.modrinth.com":
            raise ModMeshError(ErrorCode.NETWORK, "Unrecognized Modrinth download origin")
        files.append(
            ModFile(
                reference,
                f"{identity.algorithm.value}:{identity.value}",
                text(file, "filename"),
                integer(file, "size"),
                url,
                hashes,
                file.get("primary") is True,
            )
        )
    dependencies: list[Dependency] = []
    known = "dependencies" in data and isinstance(data["dependencies"], list)
    for item in array_data(data.get("dependencies", [])):
        dependency = object_data(item)
        try:
            kind = DependencyKind(text(dependency, "dependency_type"))
        except ValueError as exc:
            raise ModMeshError(
                ErrorCode.NETWORK, "Unknown Modrinth dependency relationship"
            ) from exc
        dependencies.append(
            Dependency(
                kind,
                DependencyTarget(
                    "modrinth",
                    optional_text(dependency, "project_id"),
                    optional_text(dependency, "version_id"),
                    filename=optional_text(dependency, "file_name"),
                ),
            )
        )
    try:
        channel = ReleaseChannel(text(data, "version_type"))
    except ValueError as exc:
        raise ModMeshError(ErrorCode.NETWORK, "Unknown Modrinth release channel") from exc
    return ModVersion(
        reference,
        text(data, "id"),
        text(data, "version_number"),
        _strings(data.get("game_versions")),
        _strings(data.get("loaders")),
        tuple(files),
        channel,
        tuple(dependencies),
        known,
    )


class ModrinthSource(SourceAdapter):
    version_filters = True

    descriptor = SourceDescriptor(
        "modrinth",
        "Modrinth",
        frozenset({"minecraft"}),
        frozenset(SourceCapability) - {SourceCapability.EXTERNAL_HANDOFF},
    )

    def __init__(self, client: ApiClient | None = None) -> None:
        self._client = client or ApiClient("https://api.modrinth.com/v2/")
        self._owns_client = client is None
        self._verified_projects: set[str] = set()

    async def search(self, request: SearchRequest) -> SearchPage:
        self.require(request.game_id, SourceCapability.SEARCH)
        facets = [["project_type:mod"]]
        if request.game_version:
            facets.append([f"versions:{_filter_value(request.game_version)}"])
        if request.loader:
            facets.append([f"categories:{_filter_value(request.loader)}"])
        index = {
            SearchSort.RELEVANCE: "relevance",
            SearchSort.DOWNLOADS: "downloads",
            SearchSort.UPDATED: "updated",
        }[request.sort]
        payload = object_data(
            await self._client.get_json(
                "search",
                params={
                    "query": request.query,
                    "facets": json.dumps(facets),
                    "offset": request.offset,
                    "limit": request.limit,
                    "index": index,
                },
            )
        )
        hits = array_data(payload.get("hits"))
        total = integer(payload, "total_hits")
        projects: list[SourceProject] = []
        for item in hits:
            data = object_data(item)
            if data.get("project_type") != "mod":
                continue
            project_id = text(data, "project_id")
            self._remember(project_id)
            author = optional_text(data, "author")
            projects.append(
                SourceProject(
                    SourceRef("modrinth", project_id),
                    request.game_id,
                    text(data, "title"),
                    text(data, "description", ""),
                    (author,) if author else (),
                    f"https://modrinth.com/mod/{_native(project_id)}",
                )
            )
        next_offset = request.offset + len(hits)
        return SearchPage(
            tuple(projects), total, next_offset if hits and next_offset < total else None
        )

    async def project(self, game_id: str, project_id: str) -> SourceProject:
        self.require(game_id, SourceCapability.PROJECT)
        data = object_data(await self._client.get_json(f"project/{_native(project_id)}"))
        if data.get("project_type") != "mod":
            raise ModMeshError(ErrorCode.UNSUPPORTED, "Selected Modrinth project is not a mod")
        stable_id = text(data, "id")
        self._remember(stable_id)
        links = tuple(
            value
            for name in ("source_url", "issues_url", "wiki_url")
            if (value := optional_text(data, name))
        )
        return SourceProject(
            SourceRef("modrinth", stable_id),
            game_id,
            text(data, "title"),
            text(data, "description", ""),
            web_url=f"https://modrinth.com/mod/{_native(stable_id)}",
            description=text(data, "body", ""),
            links=links,
        )

    async def versions(
        self, game_id: str, project_id: str, *, query: VersionQuery | None = None
    ) -> tuple[ModVersion, ...]:
        self.require(game_id, SourceCapability.VERSIONS)
        if project_id not in self._verified_projects:
            project_id = (await self.project(game_id, project_id)).reference.project_id
        params: dict[str, str] = {}
        if query is not None:
            if query.game_version:
                params["game_versions"] = json.dumps([_filter_value(query.game_version)])
            if query.loader:
                params["loaders"] = json.dumps([_filter_value(query.loader)])
        versions = tuple(
            _version(value)
            for value in array_data(
                await self._client.get_json(f"project/{_native(project_id)}/version", params=params)
            )
        )
        if any(version.project != SourceRef("modrinth", project_id) for version in versions):
            raise ModMeshError(ErrorCode.NETWORK, "Modrinth returned versions of another project")
        return versions

    async def version(self, game_id: str, version_id: str) -> ModVersion:
        self.require(game_id, SourceCapability.VERSIONS)
        version = _version(await self._client.get_json(f"version/{_native(version_id)}"))
        if version.version_id != version_id:
            raise ModMeshError(ErrorCode.INTEGRITY, "Modrinth version ID mismatch")
        if version.project.project_id not in self._verified_projects:
            await self.project(game_id, version.project.project_id)
        return version

    async def dependencies(
        self, game_id: str, project_id: str, version_id: str
    ) -> tuple[Dependency, ...]:
        self.require(game_id, SourceCapability.DEPENDENCIES)
        if project_id not in self._verified_projects:
            project_id = (await self.project(game_id, project_id)).reference.project_id
        version = _version(await self._client.get_json(f"version/{_native(version_id)}"))
        if version.project.project_id != project_id:
            raise ModMeshError(ErrorCode.VALIDATION, "Modrinth version belongs to another project")
        if not version.dependencies_known:
            raise ModMeshError(ErrorCode.UNSUPPORTED, "Modrinth dependency metadata is unavailable")
        return version.dependencies

    async def resolve_download(self, game_id: str, file: ModFile) -> DownloadAccess:
        self.require(game_id, SourceCapability.DOWNLOAD_DIRECT)
        if file.project.source_id != "modrinth" or not file.hashes:
            raise ModMeshError(ErrorCode.VALIDATION, "File is not an identified Modrinth artifact")
        supported = tuple(
            value
            for value in file.hashes
            if value.algorithm in {HashAlgorithm.SHA512, HashAlgorithm.SHA1}
        )
        if not supported:
            raise ModMeshError(
                ErrorCode.VALIDATION, "Modrinth requires a SHA-512 or SHA-1 file identifier"
            )
        if file.project.project_id not in self._verified_projects:
            await self.project(game_id, file.project.project_id)
        identity = next(
            (value for value in supported if value.algorithm == HashAlgorithm.SHA512), supported[0]
        )
        if file.file_id != f"{identity.algorithm.value}:{identity.value}":
            raise ModMeshError(
                ErrorCode.VALIDATION, "Modrinth file ID does not match its content identifier"
            )
        version = _version(
            await self._client.get_json(
                f"version_file/{identity.value}", params={"algorithm": identity.algorithm.value}
            )
        )
        if version.project != file.project:
            raise ModMeshError(ErrorCode.INTEGRITY, "Modrinth file belongs to another project")
        for candidate in version.files:
            if identity in candidate.hashes:
                hashes = {value.algorithm: value for value in candidate.hashes}
                if any(
                    value.algorithm in hashes and hashes[value.algorithm] != value
                    for value in file.hashes
                ):
                    raise ModMeshError(ErrorCode.INTEGRITY, "Modrinth file hash metadata conflicts")
                return DownloadAccess(DownloadMode.DIRECT, candidate.download_url)
        raise ModMeshError(ErrorCode.NOT_FOUND, "Modrinth content identifier was not found")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _remember(self, project_id: str) -> None:
        if len(self._verified_projects) >= 512:
            self._verified_projects.clear()
        self._verified_projects.add(project_id)

    def catalog_url(self, game_id: str) -> str | None:
        return "https://modrinth.com/mods" if game_id == "minecraft" else None
