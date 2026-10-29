from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from modmesh.apps.library import GameLibrary
from modmesh.apps.runtime import create_game_registry
from modmesh.apps.workspace_activation import (
    active_workspace,
    apply_workspace,
    prepare_workspace,
    recover_workspace,
)
from modmesh.apps.workspace_artifacts import import_local
from modmesh.apps.workspace_environment import inspect_workspace_environment
from modmesh.apps.workspace_legacy import adopt_profile, import_manifest_workspace
from modmesh.apps.workspace_lifecycle import freeze_workspace, write_lock
from modmesh.apps.workspace_restore import apply_restore, prepare_restore
from modmesh.apps.workspace_updates import prepare_update, save_update
from modmesh.core.compatibility import inspect_compatibility
from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.discovery import current_platform
from modmesh.core.games import DiscoveryContext
from modmesh.core.pack_io import read_manifest
from modmesh.core.workspace_branches import WorkspaceBranches
from modmesh.core.workspace_history import WorkspaceHistory
from modmesh.core.workspaces import WorkspaceStore
from modmesh.domain.models import SourceRef
from modmesh.storage.metadata import MetadataStore


def configure_workspace_cli(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser("workspace", help="Edit independent local modpack workspaces")
    parser.add_argument(
        "action",
        choices=(
            "list",
            "create",
            "show",
            "rename",
            "clone",
            "delete-plan",
            "delete",
            "import",
            "enable",
            "disable",
            "pin",
            "unpin",
            "recipe",
            "remove-entry",
            "order",
            "lock",
            "snapshot",
            "history",
            "snapshot-show",
            "adopt-profile",
            "import-manifest",
            "plan",
            "apply",
            "active",
            "recover",
            "compatibility",
            "environment",
            "update-plan",
            "update",
            "protect-plan",
            "protect",
            "unprotect-plan",
            "unprotect",
            "fork-plan",
            "fork",
            "branch-info",
            "restore-plan",
            "restore",
        ),
    )
    parser.add_argument("--game")
    parser.add_argument("--id", dest="pack_id")
    parser.add_argument("--name")
    parser.add_argument("--expected", help="Workspace digest returned by show")
    parser.add_argument("--approve", help="Exact deletion or activation plan fingerprint")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--source")
    parser.add_argument("--project")
    parser.add_argument("--rule")
    parser.add_argument("--prefix")
    parser.add_argument("--order", action="append", default=[])
    parser.add_argument("--path", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--snapshot")
    parser.add_argument("--profile")
    parser.add_argument(
        "--allow-pinned", action="append", default=[], help="Explicit SOURCE:PROJECT pin override"
    )


def workspace_command(args: argparse.Namespace, settings: ClientSettings) -> int:
    if args.action in {"restore-plan", "restore"}:
        if not args.pack_id or args.path is None or not args.expected or not args.snapshot:
            raise ModMeshError(
                ErrorCode.VALIDATION, "Restore needs --id, --path, --expected and --snapshot"
            )
        with MetadataStore(settings.paths.database) as database:
            selected_workspace = WorkspaceStore(database).get(args.pack_id)
        installation = GameLibrary(create_game_registry(settings), settings.paths.database).inspect(
            selected_workspace.game_id, args.path, DiscoveryContext(current_platform())
        )
        restore = asyncio.run(
            prepare_restore(settings, args.pack_id, args.snapshot, installation, args.expected)
        )
        if args.action == "restore":
            receipt = apply_restore(settings, restore, args.approve or "")
            print(
                json.dumps(
                    {"state": "committed", "receipt": receipt.document(), "game_downgraded": False},
                    ensure_ascii=False,
                )
            )
            return 0
        print(json.dumps(restore.document(), ensure_ascii=False))
        return int(not restore.update.applicable)
    if args.action in {
        "protect-plan",
        "protect",
        "unprotect-plan",
        "unprotect",
        "fork-plan",
        "fork",
        "branch-info",
    }:
        if not args.pack_id:
            raise ModMeshError(ErrorCode.VALIDATION, "Select --id PACK_ID")
        with MetadataStore(settings.paths.database) as database:
            if args.action == "branch-info":
                branch_result = database.get("workspaces.branches", args.pack_id) or {
                    "kind": "editable"
                }
            else:
                if not args.expected:
                    raise ModMeshError(
                        ErrorCode.VALIDATION, "Branch changes need --expected DIGEST"
                    )
                branches = WorkspaceBranches(database)
                branch_plan = branches.plan(
                    args.pack_id, args.expected, args.action.removesuffix("-plan"), args.name
                )
                if args.action.endswith("-plan"):
                    branch_result = branch_plan.document()
                else:
                    changed = branches.apply(branch_plan, args.approve or "")
                    branch_result = {**changed.document(), "digest": changed.digest}
        print(json.dumps(branch_result, ensure_ascii=False))
        return 0
    if args.action in {"update-plan", "update"}:
        if not args.pack_id or args.path is None or not args.expected:
            raise ModMeshError(ErrorCode.VALIDATION, "Update needs --id, --path and --expected")
        with MetadataStore(settings.paths.database) as database:
            workspace = WorkspaceStore(database).get(args.pack_id)
        installation = GameLibrary(create_game_registry(settings), settings.paths.database).inspect(
            workspace.game_id, args.path, DiscoveryContext(current_platform())
        )
        candidate = read_manifest(args.file) if args.file else None
        if candidate is not None and candidate.context.game_id != workspace.game_id:
            raise ModMeshError(ErrorCode.CONFLICT, "Update manifest belongs to another game")
        overrides = tuple(
            SourceRef(*value.split(":", 1)) for value in args.allow_pinned if ":" in value
        )
        if len(overrides) != len(args.allow_pinned):
            raise ModMeshError(ErrorCode.VALIDATION, "Pin overrides use SOURCE:PROJECT")
        update = asyncio.run(
            prepare_update(
                settings,
                args.pack_id,
                installation,
                args.expected,
                entries=candidate.entries if candidate else None,
                settings_files=candidate.settings if candidate else None,
                allow_pinned=overrides,
            )
        )
        if args.action == "update":
            saved = save_update(settings, update, args.approve or "")
            print(
                json.dumps(
                    {**saved.document(), "digest": saved.digest, "game_files_changed": False},
                    ensure_ascii=False,
                )
            )
            return 0
        print(json.dumps(update.document(), ensure_ascii=False))
        return int(not update.applicable)
    if args.action in {"active", "recover"}:
        if not args.game or args.path is None:
            raise ModMeshError(ErrorCode.VALIDATION, "Select --game and --path")
        installation = GameLibrary(create_game_registry(settings), settings.paths.database).inspect(
            args.game, args.path, DiscoveryContext(current_platform())
        )
        if args.action == "recover":
            recovered = recover_workspace(settings, installation)
            print(json.dumps([asdict(item) for item in recovered], ensure_ascii=False))
            return int(any(item.state == "recovery_required" for item in recovered))
        status = active_workspace(settings, installation)
        print(json.dumps(status, ensure_ascii=False))
        return int(status["state"] not in {"none", "active"})
    if args.action in {"plan", "apply"}:
        if not args.pack_id or args.path is None or not args.expected:
            raise ModMeshError(
                ErrorCode.VALIDATION, "Activation plan needs --id, --path and --expected"
            )
        with MetadataStore(settings.paths.database) as database:
            workspace = WorkspaceStore(database).get(args.pack_id)
        installation = GameLibrary(create_game_registry(settings), settings.paths.database).inspect(
            workspace.game_id, args.path, DiscoveryContext(current_platform())
        )
        prepared = asyncio.run(
            prepare_workspace(settings, args.pack_id, installation, args.expected)
        )
        if args.action == "apply":
            receipt = apply_workspace(settings, prepared, args.approve or "")
            print(
                json.dumps(
                    {"state": "committed", "receipt": receipt.document()}, ensure_ascii=False
                )
            )
            return 0
        print(json.dumps(prepared.document(), ensure_ascii=False))
        return 0 if prepared.applicable else 1
    if args.action in {"adopt-profile", "import-manifest"}:
        if args.action == "import-manifest":
            if args.file is None:
                raise ModMeshError(ErrorCode.VALIDATION, "Select --file MANIFEST")
            adopted = import_manifest_workspace(settings, read_manifest(args.file))
        else:
            if not args.game or args.path is None or not args.profile:
                raise ModMeshError(ErrorCode.VALIDATION, "Select --game, --path and --profile")
            installation = GameLibrary(
                create_game_registry(settings), settings.paths.database
            ).inspect(args.game, args.path, DiscoveryContext(current_platform()))
            adopted = adopt_profile(settings, installation, args.profile, name=args.name)
        print(json.dumps({**adopted.document(), "digest": adopted.digest}, ensure_ascii=False))
        return 0
    if args.action in {"lock", "snapshot", "compatibility", "environment"}:
        if not args.pack_id or not args.expected or args.path is None:
            raise ModMeshError(
                ErrorCode.VALIDATION, "Lock preview needs --id, --path and --expected"
            )
        with MetadataStore(settings.paths.database) as database:
            workspace = WorkspaceStore(database).get(args.pack_id)
        installation = GameLibrary(create_game_registry(settings), settings.paths.database).inspect(
            workspace.game_id, args.path, DiscoveryContext(current_platform())
        )
        if args.action == "environment":
            if workspace.digest != args.expected:
                raise ModMeshError(ErrorCode.CONFLICT, "Workspace changed")
            drift = inspect_workspace_environment(settings, workspace, installation)
            print(json.dumps(drift.document(), ensure_ascii=False))
            return int(bool(drift.changes))
        lock = freeze_workspace(settings, args.pack_id, installation, args.expected)
        if args.action == "compatibility":
            report = inspect_compatibility(lock)
            with MetadataStore(settings.paths.database) as database:
                database.put("workspaces.compatibility", lock.digest, report.document())
            print(json.dumps(report.document(), ensure_ascii=False))
            return int(report.state == "conflict")
        if args.action == "snapshot":
            with MetadataStore(settings.paths.database) as database:
                WorkspaceHistory(database).capture(lock)
        if args.output is not None:
            write_lock(args.output, lock)
        print(json.dumps({"digest": lock.digest, "lock": lock.document()}, ensure_ascii=False))
        return 0
    if args.action in {"history", "snapshot-show"}:
        with MetadataStore(settings.paths.database) as database:
            history = WorkspaceHistory(database)
            if args.action == "snapshot-show":
                if not args.snapshot:
                    raise ModMeshError(ErrorCode.VALIDATION, "Select --snapshot DIGEST")
                history_result: object = history.get(args.snapshot).document()
            else:
                if not args.pack_id:
                    raise ModMeshError(ErrorCode.VALIDATION, "Select --id PACK_ID")
                history_result = [
                    {
                        "digest": item.lock.digest,
                        "revision": item.lock.workspace.revision,
                        "created_at": item.created_at,
                    }
                    for item in history.list(args.pack_id)
                ]
        print(json.dumps(history_result, ensure_ascii=False))
        return 0
    if args.action == "import":
        if not args.pack_id or args.file is None or not args.expected:
            raise ModMeshError(ErrorCode.VALIDATION, "Import needs --id, --file and --expected")
        imported = asyncio.run(import_local(settings, args.pack_id, args.file, args.expected))
        print(json.dumps({**imported.document(), "digest": imported.digest}, ensure_ascii=False))
        return 0
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
            if args.action in {
                "enable",
                "disable",
                "pin",
                "unpin",
                "recipe",
                "remove-entry",
                "order",
            }:
                if not args.expected:
                    raise ModMeshError(ErrorCode.VALIDATION, "Editing needs --expected DIGEST")
                if args.action == "order":
                    refs = tuple(
                        SourceRef(*value.split(":", 1)) for value in args.order if ":" in value
                    )
                    if len(refs) != len(args.order):
                        raise ModMeshError(
                            ErrorCode.VALIDATION, "Order uses SOURCE:PROJECT references"
                        )
                    workspace = store.reorder(args.pack_id, refs, args.expected)
                else:
                    if not args.source or not args.project:
                        raise ModMeshError(ErrorCode.VALIDATION, "Select --source and --project")
                    workspace = store.edit_entry(
                        args.pack_id,
                        SourceRef(args.source, args.project),
                        args.expected,
                        enabled=True
                        if args.action == "enable"
                        else False
                        if args.action == "disable"
                        else None,
                        pinned=True
                        if args.action == "pin"
                        else False
                        if args.action == "unpin"
                        else None,
                        rule_id=args.rule if args.action == "recipe" else None,
                        prefix=args.prefix if args.action == "recipe" else None,
                        remove=args.action == "remove-entry",
                        set_recipe=args.action == "recipe",
                    )
                print(
                    json.dumps(
                        {**workspace.document(), "digest": workspace.digest}, ensure_ascii=False
                    )
                )
                return 0
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
