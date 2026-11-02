from configparser import ConfigParser, Error
from dataclasses import asdict
from pathlib import Path

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.planning import portable_path, snapshot, target_path
from modmesh.core.workspaces import document_digest


def prism_instance(root: Path) -> tuple[tuple[str, ...], str]:
    instance = root.parent
    if root.name not in {"minecraft", ".minecraft"} or instance.parent.name != "instances":
        raise ModMeshError(ErrorCode.UNSUPPORTED, "Custom Prism layouts require manual launch")
    identifier = portable_path(instance.name)
    if "/" in identifier or identifier.startswith("-"):
        raise ModMeshError(ErrorCode.VALIDATION, "Invalid Prism instance folder identifier")
    application = instance.parent.parent
    config = target_path(application, "prismlauncher.cfg")
    proof = snapshot(config)
    if proof.size > 2 * 1024**2:
        raise ModMeshError(ErrorCode.VALIDATION, "Prism configuration exceeds the size limit")
    parser = ConfigParser(interpolation=None, strict=True, empty_lines_in_values=False)
    try:
        with config.open("r", encoding="utf-8-sig") as stream:
            parser.read_string(stream.read(2 * 1024**2 + 1))
        if (
            parser.defaults()
            or parser.get("General", "InstanceDir", fallback="instances") != "instances"
            or parser.get("General", "AdditionalInstanceDirs", fallback="") != ""
        ):
            raise ModMeshError(
                ErrorCode.UNSUPPORTED,
                "Custom or additional Prism instance directories require manual launch",
            )
    except (Error, UnicodeError, OSError) as exc:
        raise ModMeshError(
            ErrorCode.VALIDATION, "Cannot verify Prism instance directory settings"
        ) from exc
    if snapshot(config) != proof:
        raise ModMeshError(ErrorCode.CONFLICT, "Prism directory settings changed while reading")
    metadata = {
        "launcher_config": asdict(proof),
        "pack": asdict(snapshot(target_path(application, f"instances/{identifier}/mmc-pack.json"))),
        "config": asdict(
            snapshot(target_path(application, f"instances/{identifier}/instance.cfg"))
        ),
        "application": str(application),
        "instance": identifier,
        "instance_directory": "instances",
        "additional_directories": "",
    }
    return ("--dir", str(application), "--launch", identifier), document_digest(metadata)
