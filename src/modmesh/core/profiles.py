from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from modmesh.core.diagnostics import ErrorCode, ModMeshError
from modmesh.core.downloads import publish_file
from modmesh.core.games import GameCapability
from modmesh.core.installation import (
    OperationProgress,
    OperationResult,
    _check_cancelled,
    _copy,
    _sync_dir,
)
from modmesh.core.planning import (
    FileSnapshot,
    InstallationPlan,
    InstallInput,
    PlanIssue,
    portable_path,
    snapshot,
    target_path,
)
from modmesh.core.removal import RemovalManager
from modmesh.core.resolver import DependencyResolution
from modmesh.domain.artifacts import FileHash, HashAlgorithm
from modmesh.domain.dependencies import DependencyKind
from modmesh.storage.installed import (
    ManagedMod,
    array_record,
    encode_mod,
    integer_record,
    mod_key,
    object_record,
    string_record,
)
from modmesh.storage.metadata import MetadataStore


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            default=lambda item: sorted(item) if isinstance(item, (set, frozenset)) else str(item),
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ProfileFile:
    path: str
    sha256: str
    size: int
    owners: tuple[str, ...]
    original_sha256: str | None = None
    setting: bool = False

    @classmethod
    def read(cls, value: object) -> ProfileFile:
        data = object_record(value)
        sha = FileHash(HashAlgorithm.SHA256, string_record(data.get("sha256"))).value
        original = data.get("original_sha256")
        if original is not None:
            original = FileHash(HashAlgorithm.SHA256, string_record(original)).value
        return cls(
            portable_path(string_record(data.get("path"))),
            sha,
            integer_record(data.get("size")),
            tuple(string_record(item) for item in array_record(data.get("owners", []))),
            original,
            data.get("setting") is True,
        )


@dataclass(frozen=True)
class Profile:
    profile_id: str
    name: str
    context: dict[str, object]
    mods: tuple[ManagedMod, ...] = ()
    files: tuple[ProfileFile, ...] = ()

    @classmethod
    def read(cls, value: object) -> Profile:
        data = object_record(value)
        identity = string_record(data.get("profile_id"))
        name = string_record(data.get("name"))
        if len(name) > 100 or len(identity) > 100:
            raise ModMeshError(ErrorCode.STORAGE, "Invalid profile identifier or name")
        files = tuple(ProfileFile.read(item) for item in array_record(data.get("files", [])))
        mods = tuple(ManagedMod.read(item) for item in array_record(data.get("mods", [])))
        if len(files) > 10000 or len({item.path.casefold() for item in files}) != len(files):
            raise ModMeshError(ErrorCode.STORAGE, "Invalid profile file set")
        if len({mod_key(item.mod.project.reference) for item in mods}) != len(mods):
            raise ModMeshError(ErrorCode.STORAGE, "Duplicate profile mod")
        return cls(identity, name, object_record(data.get("context")), mods, files)


def profile_record(profile: Profile) -> dict[str, object]:
    return {
        "profile_id": profile.profile_id,
        "name": profile.name,
        "context": profile.context,
        "files": [asdict(item) for item in profile.files],
        "mods": [
            {
                "mod": encode_mod(item.mod),
                "paths": item.paths,
                "dependencies": [asdict(ref) for ref in item.dependencies],
                "operation_id": item.operation_id,
                "rule_id": item.rule_id,
                "prefix": item.prefix,
            }
            for item in profile.mods
        ],
    }


@dataclass(frozen=True)
class ProfileChange:
    path: str
    action: str
    before: FileSnapshot | None
    desired: ProfileFile | None
    content_sha256: str | None
    size: int


@dataclass(frozen=True)
class ProfilePlan:
    plan_id: str
    target: Profile
    previous: Profile
    active_id: str | None
    root_device: int
    root_inode: int
    changes: tuple[ProfileChange, ...]
    issues: tuple[PlanIssue, ...]
    warnings: tuple[str, ...]

    @property
    def applicable(self) -> bool:
        return not self.issues

    @property
    def fingerprint(self) -> str:
        data = asdict(self)
        data.pop("plan_id")
        return _digest(data)


