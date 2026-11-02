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
    with MetadataStore(settings.paths.database) as database:
        published = {
            key: read_launch_receipt(value)
            for key, value in database.list("launch.attempts")
            if value.get("target") == target
        }
    consistent = core == published
    attempts = {**published, **core}
    report = games.get(installation.game_id).validate_operation(installation)
    observed = any(item.code == "game_running" for item in report.issues)
    return {
        "target": target,
        "mutation_blocked": blocked,
        "journal_consistent": consistent,
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
