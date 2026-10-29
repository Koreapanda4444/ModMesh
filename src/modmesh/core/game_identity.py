from __future__ import annotations

from dataclasses import asdict, dataclass

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.game_rules import GameRule
from modmesh.core.hashing import verify_file
from modmesh.core.packs import PackContext, closed
from modmesh.core.planning import portable_path, target_path
from modmesh.core.workspaces import document_digest
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.domain.models import GameInstallation
from modmesh.storage.installed import array_record, integer_record, string_record


@dataclass(frozen=True)
class IdentityFile:
    path: str
    sha256: str
    size: int
    role: str

    @classmethod
    def read(cls, value: object) -> IdentityFile:
        data = closed(value, {"path", "sha256", "size", "role"})
        path = portable_path(string_record(data["path"]))
        sha = FileHash(HashAlgorithm.SHA256, string_record(data["sha256"])).value
        size, role = integer_record(data["size"]), string_record(data["role"])
        if not 0 <= size <= 2 * 1024**3 or role not in {"executable", "loader"}:
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid game identity file")
        return cls(path, sha, size, role)


@dataclass(frozen=True)
class GameIdentity:
    context: PackContext
    version_source: str
    files: tuple[IdentityFile, ...]
    unknown: tuple[str, ...]

    @property
    def digest(self) -> str:
        return document_digest(self.document())

    def document(self) -> dict[str, object]:
        return {
            "context": self.context.document(),
            "version_source": self.version_source,
            "files": [asdict(item) for item in self.files],
            "unknown": list(self.unknown),
        }

    @classmethod
    def read(cls, value: object) -> GameIdentity:
        data = closed(value, {"context", "version_source", "files", "unknown"})
        source = string_record(data["version_source"])
        if source not in {"platform_metadata", "launcher_metadata", "unknown"}:
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid game version evidence source")
        files = tuple(IdentityFile.read(item) for item in array_record(data["files"]))
        unknown = tuple(string_record(item) for item in array_record(data["unknown"]))
        if len(files) > 128 or len({item.path.casefold() for item in files}) != len(files):
            raise ModMeshError(ErrorCode.VALIDATION, "Duplicate or excessive game identity files")
        if len(unknown) > 16 or any(len(item) > 200 or not item.isprintable() for item in unknown):
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid game identity limitations")
        return cls(PackContext.read(data["context"]), source, files, unknown)


def capture_game_identity(installation: GameInstallation, rule: GameRule) -> GameIdentity:
    if rule.game_id != installation.game_id:
        raise ModMeshError(ErrorCode.CONFLICT, "Game identity rule mismatch")
    source, evidence = "unknown", installation.version.evidence or ""
    if installation.version.build_id is not None and "steam-appmanifest:" in evidence:
        source = "platform_metadata"
    elif installation.version.value is not None and evidence.startswith(
        ("prism-multimc:", "minecraft-launcher:")
    ):
        source = "launcher_metadata"
    unknown = ["Game runtime and executable semantic version are not verified"]
    if source == "unknown":
        unknown.append("Game version has no verified platform or launcher metadata")
    if installation.loader is not None and installation.loader.version is None:
        unknown.append("Loader version is unknown; marker hashes identify content only")
    candidates = {
        name: "executable"
        for name in rule.detect.required_files
        if name.lower().endswith((".exe", ".dll", ".jar"))
    }
    candidates.update({name: "loader" for name in rule.loader.markers})
    files = []
    for name, role in sorted(candidates.items()):
        path = target_path(installation.path, name)
        if not path.exists():
            if role == "executable":
                raise ModMeshError(ErrorCode.CONFLICT, "Required game identity file disappeared")
            continue
        if path.stat(follow_symlinks=False).st_size > 2 * 1024**3:
            raise ModMeshError(ErrorCode.UNSUPPORTED, "Game identity file exceeds 2 GiB")
        artifact = verify_file(path)
        files.append(IdentityFile(name, artifact.sha256.value, artifact.size, role))
    return GameIdentity.read(
        GameIdentity(
            PackContext.from_installation(installation), source, tuple(files), tuple(unknown)
        ).document()
    )
