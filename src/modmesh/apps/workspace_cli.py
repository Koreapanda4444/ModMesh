from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from modmesh.apps.runtime import create_game_registry
from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.workspaces import WorkspaceStore
from modmesh.storage.metadata import MetadataStore


def configure_workspace_cli(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser("workspace", help="Edit independent local modpack workspaces")
    parser.add_argument(
        "action", choices=("list", "create", "show", "rename", "clone", "delete-plan", "delete")
    )
    parser.add_argument("--game")
    parser.add_argument("--id", dest="pack_id")
    parser.add_argument("--name")
    parser.add_argument("--expected", help="Workspace digest returned by show")
    parser.add_argument("--approve", help="Exact deletion plan fingerprint")


def workspace_command(args: argparse.Namespace, settings: ClientSettings) -> int:
    with MetadataStore(settings.paths.database) as database:
        store = WorkspaceStore(database)
        if args.action == "list":
            result: object = [
                {**item.document(), "digest": item.digest} for item in store.list(args.game)
            ]
        elif args.action == "create":
            if not args.game or not args.name:
                raise ModMeshError(
                    ErrorCode.VALIDATION, "Workspace creation needs --game and --name"
                )
            create_game_registry(settings).get(args.game)
            workspace = store.create(args.game, args.name)
            result = {**workspace.document(), "digest": workspace.digest}
        else:
            if not args.pack_id:
                raise ModMeshError(ErrorCode.VALIDATION, "Select --id PACK_ID")
            if args.action in {"rename", "clone"}:
                if not args.name or not args.expected:
                    raise ModMeshError(
                        ErrorCode.VALIDATION, "Editing needs --name and --expected DIGEST"
                    )
                workspace = (store.rename if args.action == "rename" else store.clone)(
                    args.pack_id, args.name, args.expected
                )
                result = {**workspace.document(), "digest": workspace.digest}
            elif args.action == "delete-plan":
                plan = store.deletion_plan(args.pack_id)
                result = {
                    **asdict(plan),
                    "applicable": plan.applicable,
                    "fingerprint": plan.fingerprint,
                }
            elif args.action == "delete":
                store.delete(args.pack_id, args.approve or "")
                result = {"archived": args.pack_id, "game_files_changed": False}
            else:
                workspace = store.get(args.pack_id)
                result = {**workspace.document(), "digest": workspace.digest}
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
