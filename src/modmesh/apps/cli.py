"""Core command-line access while the full desktop views are being implemented."""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from modmesh.apps.runtime import create_game_registry, source_registry
from modmesh.core.config import ClientSettings, load_client_settings
from modmesh.core.diagnostics import ModMeshError, configure_logging
from modmesh.core.discovery import DiscoveryEngine, current_platform
from modmesh.core.games import DiscoveryContext
from modmesh.core.identity import IdentityRegistry
from modmesh.core.matching import ProjectMatcher
from modmesh.core.search import SearchState, UnifiedSearch
from modmesh.core.sources import SearchRequest, SearchSort
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
    args = parser.parse_args()
    if args.command == "discover" and args.path is not None and args.game is None:
        parser.error("--path requires --game")
    try:
        settings = load_client_settings(args.config)
        configure_logging(level=settings.log_level)
        if args.command == "search":
            return asyncio.run(_search(args, settings))
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
        result = await UnifiedSearch(registry).search(request, source_ids=args.source)
        payload = asdict(result)
        with MetadataStore(settings.paths.database) as store:
            payload["matching"] = asdict(
                ProjectMatcher(IdentityRegistry(store)).match(result.projects)
            )
        print(json.dumps(payload, ensure_ascii=False, default=str))
        return 0 if any(source.state == SearchState.SUCCESS for source in result.sources) else 1


if __name__ == "__main__":
    raise SystemExit(main())
