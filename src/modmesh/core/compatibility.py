from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum

from modmesh.core.conflicts import analyze_dependencies
from modmesh.core.lockfiles import PackLock
from modmesh.core.planning import PlanIssue


class CompatibilityState(StrEnum):
    CONFIRMED = "confirmed"
    LIKELY = "likely"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class CompatibilityEvidence:
    code: str
    state: CompatibilityState
    scope: str
    detail: str


@dataclass(frozen=True)
class CompatibilityReport:
    state: CompatibilityState
    checked_at: str
    lock_digest: str
    evidence: tuple[CompatibilityEvidence, ...]

    def document(self) -> dict[str, object]:
        return asdict(self)


def inspect_compatibility(
    lock: PackLock, issues: tuple[PlanIssue, ...] = ()
) -> CompatibilityReport:
    evidence = []
    conflicts = analyze_dependencies(lock.resolution())
    for problem in conflicts.issues:
        evidence.append(
            CompatibilityEvidence(
                problem.code,
                CompatibilityState.CONFLICT if problem.blocking else CompatibilityState.UNKNOWN,
                "provider_constraints",
                problem.message,
            )
        )
    evidence.extend(
        CompatibilityEvidence(item.code, CompatibilityState.CONFLICT, "file_plan", item.message)
        for item in issues
    )
    if not conflicts.issues:
        evidence.append(
            CompatibilityEvidence(
                "declared_constraints",
                CompatibilityState.CONFIRMED,
                "provider_constraints",
                "Selected versions satisfy the supplied game/loader/dependency declarations only",
            )
        )
    if lock.context.game_version is None and lock.context.build_id is None:
        evidence.append(
            CompatibilityEvidence(
                "game_version_unknown",
                CompatibilityState.UNKNOWN,
                "game_identity",
                "No exact platform build or game version is known",
            )
        )
    else:
        evidence.append(
            CompatibilityEvidence(
                "game_identity",
                CompatibilityState.CONFIRMED,
                "game_identity",
                "Locked platform or launcher metadata identifies this build; "
                "it does not prove mod loading",
            )
        )
    for entry in lock.entries:
        strong = any(item.strong for item in entry.mod.file.hashes)
        evidence.append(
            CompatibilityEvidence(
                "artifact_hash",
                CompatibilityState.CONFIRMED if strong else CompatibilityState.UNKNOWN,
                "expected_artifact_identity",
                f"{entry.mod.project.reference.source_id}:{entry.mod.file.file_id}: "
                + (
                    "strong expected hash supplied"
                    if strong
                    else "no strong expected hash supplied"
                ),
            )
        )
    evidence.append(
        CompatibilityEvidence(
            "game_loading_unverified",
            CompatibilityState.UNKNOWN,
            "game_runtime",
            "The game has not been launched or observed loading these mods",
        )
    )
    relevant = [item for item in evidence if item.scope != "game_runtime"]
    state = (
        CompatibilityState.CONFLICT
        if any(item.state == CompatibilityState.CONFLICT for item in relevant)
        else CompatibilityState.UNKNOWN
        if any(item.state == CompatibilityState.UNKNOWN for item in relevant)
        else CompatibilityState.LIKELY
    )
    return CompatibilityReport(state, datetime.now(UTC).isoformat(), lock.digest, tuple(evidence))
