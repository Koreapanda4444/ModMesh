import os
import subprocess
from contextlib import suppress

import psutil

from modmesh.core.diagnostics import ErrorCode, ModMeshError, redact_text
from modmesh.core.discovery import current_platform
from modmesh.core.launching import LaunchCall, LaunchCallState, LaunchMethod, LaunchPlan
from modmesh.core.planning import snapshot
from modmesh.domain.models import Platform


def verify_launch_files(plan: LaunchPlan) -> None:
    root = plan.root.stat(follow_symlinks=False)
    if (
        plan.root.is_symlink()
        or plan.root.is_junction()
        or not plan.root.is_dir()
        or (root.st_dev, root.st_ino) != (plan.root_device, plan.root_inode)
    ):
        raise ModMeshError(ErrorCode.CONFLICT, "Game root identity changed after launch approval")
    if plan.executable is not None:
        if plan.executable_proof is None or snapshot(plan.executable) != plan.executable_proof:
            raise ModMeshError(ErrorCode.CONFLICT, "Launch executable changed after approval")
        if plan.method == LaunchMethod.EXECUTABLE and not plan.executable.is_relative_to(plan.root):
            raise ModMeshError(ErrorCode.CONFLICT, "Game executable escapes the approved game root")


class NativeLaunchBoundary:
    def invoke(self, plan: LaunchPlan) -> LaunchCall:
        if plan.method == LaunchMethod.MANUAL:
            return LaunchCall(LaunchCallState.MANUAL, detail=plan.instruction)
        if plan.platform != current_platform():
            raise ModMeshError(
                ErrorCode.UNSUPPORTED, "Cannot launch a game for another operating system"
            )
        verify_launch_files(plan)
        try:
            if plan.method == LaunchMethod.STEAM:
                assert plan.uri is not None
                if plan.platform == Platform.WINDOWS:
                    startfile = getattr(os, "startfile", None)
                    if startfile is None:
                        raise ModMeshError(
                            ErrorCode.UNSUPPORTED, "Windows URI handler is unavailable"
                        )
                    startfile(plan.uri, "open")
                    return LaunchCall(
                        LaunchCallState.ACCEPTED,
                        detail="Steam URI accepted by the OS handler; game loading is unverified",
                    )
                helper = "/usr/bin/open" if plan.platform == Platform.MACOS else "/usr/bin/xdg-open"
                child = subprocess.Popen(
                    [helper, plan.uri],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
                try:
                    code = child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    return LaunchCall(
                        LaunchCallState.UNCERTAIN,
                        detail="OS URI handler has not returned; "
                        "inspect the existing Steam client before retrying",
                    )
                return LaunchCall(
                    LaunchCallState.ACCEPTED if code == 0 else LaunchCallState.UNCERTAIN,
                    detail=f"OS URI handler returned {code}; "
                    "this is not game or mod loading evidence",
                )
            assert plan.executable is not None
            child = subprocess.Popen(
                [str(plan.executable), *plan.arguments],
                cwd=plan.root,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=os.name != "nt",
            )
            exit_code = child.poll()
            if exit_code is not None and exit_code != 0:
                return LaunchCall(
                    LaunchCallState.UNCERTAIN,
                    detail=f"Launch process exited with code {exit_code}; "
                    "verify game state before retrying",
                )
            started = None
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                started = psutil.Process(child.pid).create_time()
            return LaunchCall(
                LaunchCallState.ACCEPTED,
                child.pid,
                started,
                "OS process creation succeeded; game and mod loading are unverified",
            )
        except OSError as exc:
            return LaunchCall(LaunchCallState.FAILED, detail=redact_text(str(exc)))
