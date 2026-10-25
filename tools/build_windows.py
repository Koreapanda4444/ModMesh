from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from packaging.requirements import Requirement

from modmesh import __version__

ROOT = Path(__file__).resolve().parents[1]
QT_VERSION = "6.12.0"


def runtime_distributions():
    pending = [("modmesh", {"desktop"})]
    seen = set()
    result = {}
    while pending:
        name, extras = pending.pop()
        key = (name.lower().replace("_", "-"), tuple(sorted(extras)))
        if key in seen:
            continue
        seen.add(key)
        distribution = metadata.distribution(name)
        result[distribution.metadata["Name"]] = distribution
        for declaration in distribution.requires or []:
            requirement = Requirement(declaration)
            if requirement.marker is None or any(
                requirement.marker.evaluate({"extra": extra}) for extra in extras | {""}
            ):
                pending.append((requirement.name, set(requirement.extras)))
    return result


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sources(output, notices):
    base = f"https://download.qt.io/official_releases/qt/6.12/{QT_VERSION}/submodules/"
    urls = [
        base + f"{module}-everywhere-src-{QT_VERSION}.tar.xz"
        for module in ("qtbase", "qtsvg", "qtimageformats")
    ]
    urls.append(
        "https://download.qt.io/official_releases/QtForPython/pyside6/"
        f"PySide6-{QT_VERSION}-src/pyside-setup-everywhere-src-{QT_VERSION}.tar.xz"
    )
    records = []
    output.mkdir(parents=True, exist_ok=True)
    for url in urls:
        archive = output / url.rsplit("/", 1)[1]
        request = urllib.request.Request(url, headers={"User-Agent": "ModMesh-release-build"})
        with urllib.request.urlopen(request, timeout=120) as response, archive.open("xb") as target:
            size = 0
            while chunk := response.read(1024**2):
                size += len(chunk)
                if size > 256 * 1024**2:
                    raise RuntimeError("Qt source archive exceeds download limit")
                target.write(chunk)
        with tarfile.open(archive, "r:xz") as package:
            for member in package:
                relative = Path(*Path(member.name).parts[1:])
                if not relative.parts or ".." in relative.parts or relative.is_absolute():
                    continue
                name = relative.name.lower()
                legal = (
                    "LICENSES" in relative.parts
                    or name.startswith(("license", "copying", "notice"))
                    or name == "qt_attribution.json"
                )
                if member.isfile() and legal and member.size <= 2 * 1024**2:
                    stream = package.extractfile(member)
                    if stream is not None:
                        destination = notices / archive.name.removesuffix(".tar.xz") / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(stream.read())
        records.append({"url": url, "file": archive.name, "sha256": digest(archive)})
    (output / "sources.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    return records


def license_files(bundle, source_folder):
    directory = bundle / "licenses"
    directory.mkdir(parents=True)
    packages = []
    for name, distribution in sorted(runtime_distributions().items()):
        if name.lower() == "modmesh":
            continue
        packages.append(
            {
                "name": name,
                "version": distribution.version,
                "license": distribution.metadata.get("License-Expression")
                or distribution.metadata.get("License", "See included license files"),
            }
        )
        for file in distribution.files or []:
            path = Path(str(file))
            if path.is_absolute() or ".." in path.parts:
                continue
            if any(
                part.lower() == "licenses" for part in path.parts
            ) or path.name.lower().startswith(("license", "copying", "notice")):
                original = Path(distribution.locate_file(file))
                if original.is_file():
                    destination = directory / name / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(original, destination)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("Python distribution license was not found")
    shutil.copy2(python_license, directory / "Python-LICENSE.txt")
    installer = metadata.distribution("pyinstaller")
    bootloader_license = next(
        (
            Path(installer.locate_file(file))
            for file in installer.files or []
            if Path(str(file)).name == "COPYING.txt"
        ),
        Path("missing-pyinstaller-license"),
    )
    if not bootloader_license.is_file():
        raise RuntimeError("PyInstaller bootloader license was not found")
    shutil.copy2(bootloader_license, directory / "PyInstaller-COPYING.txt")
    source_records = sources(source_folder, directory / "Qt")
    lines = [
        "ModMesh third-party notices",
        "",
        "Qt, PySide6 and shiboken6 are used under LGPL-3.0-only.",
        "Qt copyright: The Qt Company Ltd. and other contributors.",
        "These libraries are dynamically linked in _internal/PySide6 and _internal/shiboken6.",
        "You may replace them with interface-compatible modified versions.",
        "Reverse engineering for debugging such library modifications is not restricted.",
        "License texts and third-party attributions are included in licenses/.",
        "Corresponding Qt/PySide source archives accompany this build as a separate sources ZIP.",
        "Source archive URLs and SHA-256 values:",
        *[json.dumps(item) for item in source_records],
        "",
        "Python: " + platform.python_version(),
        "PyInstaller bootloader: " + metadata.version("pyinstaller"),
        "",
        "Runtime packages:",
    ]
    lines.extend(f"{item['name']} {item['version']}: {item['license']}" for item in packages)
    (bundle / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (bundle / "runtime-packages.json").write_text(json.dumps(packages, indent=2), encoding="utf-8")


def build(output):
    if sys.platform != "win32" or platform.machine().upper() not in {"AMD64", "X86_64"}:
        raise SystemExit("Build Windows x64 packages on Windows x64 with Python 3.12")
    if sys.version_info[:2] != (3, 12) or metadata.version("PySide6-Essentials") != QT_VERSION:
        raise SystemExit("The Windows build requires the pinned Python and Qt versions")
    if output.exists():
        raise SystemExit("Choose a new empty output path")
    output.mkdir(parents=True)
    work = ROOT / "build" / ("windows-" + __version__)
    work.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        "ModMesh",
        "--icon",
        "NONE",
        "--noupx",
        "--distpath",
        str(output),
        "--workpath",
        str(work / "pyinstaller"),
        "--specpath",
        str(work),
        "--paths",
        str(ROOT / "src"),
        "--hidden-import",
        "keyring.backends.Windows",
        "--collect-submodules",
        "win32ctypes",
        "--copy-metadata",
        "keyring",
        "--copy-metadata",
        "jaraco.classes",
        "--copy-metadata",
        "jaraco.context",
        "--copy-metadata",
        "jaraco.functools",
        "--exclude-module",
        "modmesh.apps.server",
        str(ROOT / "packaging/desktop_entry.py"),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    bundle = output / "ModMesh"
    source_folder = work / "sources"
    if source_folder.exists():
        raise SystemExit("Previous source downloads exist; use a fresh build directory")
    license_files(bundle, source_folder)
    shutil.copy2(ROOT / "docs/windows.md", bundle / "README.txt")
    permitted = {
        "Qt6Core.dll",
        "Qt6Gui.dll",
        "Qt6Widgets.dll",
        "Qt6Network.dll",
        "Qt6OpenGL.dll",
        "Qt6OpenGLWidgets.dll",
        "Qt6Concurrent.dll",
        "Qt6PrintSupport.dll",
        "Qt6Sql.dll",
        "Qt6Test.dll",
        "Qt6Xml.dll",
        "Qt6Svg.dll",
        "Qt6SvgWidgets.dll",
        "Qt6DBus.dll",
    }
    unknown = [path.name for path in bundle.rglob("Qt6*.dll") if path.name not in permitted]
    if unknown:
        raise RuntimeError(f"Review additional Qt modules before distribution: {unknown}")
    portable = output / f"ModMesh-{__version__}-windows-x64-portable.zip"
    with zipfile.ZipFile(portable, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output))
    source_zip = output / f"ModMesh-{__version__}-third-party-sources.zip"
    with zipfile.ZipFile(source_zip, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in sorted(source_folder.iterdir()):
            archive.write(path, path.name)
    compiler = shutil.which("ISCC.exe") or str(
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
        / "Inno Setup 6/ISCC.exe"
    )
    subprocess.run(
        [
            compiler,
            f"/DAppVersion={__version__}",
            f"/DBundleRoot={bundle}",
            f"/DOutputRoot={output}",
            str(ROOT / "packaging/windows.iss"),
        ],
        check=True,
    )
    files = sorted(output.glob("*.zip")) + sorted(output.glob("*-setup.exe"))
    (output / "SHA256SUMS.txt").write_text(
        "".join(f"{digest(path)}  {path.name}\n" for path in files), encoding="utf-8"
    )
    print(json.dumps({"version": __version__, "files": [path.name for path in files]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "windows")
    build(parser.parse_args().output.resolve())
