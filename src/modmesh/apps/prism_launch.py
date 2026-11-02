from dataclasses import asdict, replace
from pathlib import Path

from modmesh.core.config import ClientSettings
from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.launching import LaunchMethod, LaunchPlan
from modmesh.core.planning import portable_path, snapshot, target_path
from modmesh.core.workspaces import document_digest
from modmesh.storage.metadata import MetadataStore


def prism_plan(base: LaunchPlan, executable: Path) -> LaunchPlan:
    if base.game_id != "minecraft" or base.root.name not in {"minecraft", ".minecraft"}:
        raise ModMeshError(ErrorCode.UNSUPPORTED, "Select a verified Prism instance game directory")
    instance = base.root.parent
    if instance.parent.name != "instances":
        raise ModMeshError(
            ErrorCode.UNSUPPORTED, "Custom Prism instance layouts need manual launch"
        )
    identifier = portable_path(instance.name)
    if "/" in identifier or identifier.startswith("-"):
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid Prism instance folder identifier")
    application = instance.parent.parent
    if executable.is_symlink() or executable.is_junction():
        raise ModMeshError(ErrorCode.VALIDATION, "Select the Prism executable itself")
    executable = executable.expanduser().resolve(strict=True)
    if executable.name.casefold() not in {
        "prismlauncher",
        "prismlauncher.exe",
        "prismlauncher.appimage",
    }:
        raise ModMeshError(ErrorCode.UNSUPPORTED, "Select a PrismLauncher executable")
    proof = snapshot(executable)
    metadata = {
        "pack": asdict(snapshot(target_path(application, f"instances/{identifier}/mmc-pack.json"))),
        "config": asdict(
            snapshot(target_path(application, f"instances/{identifier}/instance.cfg"))
        ),
        "application": str(application),
        "instance": identifier,
    }
    instance_digest = document_digest(metadata)
    arguments = ("--dir", str(application), "--launch", identifier)
    binding_digest = document_digest(
        {
            "target": base.target,
            "executable": str(executable),
            "proof": asdict(proof),
            "arguments": arguments,
            "instance_digest": instance_digest,
        }
    )
    return replace(
        base,
        method=LaunchMethod.PRISM,
        executable=executable,
        executable_proof=proof,
        arguments=arguments,
        binding_digest=binding_digest,
        instance_digest=instance_digest,
        instruction="Use the existing Prism account and ownership checks; "
        "selected executable hash is user approval, not a vendor signature",
    )


def bind_prism(settings: ClientSettings, plan: LaunchPlan, approval: str) -> None:
    if plan.method != LaunchMethod.PRISM or plan.executable is None or approval != plan.fingerprint:
        raise ModMeshError(ErrorCode.CONFLICT, "Approve the exact Prism launch preview")
    if prism_plan(plan, plan.executable).fingerprint != approval:
        raise ModMeshError(ErrorCode.CONFLICT, "Prism instance or executable changed")
    with MetadataStore(settings.paths.database) as database:
        database.put(
            "launch.bindings",
            plan.target,
            {
                "executable": str(plan.executable),
                "binding_digest": plan.binding_digest,
            },
        )


def bound_prism(settings: ClientSettings, base: LaunchPlan) -> LaunchPlan:
    with MetadataStore(settings.paths.database) as database:
        binding = database.get("launch.bindings", base.target)
    if binding is None:
        return base
    executable = binding.get("executable")
    if not isinstance(executable, str) or not Path(executable).is_absolute():
        raise ModMeshError(ErrorCode.STORAGE, "Invalid saved Prism binding")
    plan = prism_plan(base, Path(executable))
    if plan.binding_digest != binding.get("binding_digest"):
        raise ModMeshError(ErrorCode.CONFLICT, "Prism changed; review and approve a new binding")
    return plan
