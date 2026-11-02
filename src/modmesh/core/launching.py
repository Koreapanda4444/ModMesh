from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.planning import FileSnapshot
from modmesh.core.workspaces import document_digest
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.domain.models import Platform
from modmesh.domain.validation import require_id


class LaunchMethod(StrEnum):
    MANUAL = "manual"
    EXECUTABLE = "executable"
    STEAM = "steam"
    PRISM = "prism"


class LaunchCallState(StrEnum):
    ACCEPTED = "accepted"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    MANUAL = "manual_required"


@dataclass(frozen=True)
class LaunchPlan:
    game_id: str
    target: str
    platform: Platform
    root: Path
    root_device: int
    root_inode: int
    rule_sha256: str
    method: LaunchMethod
    executable: Path | None = None
    executable_proof: FileSnapshot | None = None
    arguments: tuple[str, ...] = ()
    uri: str | None = None
    binding_digest: str | None = None
    instance_digest: str | None = None
    instruction: str = ""

    def __post_init__(self) -> None:
        require_id(self.game_id, "launch game")
        FileHash(HashAlgorithm.SHA256, self.rule_sha256)
        if not self.root.is_absolute() or self.root_inode < 1:
            raise ModMeshError(
                ErrorCode.VALIDATION, "Launch needs an identified absolute game root"
            )
        if len(self.arguments) > 16 or any(
            not item or len(item) > 4096 or any(ord(char) < 32 for char in item)
            for item in self.arguments
        ):
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid bounded launch arguments")
        if self.method == LaunchMethod.MANUAL and (
            self.executable is not None or self.uri is not None or self.arguments
        ):
            raise ModMeshError(ErrorCode.VALIDATION, "Manual launch cannot carry a command")
        if self.method in {LaunchMethod.EXECUTABLE, LaunchMethod.PRISM} and (
            self.executable is None
            or self.executable_proof is None
            or not self.executable.is_absolute()
            or self.uri is not None
        ):
            raise ModMeshError(
                ErrorCode.VALIDATION, "Executable launch requires an exact file proof"
            )
        if self.method == LaunchMethod.STEAM and (
            self.executable is not None or self.arguments or self.uri is None
        ):
            raise ModMeshError(
                ErrorCode.VALIDATION, "Steam handoff requires only a fixed application URI"
            )
        if self.method == LaunchMethod.STEAM and (
            self.uri is None
            or re.fullmatch(r"steam://run/[1-9][0-9]{0,9}", self.uri) is None
            or int(self.uri.rsplit("/", 1)[1]) > 4294967295
        ):
            raise ModMeshError(
                ErrorCode.VALIDATION, "Only a fixed Steam application run URI is allowed"
            )
        for digest in (self.binding_digest, self.instance_digest):
            if digest is not None:
                FileHash(HashAlgorithm.SHA256, digest)

    @property
    def fingerprint(self) -> str:
        return document_digest(self.document())

    def document(self) -> dict[str, object]:
        return {
            "game_id": self.game_id,
            "target": self.target,
            "platform": self.platform.value,
            "root": str(self.root),
            "root_device": self.root_device,
            "root_inode": self.root_inode,
            "rule_sha256": self.rule_sha256,
            "method": self.method.value,
            "executable": str(self.executable) if self.executable else None,
            "executable_proof": asdict(self.executable_proof) if self.executable_proof else None,
            "arguments": list(self.arguments),
            "uri": self.uri,
            "binding_digest": self.binding_digest,
            "instance_digest": self.instance_digest,
            "instruction": self.instruction,
            "mod_loading": "unverified",
        }


@dataclass(frozen=True)
class LaunchCall:
    state: LaunchCallState
    pid: int | None = None
    process_started_at: float | None = None
    detail: str = ""

    def document(self) -> dict[str, object]:
        return {**asdict(self), "game_process_observed": False, "mod_loading": "unverified"}


class LaunchBoundary(Protocol):
    def invoke(self, plan: LaunchPlan) -> LaunchCall: ...
