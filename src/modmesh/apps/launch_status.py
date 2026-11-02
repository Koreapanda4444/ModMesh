from datetime import UTC, datetime

from modmesh.apps.runtime import create_game_registry
from modmesh.apps.workspace_legacy import activation_target
from modmesh.core.config import ClientSettings
from modmesh.core.launch_guard import launch_hold
from modmesh.core.launch_receipts import read_launch_receipt
from modmesh.core.profiles import ProfileManager
from modmesh.domain.models import GameInstallation
from modmesh.storage.metadata import MetadataStore


def launch_status(settings: ClientSettings, installation: GameInstallation) -> dict[str, object]:
    target = activation_target(installation)
    games = create_game_registry(settings)
    manager = ProfileManager(games, installation)
    with manager._locked() as store:
        blocked = launch_hold(store) is not None
        core = (
            {key: read_launch_receipt(value) for key, value in store.list("launch.attempts")}
            if store
            else {}
        )
        active = store.get("launch.guard", "active") if store else None
        if active is not None and active.get("state") != "released":
            active = read_launch_receipt(active)
    with MetadataStore(settings.paths.database) as database:
        published = {
            key: read_launch_receipt(value)
            for key, value in database.list("launch.attempts")
            if value.get("target") == target
        }
        releases = [
            value for _, value in database.list("launch.releases") if value.get("target") == target
        ]
    consistent = core == published
    attempts = {**published, **core}
    if active is not None and active.get("state") != "released":
        identity = str(active["attempt_id"])
        consistent = consistent and core.get(identity) == active
        attempts[identity] = active
    report = games.get(installation.game_id).validate_operation(installation)
    observed = any(item.code == "game_running" for item in report.issues)
    latest = next(
        iter(sorted(attempts.values(), key=lambda value: str(value["created_at"]), reverse=True)),
        None,
    )
    outcome = str(latest["state"]) if latest else "none"
    guidance = {
        "invoking": "The app stopped around OS handoff. Check the game and launcher; "
        "do not assume launch failed or retry automatically.",
        "uncertain": "Inspect the existing launcher and game. Close both and clear queued requests "
        "before explicitly releasing the guard.",
        "accepted": "OS or launcher accepted the request. "
        "Game and mod loading need separate evidence. Close the game before changing its pack.",
        "failed": "OS could not create the launch process. Check the installed URI handler, "
        "approved executable path and permissions, then review a fresh Play plan.",
        "manual_required": "The applied pack is retained. Open this exact instance "
        "using its existing launcher and normal account.",
        "none": "No recorded launch request. Review a Play plan to apply and launch a pack.",
    }[outcome]
    return {
        "target": target,
        "mutation_blocked": blocked,
        "journal_consistent": consistent,
        "releases": releases,
        "guidance": guidance,
        "automatic_retry": False,
        "attempts": sorted(
            attempts.values(), key=lambda value: str(value["created_at"]), reverse=True
        ),
        "current_observation": {
            "game_process_observed": observed,
            "mod_loading": "unverified",
            "observed_at": datetime.now(UTC).isoformat(),
            "issues": [{"code": item.code, "message": item.message} for item in report.issues],
        },
    }
