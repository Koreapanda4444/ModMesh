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
    SearchScope,
    SourceAdapter,
    SourceCapability,
    SourceDescriptor,
    SourceStatus,
    VersionQuery,
)
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.domain.models import ModFile, ModVersion, SourceProject, SourceRef

_GAMES = {"palworld": "palworld", "minecraft": "minecraft"}
_PUBLIC = frozenset({SourceCapability.SEARCH, SourceCapability.EXTERNAL_HANDOFF})
_AUTHENTICATED = frozenset(
    {
        SourceCapability.PROJECT,
        SourceCapability.VERSIONS,
        SourceCapability.FILES,
        SourceCapability.HASHES,
    }
)


def _numeric(value: str) -> str:
    if not re.fullmatch(r"[0-9]{1,20}", value):
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid Nexus native ID")
    return str(int(value))


def _domain(game_id: str) -> str:
    try:
        return _GAMES[game_id]
    except KeyError as exc:
        raise ModMeshError(ErrorCode.UNSUPPORTED, "Nexus game mapping is unavailable") from exc


def _mod_id(game_id: str, project_id: str) -> str:
    if ":" in project_id:
        domain, project_id = project_id.split(":", 1)
        if domain != _domain(game_id):
            raise ModMeshError(ErrorCode.VALIDATION, "Nexus project belongs to another game")
    return _numeric(project_id)


def _reference(game_id: str, mod_id: str) -> SourceRef:
    return SourceRef("nexus", f"{_domain(game_id)}:{_numeric(mod_id)}")


def _page_url(game_id: str, mod_id: str) -> str:
    return f"https://www.nexusmods.com/{_domain(game_id)}/mods/{_numeric(mod_id)}"