class ProfileManager(RemovalManager):
    def context(self) -> dict[str, object]:
        item = self.installation
        return {
            "game_id": item.game_id,
            "edition": item.edition,
            "platform": item.platform.value,
            "version": asdict(item.version),
            "loader": asdict(item.loader) if item.loader else None,
        }

    def _ready(self, store: MetadataStore) -> None:
        if any(
            value.get("state") not in {"committed", "rolled_back", "failed"}
            for _, value in store.list("install.operations")
        ):
            raise ModMeshError(ErrorCode.CONFLICT, "Recover incomplete operations first")
        adapter = self.games.get(self.installation.game_id)
        if GameCapability.PROFILES not in adapter.capabilities:
            raise ModMeshError(ErrorCode.UNSUPPORTED, "Game has no verified profile strategy")
        actual = adapter.inspect(self.root, self.installation.platform)
        if (
            actual is None
            or actual.version != self.installation.version
            or actual.loader != self.installation.loader
        ):
            raise ModMeshError(ErrorCode.CONFLICT, "Game or loader changed; rediscover it first")
        for report in (
            adapter.validate(self.installation),
            adapter.validate_operation(self.installation),
        ):
            if not report.valid:
                raise ModMeshError(
                    ErrorCode.CONFLICT,
                    "; ".join(item.message for item in report.issues if item.blocking),
                )

    def _blob(self, sha: str) -> Path:
        FileHash(HashAlgorithm.SHA256, sha)
        return target_path(self.state, "profiles/blobs/" + sha)

    def _save_blob(self, source: Path) -> FileSnapshot:
        proof = snapshot(source)
        directory = target_path(self.state, "profiles/blobs")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = self._blob(proof.sha256)
        if destination.exists():
            if snapshot(destination).sha256 != proof.sha256:
                raise ModMeshError(ErrorCode.INTEGRITY, "Profile content was modified")
        else:
            with source.open("rb") as stream:
                _copy(stream, destination, proof.size, proof.sha256)
        return proof

    def _active(self, store: MetadataStore) -> str | None:
        value = store.get("profiles.state", "active")
        return string_record(value["id"]) if value else None

    def active_id(self) -> str | None:
        with self._locked() as store:
            return self._active(store) if store else None

    def profiles(self) -> tuple[Profile, ...]:
        with self._locked() as store:
            return (
                tuple(Profile.read(value) for _, value in store.list("profiles.items"))
                if store
                else ()
            )

    def _get(self, store: MetadataStore, identity: str) -> Profile:
        value = store.get("profiles.items", identity)
        if value is None:
            raise ModMeshError(ErrorCode.NOT_FOUND, "Profile does not exist")
        profile = Profile.read(value)
        if profile.profile_id != identity:
            raise ModMeshError(ErrorCode.STORAGE, "Profile identity mismatch")
        return profile

    def _capture(
        self, store: MetadataStore, identity: str, name: str, settings: tuple[str, ...] = ()
    ) -> Profile:
        files = []
        for record in self._file_records(store).values():
            if record["managed"] is not True:
                raise ModMeshError(
                    ErrorCode.CONFLICT,
                    "Externally owned reused files cannot be isolated into a profile",
                )
            path = string_record(record["path"])
            source = target_path(self.root, path)
            proof = snapshot(source)
            if proof != FileSnapshot(
                string_record(record["sha256"]),
                integer_record(record["size"]),
                integer_record(record["device"]),
                integer_record(record["inode"]),
            ):
                raise ModMeshError(
                    ErrorCode.CONFLICT, "Modified or replaced managed file was preserved: " + path
                )
            self._save_blob(source)
            original = None
            if record.get("backup") is not None:
                reference, baseline = self._baseline(store, string_record(record["backup"]))
                parts = reference.split("/")
                self._save_blob(
                    self._slot(self._operation_path(parts[0]), "backups", int(parts[2]))
                )
                original = baseline.sha256
                if original != record.get("original_sha256"):
                    raise ModMeshError(ErrorCode.INTEGRITY, "Original profile baseline differs")
            files.append(
                ProfileFile(
                    path,
                    proof.sha256,
                    proof.size,
                    tuple(string_record(owner) for owner in array_record(record["owners"])),
                    original,
                )
            )
        allowed = self.games.get(self.installation.game_id).profile_settings(self.installation)
        for path in settings:
            if path not in allowed or any(
                item.path.casefold() == path.casefold() for item in files
            ):
                raise ModMeshError(ErrorCode.VALIDATION, "Setting path is not explicitly allowed")
            proof = self._save_blob(target_path(self.root, path))
            files.append(ProfileFile(path, proof.sha256, proof.size, (), setting=True))
        mods = tuple(ManagedMod.read(value) for _, value in store.list("install.mods"))
        return Profile(identity, name, self.context(), mods, tuple(files))

    def create(
        self,
        name: str,
        *,
        capture: bool = True,
        clone: str | None = None,
        settings: tuple[str, ...] = (),
    ) -> Profile:
        if not name.strip() or len(name) > 100 or any(ord(char) < 32 for char in name):
            raise ModMeshError(
                ErrorCode.VALIDATION, "Profile name needs 1–100 printable characters"
            )
        with self._locked(create=True) as store:
            assert store is not None
            self._ready(store)
            identity = str(uuid4())
            active = self._active(store)
            if active is None:
                baseline = self._capture(store, str(uuid4()), "기존 구성")
                store.put("profiles.items", baseline.profile_id, profile_record(baseline))
                store.put("profiles.state", "active", {"id": baseline.profile_id})
            if clone:
                original = self._get(store, clone)
                if clone == active:
                    original = self._capture(
                        store,
                        clone,
                        original.name,
                        tuple(item.path for item in original.files if item.setting),
                    )
                profile = replace(original, profile_id=identity, name=name.strip())
            else:
                profile = (
                    self._capture(store, identity, name.strip(), settings)
                    if capture
                    else Profile(identity, name.strip(), self.context())
                )
            store.put("profiles.items", identity, profile_record(profile))
            return profile

    def rename(self, identity: str, name: str) -> None:
        if not name.strip() or len(name) > 100 or any(ord(char) < 32 for char in name):
            raise ModMeshError(ErrorCode.VALIDATION, "Invalid profile name")
        with self._locked(create=True) as store:
            assert store is not None
            profile = replace(self._get(store, identity), name=name.strip())
            store.put("profiles.items", identity, profile_record(profile))

    def delete(self, identity: str) -> None:
        with self._locked(create=True) as store:
            assert store is not None
            self._ready(store)
            self._get(store, identity)
            if identity == self._active(store):
                raise ModMeshError(
                    ErrorCode.CONFLICT, "Switch away before deleting the active profile"
                )
            with store.transaction(write=True) as transaction:
                transaction.delete("profiles.items", identity)

    def capture_active(self) -> Profile:
        with self._locked(create=True) as store:
            assert store is not None
            self._ready(store)
            identity = self._active(store)
            if identity is None:
                raise ModMeshError(ErrorCode.NOT_FOUND, "Create a profile first")
            current = self._get(store, identity)
            value = self._capture(
                store,
                identity,
                current.name,
                tuple(item.path for item in current.files if item.setting),
            )
            store.put("profiles.items", identity, profile_record(value))
            return value

    def _switch_plan(self, store: MetadataStore, identity: str) -> ProfilePlan:
        self._ready(store)
        target = self._get(store, identity)
        active = self._active(store)
        if active is None:
            raise ModMeshError(ErrorCode.STORAGE, "Active profile is missing")
        old = self._get(store, active)
        previous = self._capture(
            store, active, old.name, tuple(item.path for item in old.files if item.setting)
        )
        issues = []
        if target.context != self.context():
            issues.append(
                PlanIssue("profile_environment", "Profile needs a different game version or loader")
            )
        if identity == active:
            issues.append(PlanIssue("profile_active", "The selected profile is already active"))
        current = {item.path.casefold(): item for item in previous.files}
        desired = {item.path.casefold(): item for item in target.files}
        records = self._file_records(store)
        changes = []
        adapter = self.games.get(self.installation.game_id)
        locations = tuple(
            location.path.relative_to(self.root).as_posix() + "/"
            for location in adapter.mod_locations(self.installation)
        )
        for key in sorted(current.keys() | desired.keys()):
            before_item, after_item = current.get(key), desired.get(key)
            item = after_item or before_item
            assert item is not None
            path = item.path
            if item.setting:
                if path not in adapter.profile_settings(self.installation):
                    issues.append(PlanIssue("setting_scope", "Unsupported setting path", path))
            elif not any(path.startswith(prefix) for prefix in locations):
                issues.append(
                    PlanIssue("profile_scope", "Profile file escapes mod locations", path)
                )
            destination = target_path(self.root, path)
            before = snapshot(destination) if destination.exists() else None
            content = (
                after_item.sha256
                if after_item
                else (before_item.original_sha256 if before_item else None)
            )
            if before_item and before_item.setting and after_item is None:
                content = before.sha256 if before else None
            if (
                before_item is None
                and before
                and after_item
                and not after_item.setting
                and before.sha256 != after_item.original_sha256
            ):
                issues.append(
                    PlanIssue("external_file", "An external file blocks this profile", path)
                )
            if (
                before_item
                and after_item
                and not item.setting
                and before_item.original_sha256 != after_item.original_sha256
            ):
                issues.append(
                    PlanIssue(
                        "profile_baseline", "Profiles have different original file baselines", path
                    )
                )
            size = snapshot(self._blob(content)).size if content else 0
            if content and snapshot(self._blob(content)).sha256 != content:
                issues.append(PlanIssue("profile_integrity", "Profile blob changed", path))
            action = (
                "reuse"
                if before and before.sha256 == content
                else ("replace" if before else "create")
            )
            if content is None:
                action = "delete" if before else "reuse"
            changes.append(ProfileChange(path, action, before, after_item, content, size))
        for mod in target.mods:
            if mod.mod.project.game_id != self.installation.game_id:
                issues.append(PlanIssue("profile_game", "Profile contains a mod for another game"))
            for path in mod.paths:
                item = desired.get(path)
                if item is None or mod_key(mod.mod.project.reference) not in item.owners:
                    issues.append(
                        PlanIssue("profile_ownership", "Profile ownership is incomplete", path)
                    )
        native = adapter.validate_profile(
            self.installation,
            tuple((item.path, self._blob(item.sha256)) for item in target.files if not item.setting)
            + tuple(
                (change.path, self._blob(change.content_sha256))
                for change in changes
                if change.desired is None
                and change.content_sha256 is not None
                and not current[change.path.casefold()].setting
            ),
            tuple(
                dict.fromkeys(
                    (
                        *[string_record(value["path"]) for value in records.values()],
                        *[item.path for item in target.files if not item.setting],
                    )
                )
            ),
        )
        issues.extend(PlanIssue(item.code, item.message) for item in native.issues if item.blocking)
        root = self.root.stat(follow_symlinks=False)
        warnings = (
            "External mods are shared; saves and unselected settings are preserved.",
            *(item.message for item in native.issues if not item.blocking),
        )
        return ProfilePlan(
            str(uuid4()),
            target,
            previous,
            active,
            root.st_dev,
            root.st_ino,
            tuple(changes),
            tuple(issues),
            tuple(warnings),
        )

    def payload_plan(
        self,
        resolution: DependencyResolution,
        inputs: tuple[InstallInput, ...],
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> InstallationPlan:
        with self._locked(create=True, cancelled=cancelled) as store:
            assert store is not None
            self._ready(store)
            self._capture(store, "validation", "validation")
            files = self._file_records(store)
            return self.planner.build(
                self.installation,
                resolution,
                inputs,
                managed={
                    string_record(value["path"]): FileHash(
                        HashAlgorithm.SHA256, string_record(value["sha256"])
                    )
                    for value in files.values()
                },
                replacing=frozenset(string_record(value["path"]) for value in files.values()),
                cancelled=cancelled,
            )

    def stage_payload(self, payload: InstallationPlan, name: str) -> ProfilePlan:
        if payload.issues or len(name) > 100 or not name.strip():
            raise ModMeshError(ErrorCode.CONFLICT, "Payload needs an applicable verified plan")
        with self._locked(create=True) as store:
            assert store is not None
            self._ready(store)
            current_files = self._file_records(store)
            check = self.planner.build(
                self.installation,
                payload.resolution,
                payload.inputs,
                managed={
                    string_record(value["path"]): FileHash(
                        HashAlgorithm.SHA256, string_record(value["sha256"])
                    )
                    for value in current_files.values()
                },
                replacing=frozenset(
                    string_record(value["path"]) for value in current_files.values()
                ),
            )
            if check.fingerprint != payload.fingerprint or check.issues:
                raise ModMeshError(ErrorCode.CONFLICT, "Payload changed before profile preparation")
            if self._active(store) is None:
                baseline = self._capture(store, str(uuid4()), "기존 구성")
                store.put("profiles.items", baseline.profile_id, profile_record(baseline))
                store.put("profiles.state", "active", {"id": baseline.profile_id})
            files = []
            with TemporaryDirectory(prefix="modmesh-profile-payload-") as directory:
                for index, change in enumerate(payload.changes):
                    stage = Path(directory) / str(index)
                    if change.generated is not None:
                        with io.BytesIO(change.generated) as stream:
                            _copy(stream, stage, change.size, change.sha256)
                    elif change.member is None:
                        with change.source.open("rb") as stream:
                            _copy(stream, stage, change.size, change.sha256)
                    else:
                        with (
                            zipfile.ZipFile(change.source) as archive,
                            archive.open(change.member) as stream,
                        ):
                            _copy(stream, stage, change.size, change.sha256)
                    self._save_blob(stage)
                    previous = current_files.get(change.destination.casefold(), {})
                    original = previous.get("original_sha256")
                    files.append(
                        ProfileFile(
                            change.destination,
                            change.sha256,
                            change.size,
                            (mod_key(change.project),),
                            string_record(original) if original else None,
                        )
                    )
            mods = []
            for mod in payload.resolution.mods:
                recipe = next(item for item in payload.inputs if item.mod == mod)
                mods.append(
                    ManagedMod(
                        mod,
                        tuple(
                            change.destination.casefold()
                            for change in payload.changes
                            if change.project == mod.project.reference
                        ),
                        tuple(
                            dict.fromkeys(
                                edge.target
                                for edge in payload.resolution.edges
                                if edge.parent == mod.project.reference
                                and edge.declaration.kind == DependencyKind.REQUIRED
                            )
                        ),
                        payload.plan_id,
                        recipe.rule_id,
                        recipe.prefix,
                    )
                )
            profile = Profile(str(uuid4()), name.strip(), self.context(), tuple(mods), tuple(files))
            store.put("profiles.items", profile.profile_id, profile_record(profile))
            return self._switch_plan(store, profile.profile_id)

    def plan_switch(self, identity: str) -> ProfilePlan:
        with self._locked(create=True) as store:
            assert store is not None
            return self._switch_plan(store, identity)

    def switch(
        self,
        plan: ProfilePlan,
        approval: str,
        *,
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[[OperationProgress], None] | None = None,
    ) -> OperationResult:
        if not plan.applicable or approval != plan.fingerprint:
            raise ModMeshError(ErrorCode.CONFLICT, "Approve the exact applicable profile plan")
        with self._locked(create=True, cancelled=cancelled) as store:
            assert store is not None
            current = self._switch_plan(store, plan.target.profile_id)
            if current.fingerprint != approval or not current.applicable:
                raise ModMeshError(ErrorCode.CONFLICT, "Profile plan changed; review it again")
            operation_id = plan.plan_id
            operation = self._operation_path(operation_id)
            operation.mkdir(mode=0o700)
            for name in ("staging", "backups", "moved", "withdrawn"):
                (operation / name).mkdir(mode=0o700)
            identity = operation.stat(follow_symlinks=False)
            files = self._file_records(store)
            old_mods = dict(store.list("install.mods"))
            desired_mods = {
                mod_key(mod.mod.project.reference): object_record(value)
                for mod, value in zip(
                    plan.target.mods, array_record(profile_record(plan.target)["mods"]), strict=True
                )
            }
            record: dict[str, object] = {
                "id": operation_id,
                "kind": "profile",
                "state": "preparing",
                "device": identity.st_dev,
                "inode": identity.st_ino,
                "root_device": plan.root_device,
                "root_inode": plan.root_inode,
                "game_id": self.installation.game_id,
                "old_files": {
                    change.path.casefold(): files.get(change.path.casefold())
                    for change in plan.changes
                },
                "old_mods": {
                    key: old_mods.get(key) for key in old_mods.keys() | desired_mods.keys()
                },
                "steps": [],
                "created_dirs": [],
                "issues": [],
            }
            store.put("install.operations", operation_id, record)
            try:
                steps: list[dict[str, object]] = []
                for index, change in enumerate(plan.changes):
                    _check_cancelled(cancelled)
                    backup = stage = None
                    if change.before and change.action != "reuse":
                        with target_path(self.root, change.path).open("rb") as stream:
                            _copy(
                                stream,
                                self._slot(operation, "backups", index),
                                change.before.size,
                                change.before.sha256,
                                cancelled,
                            )
                        backup = asdict(snapshot(self._slot(operation, "backups", index)))
                    if change.content_sha256 and change.action != "reuse":
                        with self._blob(change.content_sha256).open("rb") as stream:
                            _copy(
                                stream,
                                self._slot(operation, "staging", index),
                                change.size,
                                change.content_sha256,
                                cancelled,
                            )
                        stage = asdict(snapshot(self._slot(operation, "staging", index)))
                    steps.append(
                        {
                            "path": change.path,
                            "action": change.action,
                            "before": asdict(change.before) if change.before else None,
                            "stage": stage,
                            "backup": backup,
                            "state": "prepared",
                        }
                    )
                    record["steps"] = steps
                    store.put("install.operations", operation_id, record)
                record["state"] = "applying"
                store.put("install.operations", operation_id, record)
                new_files = {}
                for index, (change, step) in enumerate(zip(plan.changes, steps, strict=True)):
                    _check_cancelled(cancelled)
                    self._parents(change.path, record, store)
                    path = target_path(self.root, change.path)
                    before = snapshot(path) if path.exists() else None
                    if before != change.before:
                        raise ModMeshError(
                            ErrorCode.CONFLICT, "Profile target changed after approval"
                        )
                    step["state"] = "publishing"
                    store.put("install.operations", operation_id, record)
                    if change.action != "reuse":
                        if before:
                            os.rename(path, self._slot(operation, "moved", index))
                            if snapshot(self._slot(operation, "moved", index)) != before:
                                raise ModMeshError(
                                    ErrorCode.CONFLICT, "Profile file changed during transfer"
                                )
                        if change.content_sha256:
                            publish_file(self._slot(operation, "staging", index), path)
                        _sync_dir(path.parent)
                    if change.desired and not change.desired.setting:
                        proof = snapshot(path)
                        if proof.sha256 != change.desired.sha256:
                            raise ModMeshError(
                                ErrorCode.INTEGRITY, "Profile file changed during apply"
                            )
                        previous = files.get(change.path.casefold(), {})
                        baseline = previous.get("backup")
                        if baseline is None and before and change.desired.original_sha256:
                            baseline = f"{operation_id}/backups/{index}"
                        new_files[change.path.casefold()] = {
                            "path": change.path,
                            **asdict(proof),
                            "managed": True,
                            "owners": change.desired.owners,
                            "backup": baseline,
                            "original_sha256": change.desired.original_sha256,
                        }
                    step["state"] = "applied"
                    store.put("install.operations", operation_id, record)
                    self._emit(
                        progress, operation_id, "applying", index + 1, len(steps), change.path
                    )
                _check_cancelled(cancelled)
                with store.transaction(write=True) as transaction:
                    for key in object_record(record["old_files"]):
                        transaction.delete("install.files", key)
                    for key, value in new_files.items():
                        transaction.put("install.files", key, value)
                    for key in old_mods:
                        transaction.delete("install.mods", key)
                    for key, value in desired_mods.items():
                        value["operation_id"] = operation_id
                        transaction.put("install.mods", key, value)
                    transaction.put(
                        "profiles.items", plan.previous.profile_id, profile_record(plan.previous)
                    )
                    transaction.put("profiles.state", "active", {"id": plan.target.profile_id})
                    record["state"] = "committed"
                    transaction.put("install.operations", operation_id, record)
                self._cleanup(store, record)
                return OperationResult(operation_id, "committed")
            except Exception as exc:
                result = self._rollback(store, record)
                raise ModMeshError(
                    exc.code if isinstance(exc, ModMeshError) else ErrorCode.STORAGE,
                    str(exc),
                    details={"recovery": result.state, "operation_id": operation_id},
                ) from exc
