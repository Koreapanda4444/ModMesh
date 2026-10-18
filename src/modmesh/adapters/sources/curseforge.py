import re
from urllib.parse import urlsplit

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
    SourceStatus,
    VersionQuery,
)
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.domain.dependencies import Dependency, DependencyKind, DependencyTarget
from modmesh.domain.models import ModFile, ModVersion, ReleaseChannel, SourceProject, SourceRef

_GAME_ID = 432
_MOD_CLASS = 6
_LOADERS = {"forge": 1, "cauldron": 2, "liteloader": 3, "fabric": 4, "quilt": 5, "neoforge": 6}
_RELATIONS = {
    1: DependencyKind.EMBEDDED,
    2: DependencyKind.OPTIONAL,
    3: DependencyKind.REQUIRED,
    4: DependencyKind.TOOL,
    5: DependencyKind.INCOMPATIBLE,
    6: DependencyKind.INCLUDE,
}
_HOME = "https://www.curseforge.com/minecraft"


def _native(value: str) -> str:
    if not re.fullmatch(r"[0-9]{1,20}", value) or int(value) <= 0:
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid CurseForge native ID")
    return str(int(value))


def _website(data: dict[str, object]) -> str:
    links = object_data(data.get("links", {}))
    url = optional_text(links, "websiteUrl") or _HOME
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.curseforge.com", "curseforge.com"}
        or parsed.username
        or parsed.password
    ):
        raise ModMeshError(ErrorCode.NETWORK, "Unrecognized CurseForge project website")
    return url.rstrip("/")


def _check_mod(data: dict[str, object]) -> None:
    if integer(data, "gameId") != _GAME_ID or integer(data, "classId") != _MOD_CLASS:
        raise ModMeshError(ErrorCode.UNSUPPORTED, "CurseForge project is not a Minecraft Java mod")
    if data.get("isAvailable") is False:
        raise ModMeshError(ErrorCode.NOT_FOUND, "CurseForge mod is unavailable")


def _project(data: dict[str, object]) -> SourceProject:
    _check_mod(data)
    links = object_data(data.get("links", {}))
    authors = tuple(
        text(object_data(author), "name") for author in array_data(data.get("authors", []))
    )
    return SourceProject(
        SourceRef("curseforge", str(integer(data, "id"))),
        "minecraft",
        text(data, "name"),
        text(data, "summary", ""),
        authors,
        _website(data),
        links=tuple(
            value
            for name in ("sourceUrl", "issuesUrl", "wikiUrl")
            if (value := optional_text(links, name))
        ),
    )


def _version(data: dict[str, object], project: dict[str, object]) -> ModVersion | None:
    project_id = str(integer(project, "id"))
    if integer(data, "modId") != int(project_id) or integer(data, "gameId") != _GAME_ID:
        raise ModMeshError(ErrorCode.NETWORK, "CurseForge file game/project context mismatch")
    if data.get("isAvailable") is not True or data.get("fileStatus") in {6, 7}:
        return None
    reference = SourceRef("curseforge", project_id)
    file_id = str(integer(data, "id"))
    hashes: list[FileHash] = []
    for item in array_data(data.get("hashes", [])):
        raw = object_data(item)
        algorithm = {1: HashAlgorithm.SHA1, 2: HashAlgorithm.MD5}.get(integer(raw, "algo"))
        if algorithm is not None:
            hashes.append(FileHash(algorithm, text(raw, "value")))
    url = (
        optional_text(data, "downloadUrl") if project.get("allowModDistribution") is True else None
    )
    if url is not None and urlsplit(url).scheme != "https":
        raise ModMeshError(ErrorCode.NETWORK, "CurseForge supplied a non-HTTPS download URL")
    file = ModFile(
        reference,
        file_id,
        text(data, "fileName"),
        integer(data, "fileLength"),
        url,
        tuple(hashes),
        project.get("mainFileId") == int(file_id),
    )
    dependencies: list[Dependency] = []
    known = isinstance(data.get("dependencies"), list)
    for item in array_data(data.get("dependencies", [])):
        raw = object_data(item)
        kind = _RELATIONS.get(integer(raw, "relationType"))
        if kind is None:
            raise ModMeshError(ErrorCode.UNSUPPORTED, "Unknown CurseForge dependency relationship")
        dependencies.append(
            Dependency(kind, DependencyTarget("curseforge", str(integer(raw, "modId"))))
        )
    tags = array_data(data.get("gameVersions", []))
    if any(not isinstance(value, str) for value in tags):
        raise ModMeshError(ErrorCode.NETWORK, "Malformed CurseForge version tags")
    versions = frozenset(str(value) for value in tags if re.match(r"^[0-9]", str(value)))
    loaders = frozenset(
        str(value).casefold() for value in tags if str(value).casefold() in _LOADERS
    )
    channel = {1: ReleaseChannel.RELEASE, 2: ReleaseChannel.BETA, 3: ReleaseChannel.ALPHA}.get(
        integer(data, "releaseType")
    )
    if channel is None:
        raise ModMeshError(ErrorCode.UNSUPPORTED, "Unknown CurseForge release channel")
    return ModVersion(
        reference,
        file_id,
        optional_text(data, "displayName"),
        versions,
        loaders,
        (file,),
        channel,
        tuple(dependencies),
        known,
    )


