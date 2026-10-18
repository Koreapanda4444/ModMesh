from __future__ import annotations

import asyncio
from dataclasses import dataclass

from modmesh.core.artifact_matching import ArtifactCandidate, identical_candidates
from modmesh.core.diagnostics import ModMeshError
from modmesh.core.identity import IdentityRegistry
from modmesh.core.sources import SourceCapability, SourceRegistry, VersionQuery
from modmesh.domain.models import SourceRef


@dataclass(frozen=True)
class AlternativeFiles:
    candidates: tuple[ArtifactCandidate, ...] = ()
    notes: tuple[str, ...] = ()


async def find_alternatives(
    requested: ArtifactCandidate,
    registry: SourceRegistry,
    identities: IdentityRegistry,
) -> AlternativeFiles:
    if not requested.compatibility_verified:
        return AlternativeFiles(
            notes=("Cross-source fallback disabled: compatibility is unverified",)
        )
    canonical = identities.resolve(requested.project.game_id, requested.project.reference)
    if canonical is None:
        return AlternativeFiles()
    references = tuple(
        ref for ref in identities.projects(canonical) if ref != requested.project.reference
    )
    if len(references) > 16:
        return AlternativeFiles(
            notes=("Too many linked projects; cross-source fallback was disabled",)
        )
    semaphore = asyncio.Semaphore(4)

    async def lookup(reference: SourceRef) -> AlternativeFiles:
        async with semaphore:
            try:
                async with asyncio.timeout(20):
                    source = registry.get(reference.source_id)
                    source.require(requested.project.game_id, SourceCapability.VERSIONS)
                    project = await source.project(requested.project.game_id, reference.project_id)
                    if (
                        project.game_id != requested.project.game_id
                        or project.reference != reference
                    ):
                        return AlternativeFiles(
                            notes=(f"{reference.source_id}: project context did not match",)
                        )
                    query = VersionQuery(requested.game_version, requested.loader)
                    versions = await source.versions(
                        project.game_id,
                        reference.project_id,
                        query=query if source.version_filters else None,
                    )
                if len(versions) > 2000:
                    return AlternativeFiles(
                        notes=(
                            f"{reference.source_id}: version catalog exceeds the fallback limit",
                        )
                    )
                candidates = []
                examined = 0
                for version in versions:
                    if version.project != reference:
                        return AlternativeFiles(
                            notes=(f"{reference.source_id}: version identity did not match",)
                        )
                    for file in version.files:
                        examined += 1
                        if examined > 10000:
                            return AlternativeFiles(
                                notes=(f"{reference.source_id}: catalog limit reached",)
                            )
                        candidate = ArtifactCandidate(
                            project, version, file, requested.game_version, requested.loader
                        )
                        if identical_candidates(requested, candidate):
                            candidates.append(candidate)
                            if len(candidates) > 256:
                                return AlternativeFiles(
                                    notes=(f"{reference.source_id}: too many matching files",)
                                )
                if not candidates:
                    return AlternativeFiles(
                        notes=(f"{reference.source_id}: no proven file; fallback unavailable",)
                    )
                return AlternativeFiles(
                    tuple(candidates),
                    (f"{reference.source_id}: {len(candidates)} proven file(s)",),
                )
            except (ModMeshError, TimeoutError) as exc:
                message = exc.message if isinstance(exc, ModMeshError) else "lookup timed out"
                return AlternativeFiles(notes=(f"{reference.source_id}: {message}",))
            except Exception:
                return AlternativeFiles(
                    notes=(f"{reference.source_id}: linked source lookup failed",)
                )

    results = await asyncio.gather(*(lookup(reference) for reference in references))
    candidates = tuple(candidate for result in results for candidate in result.candidates)
    notes = tuple(note for result in results for note in result.notes)
    if len(candidates) > 1000:
        notes += ("Fallback candidates limited to the first 1000 verified files",)
    return AlternativeFiles(candidates[:1000], notes)
