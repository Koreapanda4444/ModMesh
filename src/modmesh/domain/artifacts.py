"""Content identity and upstream verification evidence."""

import re
from dataclasses import dataclass
from enum import StrEnum

from modmesh.core.diagnostics import ErrorCode, ModMeshError


class HashAlgorithm(StrEnum):
    SHA256 = "sha256"
    SHA512 = "sha512"
    SHA1 = "sha1"
    MD5 = "md5"


@dataclass(frozen=True)
class FileHash:
    algorithm: HashAlgorithm
    value: str

    def __post_init__(self) -> None:
        length = {
            HashAlgorithm.SHA256: 64,
            HashAlgorithm.SHA512: 128,
            HashAlgorithm.SHA1: 40,
            HashAlgorithm.MD5: 32,
        }[self.algorithm]
        if not re.fullmatch(rf"[a-fA-F0-9]{{{length}}}", self.value):
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid file hash")
        object.__setattr__(self, "value", self.value.lower())

    @property
    def strong(self) -> bool:
        return self.algorithm in {HashAlgorithm.SHA256, HashAlgorithm.SHA512}


@dataclass(frozen=True)
class Artifact:
    sha256: FileHash
    size: int
    verified_hashes: tuple[FileHash, ...] = ()

    def __post_init__(self) -> None:
        if self.sha256.algorithm != HashAlgorithm.SHA256 or self.size < 0:
            raise ModMeshError(
                ErrorCode.VALIDATION, "Artifact requires SHA-256 and a nonnegative size"
            )

    @property
    def artifact_id(self) -> str:
        return f"sha256:{self.sha256.value}"

    @property
    def source_verification(self) -> str:
        if any(value.strong for value in self.verified_hashes):
            return "strong"
        return "weak" if self.verified_hashes else "unverified"