class CurseForgeSource(SourceAdapter):
    version_filters = True

    descriptor = SourceDescriptor(
        "curseforge", "CurseForge", frozenset({"minecraft"}), frozenset(SourceCapability)
    )

    def __init__(self, api_key: str | None = None, *, client: ApiClient | None = None) -> None:
        self._has_key = bool(api_key)
        self._client = client or ApiClient(
            "https://api.curseforge.com/v1/", headers={"x-api-key": api_key} if api_key else {}
        )
        self._owns_client = client is None

    def status(self, game_id: str) -> SourceStatus:
        base = super().status(game_id)
        if not base.available:
            return base
        return (
            base
            if self._has_key
            else SourceStatus(
                True,
                frozenset({SourceCapability.EXTERNAL_HANDOFF}),
                "An approved CurseForge API key is required",
            )
        )

    async def _mod(self, game_id: str, project_id: str) -> dict[str, object]:
        self.require(game_id, SourceCapability.PROJECT)
        native = _native(project_id)
        data = object_data(object_data(await self._client.get_json(f"mods/{native}")).get("data"))
        _check_mod(data)
        if str(integer(data, "id")) != native:
            raise ModMeshError(ErrorCode.NETWORK, "CurseForge project ID mismatch")
        return data

    async def search(self, request: SearchRequest) -> SearchPage:
        self.require(request.game_id, SourceCapability.SEARCH)
        params: dict[str, str | int] = {
            "gameId": _GAME_ID,
            "classId": _MOD_CLASS,
            "searchFilter": request.query,
            "index": request.offset,
            "pageSize": min(request.limit, 50),
            "sortOrder": "desc",
        }
        if request.sort != SearchSort.RELEVANCE:
            params["sortField"] = {SearchSort.DOWNLOADS: 6, SearchSort.UPDATED: 3}[request.sort]
        if request.game_version:
            params["gameVersion"] = request.game_version
        if request.loader:
            if request.loader.casefold() not in _LOADERS:
                raise ModMeshError(ErrorCode.UNSUPPORTED, "CurseForge loader filter is unsupported")
            params["modLoaderType"] = _LOADERS[request.loader.casefold()]
        payload = object_data(await self._client.get_json("mods/search", params=params))
        values = array_data(payload.get("data"))
        pagination = object_data(payload.get("pagination"))
        total = integer(pagination, "totalCount")
        next_offset = request.offset + len(values)
        return SearchPage(
            tuple(_project(object_data(item)) for item in values),
            total,
            next_offset if values and next_offset < total else None,
            warnings=("CurseForge uses its source-defined default search order.",)
            if request.sort == SearchSort.RELEVANCE
            else (),
        )

    async def project(self, game_id: str, project_id: str) -> SourceProject:
        return _project(await self._mod(game_id, project_id))

    async def versions(
        self, game_id: str, project_id: str, *, query: VersionQuery | None = None
    ) -> tuple[ModVersion, ...]:
        project = await self._mod(game_id, project_id)
        params: dict[str, str | int] = {"pageSize": 50, "index": 0}
        if query is not None:
            if query.game_version:
                params["gameVersion"] = query.game_version
            if query.loader:
                if query.loader.casefold() not in _LOADERS:
                    raise ModMeshError(
                        ErrorCode.UNSUPPORTED, "CurseForge loader filter is unsupported"
                    )
                params["modLoaderType"] = _LOADERS[query.loader.casefold()]
        found: dict[str, ModVersion] = {}
        index = 0
        for _ in range(40):
            params["index"] = index
            payload = object_data(
                await self._client.get_json(f"mods/{_native(project_id)}/files", params=params)
            )
            pagination = object_data(payload.get("pagination"))
            if integer(pagination, "index") != index:
                raise ModMeshError(ErrorCode.NETWORK, "CurseForge file pagination mismatch")
            values = array_data(payload.get("data"))
            for item in values:
                version = _version(object_data(item), project)
                if version is not None and (
                    query is None
                    or (
                        (not query.game_version or query.game_version in version.game_versions)
                        and (not query.loader or query.loader.casefold() in version.loaders)
                    )
                ):
                    found[version.version_id] = version
            index += len(values)
            if index >= integer(pagination, "totalCount"):
                return tuple(found.values())
            if not values:
                raise ModMeshError(ErrorCode.NETWORK, "CurseForge returned an incomplete file page")
        raise ModMeshError(
            ErrorCode.UNSUPPORTED, "Too many CurseForge versions; narrow the version/loader filters"
        )

    async def resolve_download(self, game_id: str, file: ModFile) -> DownloadAccess:
        self.require(game_id, SourceCapability.EXTERNAL_HANDOFF)
        if file.project.source_id != "curseforge":
            raise ModMeshError(ErrorCode.VALIDATION, "File is not from CurseForge")
        if not self._has_key:
            return DownloadAccess(DownloadMode.EXTERNAL, _HOME, "An approved API key is required")
        project_id, file_id = _native(file.project.project_id), _native(file.file_id)
        project = await self._mod(game_id, project_id)
        website = _website(project) + f"/files/{file_id}"
        if project.get("allowModDistribution") is not True:
            return DownloadAccess(
                DownloadMode.EXTERNAL,
                website,
                "Third-party distribution permission is absent or unconfirmed",
            )
        data = object_data(
            object_data(await self._client.get_json(f"mods/{project_id}/files/{file_id}")).get(
                "data"
            )
        )
        version = _version(data, project)
        if version is None or version.version_id != file_id:
            raise ModMeshError(ErrorCode.NOT_FOUND, "CurseForge file is unavailable")
        candidate = version.files[0]
        hashes = {value.algorithm: value for value in candidate.hashes}
        if any(
            value.algorithm in hashes and hashes[value.algorithm] != value for value in file.hashes
        ):
            raise ModMeshError(ErrorCode.INTEGRITY, "CurseForge file hash metadata changed")
        if candidate.download_url is None:
            return DownloadAccess(
                DownloadMode.EXTERNAL, website, "The API did not permit a direct download"
            )
        return DownloadAccess(DownloadMode.DIRECT, candidate.download_url)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def catalog_url(self, game_id: str) -> str | None:
        return _HOME if game_id == "minecraft" else None
