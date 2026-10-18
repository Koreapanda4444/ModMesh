import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from modmesh.apps.runtime import (
    DOWNLOAD_DOMAINS,
    create_game_registry,
    source_priorities,
    source_registry,
)
from modmesh.core.artifact_matching import ArtifactCandidate
from modmesh.core.cache import ArtifactCache
from modmesh.core.config import ClientSettings, load_client_settings
from modmesh.core.diagnostics import ErrorCode, ModMeshError, configure_logging, redact
from modmesh.core.discovery import DiscoveryEngine, current_platform
from modmesh.core.downloads import DownloadManager, DownloadPolicy, DownloadProgress, DownloadResult
from modmesh.core.games import DiscoveryContext
from modmesh.core.identity import IdentityRegistry
from modmesh.core.matching import ProjectMatcher
from modmesh.core.search import SearchState, UnifiedSearch
from modmesh.core.sources import SearchRequest, SearchSort, VersionQuery
from modmesh.domain.models import ModVersion
from modmesh.storage.metadata import MetadataStore


def main() -> int:
    parser = argparse.ArgumentParser(description="ModMesh core commands")
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover", help="Read-only game discovery")
    discover.add_argument("--game", help="Registered game ID")
    discover.add_argument(
        "--root", action="append", type=Path, default=[], help="Explicit discovery root"
    )
    discover.add_argument("--path", type=Path, help="Validate and remember a manual game path")
    discover.add_argument(
        "--mods", action="store_true", help="Read installed mod metadata without changing files"
    )
    search = commands.add_parser("search", help="Search configured sources independently")
    search.add_argument("query")
    search.add_argument("--game", required=True)
    search.add_argument("--source", action="append", help="Limit to selected source IDs")
    search.add_argument("--game-version")
    search.add_argument("--loader")
    search.add_argument(
        "--offset", type=int, default=0, help="Offset within each source, not a global result index"
    )
    search.add_argument("--limit", type=int, default=20, help="Requested page size per source")
    search.add_argument(
        "--sort", choices=[value.value for value in SearchSort], default=SearchSort.RELEVANCE.value
    )
    versions = commands.add_parser("versions", help="List exact source version and file IDs")
    download = commands.add_parser(
        "download", help="Download one pinned source file without installing it"
    )
    for command in (versions, download):
        command.add_argument("--game", required=True)
        command.add_argument("--source", required=True)
        command.add_argument("--project", required=True, help="Exact project ID returned by search")
        command.add_argument("--game-version")
        command.add_argument("--loader")
    versions.add_argument("--limit", type=int, default=20)
    download.add_argument("--version", required=True, help="Exact source version ID")
    download.add_argument("--file", help="Exact file ID; required if no unique primary file exists")
    download.add_argument(
        "--output",
        type=Path,
        help="Export a separate copy; otherwise return the verified cache path",
    )
    download.add_argument("--progress", action="store_true", help="Emit progress JSON to stderr")
    cache_command = commands.add_parser("cache", help="Inspect or safely clean owned cache objects")
    cache_command.add_argument("action", choices=("info", "verify", "prune", "clear"))
    args = parser.parse_args()
    if args.command == "discover" and args.path is not None and args.game is None:
        parser.error("--path requires --game")
    try:
        settings = load_client_settings(args.config)
        configure_logging(level=settings.log_level)
        if args.command == "cache":
            with ArtifactCache(
                settings.paths.cache_dir, max_bytes=settings.cache_max_bytes
            ) as cache:
                if args.action == "verify":
                    payload = cache.verify()
                else:
                    info = (
                        cache.info()
                        if args.action == "info"
                        else cache.prune(clear=args.action == "clear")
                    )
                    payload = asdict(info)
                print(json.dumps(payload, ensure_ascii=False))
            return 0
        if args.command == "search":
            return asyncio.run(_search(args, settings))
        if args.command in {"versions", "download"}:
            return asyncio.run(_source_files(args, settings))
        context = DiscoveryContext(current_platform(), tuple(args.root), os.environ)
        with MetadataStore(settings.paths.database) as store:
            registry = create_game_registry()
            engine = DiscoveryEngine(registry, store)
            if args.path is not None:
                engine.register_manual(args.game, args.path, context)
            result = engine.discover(context, game_id=args.game)
            payload = asdict(result)
            if args.mods:
                payload["mods"] = [
                    asdict(mod)
                    for record in result.records
                    for mod in registry.get(record.installation.game_id).scan_mods(
                        record.installation
                    )
                ]
            print(json.dumps(payload, ensure_ascii=False, default=str))
        return 0
    except ModMeshError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


