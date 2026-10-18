"""Core command-line access while the full desktop views are being implemented."""

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from modmesh.apps.runtime import create_game_registry
from modmesh.core.config import load_client_settings
from modmesh.core.diagnostics import ModMeshError, configure_logging
from modmesh.core.discovery import DiscoveryEngine, current_platform
from modmesh.core.games import DiscoveryContext
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
    args = parser.parse_args()
    if args.path is not None and args.game is None:
        parser.error("--path requires --game")
    try:
        settings = load_client_settings(args.config)
        configure_logging(level=settings.log_level)
        context = DiscoveryContext(current_platform(), tuple(args.root), os.environ)
        with MetadataStore(settings.paths.database) as store:
            engine = DiscoveryEngine(create_game_registry(), store)
            if args.path is not None:
                engine.register_manual(args.game, args.path, context)
            result = engine.discover(context, game_id=args.game)
            print(json.dumps(asdict(result), ensure_ascii=False, default=str))
        return 0
    except ModMeshError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
