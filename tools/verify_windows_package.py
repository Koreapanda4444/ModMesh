from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from modmesh import __version__

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(output):
    if sys.platform != "win32":
        raise SystemExit("Run package verification on Windows")
    checksums = {}
    for line in (output / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        expected, name = line.split("  ", 1)
        if Path(name).name != name or digest(output / name) != expected:
            raise RuntimeError(f"Package checksum mismatch: {name}")
        checksums[name] = expected
    portable = output / f"ModMesh-{__version__}-windows-x64-portable.zip"
    setup = output / f"ModMesh-{__version__}-windows-x64-setup.exe"
    sources = output / f"ModMesh-{__version__}-third-party-sources.zip"
    if set(checksums) != {portable.name, setup.name, sources.name}:
        raise RuntimeError("The release must include both packages and corresponding sources")
    runs = []
    with tempfile.TemporaryDirectory(prefix="modmesh-package-") as temporary:
        root = Path(temporary) / "한글 경로 with spaces"
        root.mkdir()
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith(("PYTHON", "MODMESH", "QT_", "QML"))
        }
        windows = Path(os.environ["SYSTEMROOT"])
        environment.update(
            {
                "PATH": os.pathsep.join(str(windows / part) for part in ("System32", "")),
                "APPDATA": str(root / "Roaming"),
                "LOCALAPPDATA": str(root / "Local"),
            }
        )
        sentinels = {
            root / "Roaming/ModMesh/config.toml": b"[client]\ncache_max_bytes = 104857600\n",
            root / "Local/ModMesh/user-data.bin": b"existing user data",
            root / "Local/ModMesh/cache/unknown.bin": b"existing cache file",
            root / "game/save.dat": b"existing game save",
            root / "game/mods/external.pak": b"existing external mod",
        }
        for path, content in sentinels.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        unpacked = root / "portable"
        with zipfile.ZipFile(portable) as archive:
            names = archive.namelist()
            if any(
                Path(name).is_absolute()
                or ".." in Path(name).parts
                or Path(name).name.lower() == ".env"
                or name.endswith((".sqlite3", ".log"))
                for name in names
            ):
                raise RuntimeError("Unexpected private or unsafe content in portable ZIP")
            archive.extractall(unpacked)
        bundle = unpacked / "ModMesh"
        for name in ("ModMesh.exe", "THIRD_PARTY_NOTICES.txt", "runtime-packages.json"):
            if not (bundle / name).is_file():
                raise RuntimeError(f"Missing bundle file: {name}")
        if not list((bundle / "licenses").rglob("LGPL-3.0-only.txt")):
            raise RuntimeError("Qt license texts are missing")
        with zipfile.ZipFile(sources) as archive:
            records = json.loads(archive.read("sources.json"))
            if len(records) != 4:
                raise RuntimeError("Corresponding source archives are incomplete")
            for record in records:
                with archive.open(record["file"]) as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                if actual != record["sha256"]:
                    raise RuntimeError("Corresponding source checksum mismatch")

        def exercise(executable, label):
            report = root / f"{label}.json"
            subprocess.run(
                [str(executable), "--check", "--check-report", str(report)],
                cwd=root,
                env=environment,
                check=True,
                timeout=60,
            )
            result = json.loads(report.read_text(encoding="utf-8"))
            if (
                result["version"] != __version__
                or result["platform"] != "windows"
                or not result["packaged"]
                or not result["discovery"]
            ):
                raise RuntimeError(f"Frozen native Qt check failed: {result}")
            runs.append({"stage": label, **result})

        exercise(bundle / "ModMesh.exe", "portable-first")
        exercise(bundle / "ModMesh.exe", "portable-restart")
        install = root / "installed"
        arguments = [
            str(setup),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            f"/DIR={install}",
        ]
        subprocess.run(arguments, env=environment, check=True, timeout=120)
        installed = install / "versions" / __version__ / "ModMesh.exe"
        exercise(installed, "installed-first")
        exercise(installed, "installed-restart")
        unknown = installed.parent / "user-owned.txt"
        unknown.write_bytes(b"not owned by installer")
        sentinels[unknown] = unknown.read_bytes()
        subprocess.run(arguments, env=environment, check=True, timeout=120)
        exercise(installed, "reinstalled")
        subprocess.run(
            [str(install / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            env=environment,
            check=True,
            timeout=120,
        )
        if installed.exists():
            raise RuntimeError("Uninstall did not remove the installed executable")
        for path, content in sentinels.items():
            if not path.is_file() or path.read_bytes() != content:
                raise RuntimeError(f"User content changed during package verification: {path.name}")
    result = {
        "version": __version__,
        "checksums": checksums,
        "runs": runs,
        "uninstall": True,
        "user_content_preserved": True,
        "scope": "Windows CI, native Qt, same-version reinstall; real games remain unverified",
    }
    (output / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist/windows")
    verify(parser.parse_args().output.resolve())