async def _search(args: argparse.Namespace, settings: ClientSettings) -> int:
    request = SearchRequest(
        args.game,
        args.query,
        args.game_version,
        args.loader,
        args.offset,
        args.limit,
        SearchSort(args.sort),
    )
    async with source_registry() as registry:
        result = await UnifiedSearch(
            registry, priorities=source_priorities(settings.source_priorities)
        ).search(request, source_ids=args.source)
        payload = asdict(result)
        with MetadataStore(settings.paths.database) as store:
            payload["matching"] = asdict(
                ProjectMatcher(IdentityRegistry(store)).match(result.projects)
            )
        print(json.dumps(payload, ensure_ascii=False, default=str))
        return 0 if any(source.state == SearchState.SUCCESS for source in result.sources) else 1


def _version_payload(version: ModVersion) -> dict[str, object]:
    payload = asdict(version)
    for file in payload["files"]:
        file.pop("download_url", None)
    return payload


def _progress(event: DownloadProgress) -> None:
    print(json.dumps(redact(asdict(event)), ensure_ascii=False), file=sys.stderr)


async def _source_files(args: argparse.Namespace, settings: ClientSettings) -> int:
    async with source_registry() as registry:
        source = registry.get(args.source)
        project = await source.project(args.game, args.project)
        query = (
            VersionQuery(args.game_version, args.loader)
            if args.game_version or args.loader
            else None
        )
        versions = await source.versions(args.game, project.reference.project_id, query=query)
        if args.command == "versions":
            if not 1 <= args.limit <= 1000:
                raise ModMeshError(
                    ErrorCode.VALIDATION, "Version output limit must be between 1 and 1000"
                )
            print(
                json.dumps(
                    {
                        "project": asdict(project),
                        "total": len(versions),
                        "versions": [
                            _version_payload(version) for version in versions[: args.limit]
                        ],
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
            return 0
        version = next(
            (version for version in versions if version.version_id == args.version), None
        )
        if version is None:
            raise ModMeshError(ErrorCode.NOT_FOUND, "Requested source version is unavailable")
        files = (
            tuple(file for file in version.files if file.file_id == args.file)
            if args.file
            else version.files
        )
        if not files:
            raise ModMeshError(ErrorCode.NOT_FOUND, "Requested source file is unavailable")
        if len(files) > 1:
            primary = tuple(file for file in files if file.primary)
            if len(primary) != 1:
                raise ModMeshError(ErrorCode.VALIDATION, "Choose an exact --file ID")
            files = primary
        requested = ArtifactCandidate(project, version, files[0], args.game_version, args.loader)
        with ArtifactCache(settings.paths.cache_dir, max_bytes=settings.cache_max_bytes) as cache:
            async with DownloadManager(
                DOWNLOAD_DOMAINS,
                policy=DownloadPolicy(max_bytes=min(8 * 1024**3, settings.cache_max_bytes)),
            ) as manager:

                async def fetch(destination: Path) -> DownloadResult:
                    return await manager.submit_candidate(
                        requested,
                        (),
                        registry,
                        source_priorities(settings.source_priorities),
                        destination,
                        progress=_progress if args.progress else None,
                    ).wait()

                entry = await cache.obtain(files[0], fetch)
                output = await cache.export_async(entry, args.output) if args.output else entry.path
                payload = asdict(entry)
                payload["path"] = output
                payload["source_verification"] = entry.artifact.source_verification
        print(json.dumps(payload, ensure_ascii=False, default=str))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
