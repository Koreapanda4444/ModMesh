from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import psutil

from modmesh.core.games import EnvironmentIssue, EnvironmentReport


def process_guard(
    names: frozenset[str], matches: Callable[[psutil.Process], bool]
) -> EnvironmentReport:
    issues = []
    for process in psutil.process_iter():
        try:
            if process.pid == os.getpid() or process.name().casefold() not in names:
                continue
            if matches(process):
                issues.append(
                    EnvironmentIssue(
                        "game_running",
                        "The selected game instance is running; close it before changing files",
                        True,
                    )
                )
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            try:
                relevant = process.name().casefold() in names
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                relevant = False
            if relevant:
                issues.append(
                    EnvironmentIssue(
                        "process_unreadable",
                        "A game process cannot be inspected; close it before changing files",
                        True,
                    )
                )
    return EnvironmentReport(tuple(dict.fromkeys(issues)))


def executable_guard(path: Path) -> EnvironmentReport:
    expected = path.resolve()
    return process_guard(
        frozenset({path.name.casefold()}), lambda process: Path(process.exe()).resolve() == expected
    )


def java_guard(root: Path) -> EnvironmentReport:
    expected = root.resolve()

    def matches(process: psutil.Process) -> bool:
        arguments = process.cmdline()
        working = Path(process.cwd())

        def directory(value: str) -> Path:
            path = Path(value)
            return (path if path.is_absolute() else working / path).resolve()

        for index, argument in enumerate(arguments):
            if argument == "--gameDir" and index + 1 < len(arguments):
                return directory(arguments[index + 1]) == expected
            if argument.startswith("--gameDir="):
                return directory(argument.split("=", 1)[1]) == expected
        return working.resolve() == expected

    return process_guard(frozenset({"java", "java.exe", "javaw.exe", "javaw"}), matches)
