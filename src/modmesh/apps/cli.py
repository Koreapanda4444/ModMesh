import argparse
import asyncio
import json
import os
import sys
import threading
from dataclasses import asdict
from functools import partial
from pathlib import Path

from modmesh.apps.operations import prepare_resolution
from modmesh.apps.runtime import (
    DOWNLOAD_DOMAINS,
    create_game_registry,
    source_priorities,
    source_registry,
)
from modmesh.core.artifact_matching import ArtifactCandidate
from modmesh.core.cache import ArtifactCache
from modmesh.core.config import ClientSettings, load_client_settings
from modmesh.core.conflicts import analyze_dependencies
from modmesh.core.diagnostics import ErrorCode, ModMeshError, configure_logging, redact
from modmesh.core.discovery import DiscoveryEngine, current_platform
from modmesh.core.downloads import (
    DownloadManager,
    DownloadPolicy,
    DownloadProgress,
    DownloadResult,
    _blocking,
)
from modmesh.core.games import DiscoveryContext
from modmesh.core.identity import IdentityRegistry
from modmesh.core.matching import ProjectMatcher
from modmesh.core.removal import RemovalManager
from modmesh.core.resolver import DependencyResolution, DependencyResolver, ResolvedMod
from modmesh.core.search import SearchState, UnifiedSearch
from modmesh.core.sources import SearchRequest, SearchSort, SourceRegistry, VersionQuery
from modmesh.domain.models import GameInstallation, ModVersion, SourceRef
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
    resolve = commands.add_parser("resolve", help="Resolve required dependency versions")
    plan = commands.add_parser("plan", help="Preview verified game file changes")
    plan.add_argument("--path", required=True, type=Path)
    plan.add_argument("--rule", help="Explicit game installation rule ID")
    plan.add_argument("--archive-prefix", default="")
    plan.add_argument("--replace-existing", action="store_true")
    plan.add_argument(
        "--apply", metavar="FINGERPRINT", help="Apply the exact previously reviewed plan"
    )
    for command in (versions, download, resolve, plan):
        command.add_argument("--game", required=True)
        command.add_argument("--source", required=True)
        command.add_argument("--project", required=True, help="Exact project ID returned by search")
        command.add_argument("--game-version")
        command.add_argument("--loader")
    versions.add_argument("--limit", type=int, default=20)
    for command in (download, resolve, plan):
        command.add_argument("--version", required=True, help="Exact source version ID")
        command.add_argument("--file", help="Exact source file ID")
    download.add_argument(
        "--output",
        type=Path,
        help="Export a separate copy; otherwise return the verified cache path",
    )
    download.add_argument("--progress", action="store_true", help="Emit progress JSON to stderr")
    cache_command = commands.add_parser("cache", help="Inspect or safely clean owned cache objects")
    cache_command.add_argument("action", choices=("info", "verify", "prune", "clear"))
    remove = commands.add_parser("remove", help="Review and apply tracked mod removal")
    remove.add_argument("--source", required=True)
    remove.add_argument("--project", required=True)
    remove.add_argument("--apply", metavar="FINGERPRINT")
    remove.add_argument(
        "--also",
        action="append",
        default=[],
        metavar="SOURCE:PROJECT",
        help="Explicitly include another managed mod in the removal plan",
    )
    for name in ("installed", "recover", "remove"):
        operation = remove if name == "remove" else commands.add_parser(name)
        operation.add_argument("--game", required=True)
        operation.add_argument("--path", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "discover" and args.path is not None and args.game is None:
        parser.error("--path requires --game")
    try:
        settings = load_client_settings(args.config)
        configure_logging(level=settings.log_level)
        if args.command in {"installed", "recover", "remove"}:
            games = create_game_registry()
            installation = games.get(args.game).inspect(
                args.path.expanduser().resolve(), current_platform()
            )
            if installation is None:
                raise ModMeshError(
                    ErrorCode.VALIDATION, "Path is not a supported game installation"
                )
            manager = RemovalManager(games, installation)
            if args.command == "remove":
                references = [SourceRef(args.source, args.project)]
                for value in args.also:
                    source_id, separator, project_id = value.partition(":")
                    if not separator or not source_id or not project_id:
                        raise ModMeshError(ErrorCode.VALIDATION, "--also requires SOURCE:PROJECT")
                    references.append(SourceRef(source_id, project_id))
                removal = manager.plan_removal(references)
                payload = asdict(removal)
                payload.update(applicable=removal.applicable, fingerprint=removal.fingerprint)
                if args.apply:
                    payload["operation"] = asdict(manager.remove(removal, args.apply))
                print(json.dumps(_without_urls(payload), ensure_ascii=False, default=str))
                return 0 if removal.applicable else 1
            if args.command == "installed":
                installed_mods = manager.installed()
                print(
                    json.dumps(
                        [asdict(value) for value in installed_mods], ensure_ascii=False, default=str
                    )
                )
                return 0
            recovered = manager.recover()
            print(
                json.dumps([asdict(value) for value in recovered], ensure_ascii=False, default=str)
            )
            return 1 if any(value.state == "recovery_required" for value in recovered) else 0
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
        if args.command in {"versions", "download", "resolve", "plan"}:
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
    installation = None
    if args.command == "plan":
        installation = (
            create_game_registry()
            .get(args.game)
            .inspect(args.path.expanduser().resolve(), current_platform())
        )
        if installation is None:
            raise ModMeshError(ErrorCode.VALIDATION, "Path is not a supported game installation")
        actual_version = installation.version.value
        actual_loader = installation.loader.name if installation.loader else None
        if (args.game_version is not None and args.game_version != actual_version) or (
            args.loader is not None and args.loader != actual_loader
        ):
            raise ModMeshError(
                ErrorCode.CONFLICT, "Requested context differs from the detected game"
            )
        args.game_version, args.loader = actual_version, actual_loader
    async with source_registry() as registry:
        source = registry.get(args.source)
        project = await source.project(args.game, args.project)
        query = (
            VersionQuery(args.game_version, args.loader)
            if args.game_version or args.loader
            else None
        )
        versions = await source.versions(
            args.game,
            project.reference.project_id,
            query=query if args.command != "plan" or source.version_filters else None,
        )
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
        if args.command in {"resolve", "plan"}:
            with MetadataStore(settings.paths.database) as store:
                result = await DependencyResolver(
                    registry,
                    identities=IdentityRegistry(store),
                    priorities=source_priorities(settings.source_priorities),
                ).resolve(
                    args.game,
                    (ResolvedMod(project, version, files[0], True),),
                    game_version=args.game_version,
                    loader=args.loader,
                )
            if args.command == "plan":
                assert installation is not None
                return await _plan(args, settings, registry, installation, result)
            payload = asdict(result)
            for mod, resolved in zip(payload["mods"], result.mods, strict=True):
                mod["file"].pop("download_url", None)
                mod["version"] = _version_payload(resolved.version)
            report = analyze_dependencies(result)
            payload["conflicts"] = asdict(report)
            payload["blocked"] = report.blocked
            payload["solved"] = result.solved
            payload["metadata_complete"] = result.metadata_complete
            print(json.dumps(payload, ensure_ascii=False, default=str))
            return 0 if result.solved and not report.blocked else 1
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


async def _plan(
    args: argparse.Namespace,
    settings: ClientSettings,
    registry: SourceRegistry,
    installation: GameInstallation,
    resolution: DependencyResolution,
) -> int:
    report = analyze_dependencies(resolution)
    if report.blocked:
        print(
            json.dumps(
                {"blocked": True, "conflicts": asdict(report)}, ensure_ascii=False, default=str
            )
        )
        return 1
    cancelled = threading.Event()
    prepared = await prepare_resolution(
        settings,
        registry,
        installation,
        resolution,
        rule=args.rule,
        prefix=args.archive_prefix,
        replace_existing=args.replace_existing,
        cancelled=cancelled,
    )
    try:
        plan = prepared.plan
        payload = asdict(plan)
        payload["applicable"] = plan.applicable
        payload["fingerprint"] = plan.fingerprint
        if args.apply:
            result = await _blocking(
                partial(
                    prepared.manager.apply,
                    plan,
                    args.apply,
                    replace_existing=args.replace_existing,
                    cancelled=cancelled.is_set,
                ),
                cancelled,
            )
            payload["operation"] = asdict(result)
        print(json.dumps(_without_urls(payload), ensure_ascii=False, default=str))
        return 0 if plan.applicable else 1
    finally:
        prepared.close()


def _without_urls(value: object) -> object:
    if isinstance(value, dict):
        return {key: _without_urls(item) for key, item in value.items() if key != "download_url"}
    if isinstance(value, (list, tuple)):
        return [_without_urls(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