class NexusSource(SourceAdapter):
    descriptor = SourceDescriptor(
        "nexus",
        "Nexus Mods",
        frozenset(_GAMES),
        _PUBLIC | _AUTHENTICATED | {SourceCapability.DOWNLOAD_DIRECT},
    )

    def __init__(
        self,
        api_key: str | None = None,
        *,
        legacy_client: ApiClient | None = None,
        public_client: ApiClient | None = None,
    ) -> None:
        self._has_key = bool(api_key)
        self._premium: bool | None = None
        self._legacy = legacy_client or ApiClient(
            "https://api.nexusmods.com/v1/", headers={"apikey": api_key} if api_key else {}
        )
        self._public = public_client or ApiClient("https://api.nexusmods.com/v3/")
        self._owns_legacy = legacy_client is None
        self._owns_public = public_client is None

    def status(self, game_id: str) -> SourceStatus:
        base = super().status(game_id)
        if not base.available:
            return base
        capabilities = _PUBLIC | (_AUTHENTICATED if self._has_key else frozenset())
        if self._premium:
            capabilities |= {SourceCapability.DOWNLOAD_DIRECT}
        reason = "Trending feed only; full catalog search uses the Nexus website"
        if not self._has_key:
            reason += "; metadata/file lookup requires a Nexus API key"
        return SourceStatus(True, capabilities, reason)

    async def search(self, request: SearchRequest) -> SearchPage:
        self.require(request.game_id, SourceCapability.SEARCH)
        domain = _domain(request.game_id)
        website = f"https://www.nexusmods.com/{domain}/mods/"
        warnings = ("Nexus REST exposes a limited trending feed, not full catalog text search.",)
        if request.game_version or request.loader:
            raise ModMeshError(
                ErrorCode.UNSUPPORTED,
                "Nexus feed cannot verify version/loader filters; use the website",
            )
        payload = object_data(await self._public.get_json(f"games/{domain}/trending-mods"))
        feed = object_data(payload.get("data"))
        projects: list[SourceProject] = []
        for item in array_data(feed.get("mods")):
            data = object_data(item)
            name = text(data, "name")
            url = text(data, "mod_page_url")
            parsed = urlsplit(url)
            match = re.fullmatch(rf"/(?:games/)?{re.escape(domain)}/mods/([0-9]+)/?", parsed.path)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in {"www.nexusmods.com", "nexusmods.com"}
                or not match
            ):
                raise ModMeshError(ErrorCode.NETWORK, "Unexpected Nexus project URL")
            summary = optional_text(data, "summary") or ""
            if request.query.casefold() not in (name + " " + summary).casefold():
                continue
            author = optional_text(data, "author")
            projects.append(
                SourceProject(
                    _reference(request.game_id, match.group(1)),
                    request.game_id,
                    name,
                    summary,
                    (author,) if author else (),
                    url,
                )
            )
        return SearchPage(
            tuple(projects[request.offset : request.offset + request.limit]),
            total=None,
            warnings=warnings,
            handoff_url=website,
            scope=SearchScope.TRENDING,
        )

    def catalog_url(self, game_id: str) -> str | None:
        return f"https://www.nexusmods.com/{_GAMES[game_id]}/mods/" if game_id in _GAMES else None

    async def project(self, game_id: str, project_id: str) -> SourceProject:
        self.require(game_id, SourceCapability.PROJECT)
        mod_id = _mod_id(game_id, project_id)
        data = object_data(
            await self._legacy.get_json(f"games/{_domain(game_id)}/mods/{mod_id}.json")
        )
        if str(integer(data, "mod_id")) != mod_id:
            raise ModMeshError(ErrorCode.NETWORK, "Nexus project response ID mismatch")
        author = optional_text(data, "author")
        return SourceProject(
            _reference(game_id, mod_id),
            game_id,
            text(data, "name"),
            optional_text(data, "summary") or "",
            (author,) if author else (),
            _page_url(game_id, mod_id),
        )

    async def versions(
        self, game_id: str, project_id: str, *, query: VersionQuery | None = None
    ) -> tuple[ModVersion, ...]:
        self.require(game_id, SourceCapability.VERSIONS)
        if query is not None and (query.game_version or query.loader):
            raise ModMeshError(
                ErrorCode.UNSUPPORTED, "Nexus metadata cannot verify version/loader filters"
            )
        mod_id = _mod_id(game_id, project_id)
        reference = _reference(game_id, mod_id)
        payload = object_data(
            await self._legacy.get_json(f"games/{_domain(game_id)}/mods/{mod_id}/files.json")
        )
        versions: list[ModVersion] = []
        for item in array_data(payload.get("files")):
            data = object_data(item)
            file_id = str(integer(data, "file_id"))
            size = integer(data, "size_in_bytes") if data.get("size_in_bytes") is not None else None
            md5 = optional_text(data, "md5")
            hashes = (FileHash(HashAlgorithm.MD5, md5),) if md5 else ()
            file = ModFile(reference, file_id, text(data, "file_name"), size, hashes=hashes)
            versions.append(
                ModVersion(
                    reference,
                    file_id,
                    optional_text(data, "version") or None,
                    files=(file,),
                    dependencies_known=False,
                )
            )
        return tuple(versions)

    async def resolve_download(self, game_id: str, file: ModFile) -> DownloadAccess:
        if file.project.source_id != "nexus":
            raise ModMeshError(ErrorCode.VALIDATION, "File is not from Nexus")
        mod_id = _mod_id(game_id, file.project.project_id)
        file_id = _numeric(file.file_id)
        website = _page_url(game_id, mod_id) + f"?tab=files&file_id={file_id}"
        external = DownloadAccess(
            DownloadMode.EXTERNAL,
            website,
            "Use the official Nexus download flow; direct API access requires permission",
        )
        if not self._has_key:
            return external
        try:
            user = object_data(await self._legacy.get_json("users/validate.json"))
            self._premium = user.get("is_premium") is True
            if not self._premium:
                return external
            links = array_data(
                await self._legacy.get_json(
                    f"games/{_domain(game_id)}/mods/{mod_id}/files/{file_id}/download_link.json"
                )
            )
            if not links:
                return external
            uri = text(object_data(links[0]), "URI")
            if urlsplit(uri).scheme != "https":
                raise ModMeshError(ErrorCode.NETWORK, "Nexus supplied a non-HTTPS download URL")
            return DownloadAccess(DownloadMode.DIRECT, uri)
        except ModMeshError as exc:
            if exc.code == ErrorCode.AUTHENTICATION:
                return external
            raise

    async def aclose(self) -> None:
        if self._owns_legacy:
            await self._legacy.aclose()
        if self._owns_public:
            await self._public.aclose()
