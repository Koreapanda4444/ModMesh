from dataclasses import replace

from modmesh.apps.prism_launch import bound_prism
from modmesh.apps.runtime import create_game_registry
from modmesh.apps.workspace_legacy import activation_target
from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.game_rules import LaunchKind
from modmesh.core.launching import LaunchMethod, LaunchPlan
from modmesh.core.planning import snapshot, target_path
from modmesh.domain.models import GameInstallation
from modmesh.storage.rules import load_rules


def prepare_launch(
    settings: ClientSettings, installation: GameInstallation, *, use_binding: bool = True
) -> LaunchPlan:
    registered = load_rules(settings.paths.database).get(installation.game_id)
    if not registered.approved:
        raise ModMeshError(ErrorCode.CONFLICT, "Launch needs an approved game rule")
    adapter = create_game_registry(settings).get(installation.game_id)
    current = adapter.inspect(installation.path, installation.platform)
    if (
        current is None
        or current.path != installation.path
        or current.version != installation.version
        or current.loader != installation.loader
    ):
        raise ModMeshError(ErrorCode.CONFLICT, "Game environment changed before launch preview")
    report = adapter.validate(current)
    if not report.valid:
        raise ModMeshError(ErrorCode.CONFLICT, "Game environment failed launch validation")
    root = current.path.stat(follow_symlinks=False)
    common = LaunchPlan(
        game_id=current.game_id,
        target=activation_target(current),
        platform=current.platform,
        root=current.path,
        root_device=root.st_dev,
        root_inode=root.st_ino,
        rule_sha256=registered.rule.digest,
        method=LaunchMethod.MANUAL,
    )
    definition = registered.rule.launch
    if definition.strategy == LaunchKind.STEAM:
        return replace(
            common,
            method=LaunchMethod.STEAM,
            uri="steam://run/" + str(definition.app_id),
            instruction="Hand off to the installed Steam client; "
            "use its normal account and ownership checks",
        )
    if definition.strategy == LaunchKind.EXECUTABLE:
        assert definition.executable is not None
        executable = target_path(current.path, definition.executable)
        return replace(
            common,
            method=LaunchMethod.EXECUTABLE,
            executable=executable,
            executable_proof=snapshot(executable),
            arguments=definition.arguments,
            instruction="Start only the executable selected by the approved rule",
        )
    manual = replace(
        common,
        method=LaunchMethod.MANUAL,
        instruction="Apply the selected pack, then launch this exact game instance "
        "in its existing launcher",
    )
    return (
        bound_prism(settings, manual) if current.game_id == "minecraft" and use_binding else manual
    )
