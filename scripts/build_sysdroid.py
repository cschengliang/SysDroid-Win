"""Produce the Windows x64 portable directory, ZIP, and SHA-256 manifests.

Entry (from the repository root): lib/python-3.14.8-embed-amd64/python.exe -s scripts/build_sysdroid.py
The development interpreter, its _pth file, and installed packages are never changed.
Each freeze attempt runs in a fresh instance of that same embedded interpreter.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import importlib.util
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import traceback
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid
import zipfile

SCRIPTS_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = SCRIPTS_DIR.parent
PACKAGE_ROOT = SOURCE_ROOT / "src"
EMBEDDED_ROOT = SOURCE_ROOT / "lib" / "python-3.14.8-embed-amd64"
SITE_PACKAGES = EMBEDDED_ROOT / "Lib" / "site-packages"
BUILD_ROOT = SOURCE_ROOT / "build" / "sysdroid"
RELEASE_NAME = "SysDroid-win-x64"
EXPECTED_PYTHON = (3, 14, 8)
STDLIB_FAILURE_EXIT = 71
QT_METADATA_EXCEPTION = (
    "PySide6_Addons is a dependency of the PySide6 umbrella distribution, "
    "but is deliberately omitted: this application uses QtCore/QtGui/QtWidgets "
    "and only their normal hook-selected Essentials dependencies."
)


class BuildError(RuntimeError):
    """An unmet release prerequisite; previously published outputs stay intact."""


class StdlibCollectionError(BuildError):
    """The extracted embedded bytecode could not be collected by Analysis."""


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _beneath(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _reject_link(path: Path) -> None:
    if path.is_symlink() or path.is_junction():
        raise BuildError(f"Refusing a symbolic link or junction in build/release paths: {path}")


def _owned_directory(path: Path) -> None:
    # Check existing ancestors too; neither staging nor promotion may traverse a junction.
    current = path
    while current != SOURCE_ROOT:
        if not current.is_relative_to(SOURCE_ROOT):
            raise BuildError(f"Build directory is outside the source root: {path}")
        _reject_link(current)
        current = current.parent
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise BuildError(f"Expected a directory: {path}")


def _require_interpreter() -> None:
    expected = EMBEDDED_ROOT / "python.exe"
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise BuildError("This release must be built on Windows with the embedded x64 interpreter.")
    if Path(sys.executable).resolve() != expected.resolve():
        raise BuildError(f"Use this exact interpreter: {expected} -s {Path(__file__).resolve()}")
    if tuple(sys.version_info[:3]) != EXPECTED_PYTHON:
        raise BuildError(f"Expected CPython {'.'.join(map(str, EXPECTED_PYTHON))}; observed {sys.version}.")
    if not sys.flags.no_user_site:
        raise BuildError("User site-packages must be disabled; invoke the embedded interpreter with -s.")
    for entry in sys.path:
        if not entry:
            raise BuildError("A current-working-directory import path is not allowed during a release build.")
        path = Path(entry).resolve()
        if not (_beneath(path, EMBEDDED_ROOT) or path in (SOURCE_ROOT, PACKAGE_ROOT, SCRIPTS_DIR)):
            raise BuildError(f"Unexpected interpreter import path (no external Python dependencies allowed): {path}")
    for filename in ("python314.zip", "python314.dll", "python3.dll", "LICENSE.txt"):
        if not (EMBEDDED_ROOT / filename).is_file():
            raise BuildError(f"Required embedded interpreter input is missing: {EMBEDDED_ROOT / filename}")


@contextmanager
def _build_lock():
    import msvcrt

    _owned_directory(BUILD_ROOT)
    # OS locking releases automatically after a crash, unlike a stale sentinel file.
    with (BUILD_ROOT / "build.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BuildError("Another SysDroid release build is holding the build lock.") from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _dependency_locks(stage: Path) -> tuple[dict[str, metadata.Distribution], dict[str, metadata.Distribution]]:
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from packaging.version import Version

    installed: dict[str, metadata.Distribution] = {}
    for distribution in metadata.distributions(path=[str(SITE_PACKAGES)]):
        name = canonicalize_name(distribution.metadata["Name"])
        if name in installed:
            raise BuildError(f"Duplicate installed distribution metadata: {name}")
        installed[name] = distribution

    marker_environment = default_environment()

    def collect(roots: tuple[str, ...], *, omit_qt_addons: bool = False) -> dict[str, metadata.Distribution]:
        collected: dict[str, metadata.Distribution] = {}
        extras: dict[str, set[str]] = {}
        pending = [(name, set()) for name in roots]
        while pending:
            requested_name, requested_extras = pending.pop()
            name = canonicalize_name(requested_name)
            distribution = installed.get(name)
            if distribution is None:
                raise BuildError(f"Required distribution is not installed in the embedded interpreter: {name}")
            previous_extras = extras.get(name)
            if name in collected and requested_extras.issubset(previous_extras or set()):
                continue
            extras.setdefault(name, set()).update(requested_extras)
            collected[name] = distribution
            for declaration in distribution.requires or ():
                requirement = Requirement(declaration)
                if requirement.marker and not any(
                    requirement.marker.evaluate({**marker_environment, "extra": extra})
                    for extra in {"", *extras[name]}
                ):
                    continue
                child_name = canonicalize_name(requirement.name)
                if omit_qt_addons and name == "pyside6" and child_name == "pyside6-addons":
                    continue
                child = installed.get(child_name)
                if child is None:
                    raise BuildError(f"Missing dependency {declaration!r} required by {distribution.metadata['Name']}")
                if requirement.url:
                    raise BuildError(f"A direct-URL dependency needs explicit provenance review: {declaration}")
                if requirement.specifier and Version(child.version) not in requirement.specifier:
                    raise BuildError(
                        f"Unsatisfied dependency: {distribution.metadata['Name']} needs {declaration}; "
                        f"embedded {child.metadata['Name']} is {child.version}."
                    )
                pending.append((requirement.name, set(requirement.extras)))
        return collected

    runtime = collect(("PySide6", "adbutils"), omit_qt_addons=True)
    build = collect(("PyInstaller", "pyinstaller-hooks-contrib", "pefile"))
    for filename, distributions, comment in (
        ("requirements-runtime.txt", runtime, QT_METADATA_EXCEPTION),
        ("requirements-build.txt", build, "Build tools only; these are not runtime dependencies."),
    ):
        text = f"# CPython {'.'.join(map(str, EXPECTED_PYTHON))}; Windows x64; actual installed versions\n# {comment}\n"
        text += "".join(
            f"{distribution.metadata['Name']}=={distribution.version}\n"
            for _, distribution in sorted(distributions.items())
        )
        (stage / filename).write_text(text, encoding="utf-8")
    _json(stage / "runtime-packages.json", [d.metadata["Name"] for _, d in sorted(runtime.items())])
    return runtime, build


def _distribution_info(distributions: dict[str, metadata.Distribution]) -> list[dict]:
    result = []
    for _, distribution in sorted(distributions.items()):
        metadata_files = [item for item in distribution.files or ()
                          if str(item).endswith(".dist-info/METADATA") and len(PurePosixPath(str(item)).parts) == 2]
        if len(metadata_files) != 1:
            raise BuildError(f"Cannot identify METADATA for installed distribution {distribution.metadata['Name']}")
        path = Path(distribution.locate_file(metadata_files[0])).resolve()
        if not _beneath(path, SITE_PACKAGES):
            raise BuildError(f"Distribution metadata is outside embedded site-packages: {path}")
        result.append({
            "name": distribution.metadata["Name"], "version": distribution.version,
            "metadata_sha256": _sha256(path),
            "project_urls": distribution.metadata.get_all("Project-URL") or [],
        })
    return result


def _extract_stdlib(stage: Path) -> dict:
    target = stage / "stdlib"
    target.mkdir()
    archive = EMBEDDED_ROOT / "python314.zip"
    with zipfile.ZipFile(archive) as source:
        seen = set()
        for item in source.infolist():
            relative = PurePosixPath(item.filename)
            if relative.is_absolute() or ".." in relative.parts or "\\" in item.filename or ":" in item.filename:
                raise BuildError(f"Unsafe embedded stdlib archive member: {item.filename}")
            key = item.filename.casefold()
            if key in seen:
                raise BuildError(f"Duplicate embedded stdlib archive member: {item.filename}")
            seen.add(key)
            destination = target.joinpath(*relative.parts)
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.open(item) as reader, destination.open("wb") as writer:
                    shutil.copyfileobj(reader, writer)
    encoding = target / "encodings" / "__init__.pyc"
    if not encoding.is_file():
        raise BuildError(f"The actual embedded stdlib has no encodings/__init__.pyc: {archive}")
    for path in target.rglob("*.pyc"):
        with path.open("rb") as reader:
            if reader.read(4) != importlib.util.MAGIC_NUMBER:
                raise BuildError(f"Embedded bytecode does not match this interpreter: {path}")
    return {"archive": archive.name, "sha256": _sha256(archive), "mode": "embedded-bytecode", "fallback": None}


def _official_stdlib_fallback(stage: Path, reason: str) -> dict:
    """Supplement only from the official source archive for this exact interpreter."""
    version = ".".join(map(str, sys.version_info[:3]))
    url = f"https://www.python.org/ftp/python/{version}/Python-{version}.tar.xz"
    cache = stage / "asset-cache"
    cache.mkdir(exist_ok=True)
    archive = cache / f"Python-{version}.tar.xz"
    try:
        request = Request(url, headers={"User-Agent": "SysDroid-release-build"})
        with urlopen(request, timeout=120) as response, archive.open("wb") as writer:
            final_url = urlsplit(response.geturl())
            if final_url.scheme != "https" or final_url.hostname != "www.python.org":
                raise BuildError(f"Unexpected CPython source download redirect: {response.geturl()}")
            shutil.copyfileobj(response, writer)
    except Exception as exc:
        raise BuildError(
            f"Embedded bytecode collection failed ({reason}). Exact official CPython {version} Lib "
            f"fallback could not be obtained from {url}: {exc}. No other Python version will be used."
        ) from exc
    prefix = f"Python-{version}"
    with tarfile.open(archive, "r:xz") as source:
        try:
            patch = source.extractfile(f"{prefix}/Include/patchlevel.h")
            if patch is None:
                raise BuildError("Official CPython archive has no patchlevel.h")
            with patch:
                header = patch.read().decode("utf-8")
        except (KeyError, UnicodeError) as exc:
            raise BuildError(f"Cannot verify official CPython source version: {archive}") from exc
        for field, expected in zip(("MAJOR", "MINOR", "MICRO"), sys.version_info[:3]):
            match = re.search(rf"^#define\s+PY_{field}_VERSION\s+(\d+)\s*$", header, re.MULTILINE)
            if not match or int(match[1]) != expected:
                raise BuildError(f"Official CPython source version does not match the embedded interpreter: {archive}")
        if not re.search(rf'^#define\s+PY_VERSION\s+"{re.escape(version)}"\s*$', header, re.MULTILINE):
            raise BuildError(f"Official CPython source is not the exact final {version} release: {archive}")
        count = 0
        seen = set()
        for item in source.getmembers():
            relative = PurePosixPath(item.name)
            if relative.parts[:2] != (prefix, "Lib"):
                continue
            if relative.is_absolute() or ".." in relative.parts or "\\" in item.name or ":" in item.name:
                raise BuildError(f"Unsafe official CPython Lib member: {item.name}")
            if item.isdir():
                continue
            if not item.isfile():
                raise BuildError(f"Nonregular file in official CPython Lib: {item.name}")
            key = item.name.casefold()
            if key in seen:
                raise BuildError(f"Duplicate official CPython Lib member: {item.name}")
            seen.add(key)
            destination = (stage / "stdlib").joinpath(*relative.parts[2:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            reader = source.extractfile(item)
            if reader is None:
                raise BuildError(f"Unreadable official CPython Lib member: {item.name}")
            with reader, destination.open("wb") as writer:
                shutil.copyfileobj(reader, writer)
            count += 1
        if not (stage / "stdlib" / "encodings" / "__init__.py").is_file():
            raise BuildError(f"Official CPython Lib is incomplete: {archive}")
    return {"url": url, "version": version, "sha256": _sha256(archive), "lib_files": count, "reason": reason}


def ensure_analysis_inputs(source_root: Path, stage: Path, stdlib: Path) -> None:
    if source_root != SOURCE_ROOT or not _beneath(stage, BUILD_ROOT) or stdlib != stage / "stdlib":
        raise BuildError("The spec can only consume this source root's dedicated build stage.")
    marker = json.loads((stage / "build-stage.json").read_text(encoding="utf-8"))
    if marker.get("source_root") != str(SOURCE_ROOT) or marker.get("python") != str(EMBEDDED_ROOT / "python.exe"):
        raise BuildError("Build stage identity does not match this source tree and embedded interpreter.")
    if not (stdlib / "encodings" / "__init__.pyc").is_file():
        raise BuildError("The stage must contain the extracted actual embedded standard library.")


def _stdlib_module_exists(stdlib: Path, name: str) -> bool:
    path = stdlib.joinpath(*name.split("."))
    return any(candidate.is_file() for candidate in (
        path.with_suffix(".pyc"), path.with_suffix(".py"), path / "__init__.pyc", path / "__init__.py",
    ))


def validate_stdlib_analysis(analysis, source_root: Path, stage: Path, stdlib: Path) -> None:
    """Reject silently missing embedded bytecode and nonembedded build inputs."""
    from PyInstaller.compat import BAD_MODULE_TYPES, PY3_BASE_MODULES

    missing = []
    for name in sorted(PY3_BASE_MODULES | {"encodings", "encodings.utf_8", "importlib.metadata"}):
        node = analysis.graph.find_node(name)
        if node is None or type(node).__name__ in BAD_MODULE_TYPES:
            missing.append(name)
    for node in analysis.graph.iter_graph():
        kind = type(node).__name__
        if kind in {"InvalidCompiledModule", "MissingModule"} and _stdlib_module_exists(stdlib, node.identifier):
            missing.append(node.identifier)
    if missing:
        raise StdlibCollectionError("Analysis did not collect embedded stdlib modules: " + ", ".join(sorted(set(missing))))
    windows = Path(os.environ.get("SystemRoot", "C:/Windows")).resolve()
    for name, source, kind in [*analysis.pure, *analysis.binaries, *analysis.datas]:
        path = Path(source).resolve()
        approved = _beneath(path, EMBEDDED_ROOT) or _beneath(path, stage)
        app_root = source_root / "src" / "sysdroid"
        approved = approved or path == source_root / "sysdroid.py"
        approved = approved or (_beneath(path, app_root) and path.suffix == ".py")
        approved = approved or (path.parent == source_root / "assets" / "icons" and path.suffix == ".png"
                                and path.name.startswith("sysdroid-"))
        if not approved:
            raise BuildError(f"Analysis collected a file outside approved embedded/application inputs: {name}: {path} ({kind})")
        if path.suffix.lower() in {".pyd", ".dll"} and _beneath(path, windows):
            raise BuildError(f"Analysis unexpectedly bundled a Windows system binary: {path}")
    if not (Path(analysis.graph.find_node("encodings").filename).resolve().is_relative_to(stdlib)):
        raise StdlibCollectionError("Analysis did not use the staged embedded encodings package.")


def prune_unused_qt_addons(analysis, stage: Path) -> None:
    """Drop hook-selected optional plugins that depend on the unused Addons wheel.

    For example QtGui's imageformats directory contains qpdf.dll, even though
    Qt6Pdf.dll belongs to Addons. Ordinary Qt hooks still select all plugins;
    wheel ownership and actual PE imports make this final selection explicit.
    The final PE audit rejects any remaining mandatory dependency on Addons.
    """
    import pefile

    addons = next((distribution for distribution in metadata.distributions(path=[str(SITE_PACKAGES)])
                   if re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).casefold() == "pyside6-addons"), None)
    if addons is None:
        _json(stage / "qt-selection.json", {"removed": [], "reason": "No Addons wheel installed."})
        return
    addon_sources = {Path(addons.locate_file(item)).resolve() for item in addons.files or ()}
    # All three PySide wheels own the same namespace files. Only Addons-exclusive
    # paths are candidates for removal; the approved runtime owns __init__.py too.
    runtime_names = json.loads((stage / "runtime-packages.json").read_text(encoding="utf-8"))
    for name in runtime_names:
        distribution = metadata.distribution(name)
        addon_sources.difference_update(Path(distribution.locate_file(item)).resolve()
                                        for item in distribution.files or ())
    addon_dll_names = {path.name.casefold() for path in addon_sources if path.suffix.casefold() == ".dll"}
    removed = []
    plugin_sources = set()
    native_imports = {}
    for name, source, _ in analysis.binaries:
        path = Path(source).resolve()
        if path.suffix.casefold() not in {".dll", ".pyd", ".exe"}:
            continue
        with pefile.PE(str(path), fast_load=True) as image:
            image.parse_data_directories(directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
            ])
            imports = {entry.dll.decode("ascii").casefold() for attribute in
                       ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT")
                       for entry in getattr(image, attribute, ())}
        native_imports[path] = imports
        if "plugins" in path.parts and _beneath(path, SITE_PACKAGES / "PySide6") and imports & addon_dll_names:
            plugin_sources.add(path)
    # Dependency collection ran before the Addons plugins were removed. Their
    # QML/Quick dependencies can remain even though no kept binding/plugin needs
    # them. Prove them unreachable instead of blindly dropping required DLLs.
    qt_libraries = {path.name.casefold(): path for path in native_imports
                    if path.parent == SITE_PACKAGES / "PySide6" and path.name.casefold().startswith("qt6")}
    roots = [path for path in native_imports
             if path not in addon_sources and path not in plugin_sources and path not in qt_libraries.values()]
    reachable = set(roots)
    while roots:
        for dependency in native_imports[roots.pop()]:
            path = qt_libraries.get(dependency)
            if path is not None and path not in reachable:
                reachable.add(path)
                roots.append(path)
    orphan_qt = {path for name, path in qt_libraries.items()
                 if name.startswith(("qt6qml", "qt6quick")) and path not in reachable}
    for attribute in ("pure", "binaries", "datas"):
        retained = []
        for entry in getattr(analysis, attribute):
            name, source, kind = entry
            path = Path(source).resolve()
            if path in addon_sources or path in plugin_sources or path in orphan_qt:
                if kind == "EXTENSION" or attribute == "pure":
                    raise BuildError(f"The application imports an unapproved Qt Addons module: {name}")
                removed.append(name)
            else:
                retained.append(entry)
        setattr(analysis, attribute, retained)
    _json(stage / "qt-selection.json", {"removed": sorted(removed), "reason": QT_METADATA_EXCEPTION})


def _pyc_collection_failure(exc: BaseException, stdlib: Path) -> bool:
    current: BaseException | None = exc
    visited = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, StdlibCollectionError):
            return True
        if isinstance(current, ImportError) and current.name and _stdlib_module_exists(stdlib, current.name):
            return True
        if isinstance(current, FileNotFoundError) and current.filename:
            filename = Path(current.filename)
            if _beneath(filename, stdlib) or "python314.zip" in str(filename).casefold():
                return True
        current = current.__cause__ or current.__context__
    return False


def _freeze_child(stage: Path, attempt: int) -> int:
    ensure_analysis_inputs(SOURCE_ROOT, stage, stage / "stdlib")
    stdlib = stage / "stdlib"
    attempt_root = stage / f"freeze-{attempt}"
    attempt_root.mkdir()
    sys.path[:0] = [str(stdlib), str(PACKAGE_ROOT)]
    importlib.invalidate_caches()
    # PyInstaller's isolated child explicitly receives sys.path, so it also sees
    # the extracted packages despite the embedded interpreter ignoring PYTHONPATH.
    os.environ["SYSDROID_BUILD_STAGE"] = str(stage)
    os.environ["SYSDROID_BUILD_STDLIB"] = str(stdlib)
    os.environ["PYINSTALLER_CONFIG_DIR"] = str(attempt_root / "pyinstaller-cache")
    os.environ["PYTHONNOUSERSITE"] = "1"
    os.environ.pop("PYTHONPATH", None)
    os.environ.pop("PYTHONHOME", None)
    sys.modules["build_sysdroid"] = sys.modules["__main__"]
    handler = logging.FileHandler(attempt_root / "pyinstaller.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger("PyInstaller").addHandler(handler)
    try:
        from PyInstaller.__main__ import run
        from PyInstaller.configure import get_config

        config = get_config(upx_dir=None)
        config["cachedir"] = str(attempt_root / "pyinstaller-cache")
        run([
            str(SOURCE_ROOT / "SysDroid.spec"),
            "--noconfirm", "--distpath", str(attempt_root / "dist"),
            "--workpath", str(attempt_root / "work"), "--log-level", "INFO",
        ], pyi_config=config)
        _validate_runtime(attempt_root / "dist" / RELEASE_NAME)
        return 0
    except BaseException as exc:
        detail = {"error": str(exc), "traceback": traceback.format_exc()}
        if _pyc_collection_failure(exc, stdlib):
            _json(attempt_root / "stdlib-failure.json", detail)
            return STDLIB_FAILURE_EXIT
        _json(attempt_root / "build-failure.json", detail)
        if isinstance(exc, KeyboardInterrupt):
            return 130
        if isinstance(exc, SystemExit) and exc.code == 0:
            return 0
        print(f"Freeze attempt failed: {exc}", file=sys.stderr)
        return 1
    finally:
        handler.close()
        logging.getLogger("PyInstaller").removeHandler(handler)


def _freeze(stage: Path, stdlib_info: dict) -> Path:
    for attempt in (1, 2):
        result = subprocess.run(
            [str(EMBEDDED_ROOT / "python.exe"), "-s", str(Path(__file__).resolve()),
             "--freeze-stage", str(stage), "--attempt", str(attempt)],
            cwd=stage, check=False,
        )
        attempt_root = stage / f"freeze-{attempt}"
        if result.returncode == 0:
            return attempt_root / "dist" / RELEASE_NAME
        failure_path = attempt_root / "stdlib-failure.json"
        if result.returncode == STDLIB_FAILURE_EXIT and attempt == 1 and failure_path.is_file():
            reason = json.loads(failure_path.read_text(encoding="utf-8"))["error"]
            print(f"Embedded pyc collection failed; obtaining exact official Lib fallback: {reason}")
            stdlib_info["fallback"] = _official_stdlib_fallback(stage, reason)
            stdlib_info["mode"] = "embedded-bytecode-with-exact-official-Lib"
            continue
        raise BuildError(
            f"PyInstaller freeze failed (exit {result.returncode}); details are preserved at {attempt_root}. "
            "No previous release outputs have been changed."
        )
    raise BuildError("Exact official CPython fallback did not produce a complete frozen application.")


def _copy_interpreter_licenses(release: Path, build: dict[str, metadata.Distribution]) -> None:
    python_license = release / "licenses" / "Python" / "LICENSE.txt"
    python_license.parent.mkdir(parents=True)
    shutil.copy2(EMBEDDED_ROOT / "LICENSE.txt", python_license)
    distribution = build["pyinstaller"]
    copied = []
    for item in distribution.files or ():
        name = PurePosixPath(str(item).replace("\\", "/"))
        if ".dist-info" not in str(name) or not any(
            part.casefold() in {"licenses", "license", "license.txt", "copying", "copying.txt", "notice", "notice.txt"}
            for part in name.parts
        ):
            continue
        source = Path(distribution.locate_file(item)).resolve()
        if not source.is_file() or not _beneath(source, SITE_PACKAGES):
            raise BuildError(f"PyInstaller license input is missing or outside embedded packages: {source}")
        destination = release / "licenses" / "PyInstaller" / name.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and _sha256(destination) != _sha256(source):
            raise BuildError(f"Conflicting PyInstaller license filenames: {source}")
        shutil.copy2(source, destination)
        copied.append(destination)
    if not any(path.name.casefold() == "copying.txt" for path in copied):
        raise BuildError("Installed PyInstaller has no bootloader COPYING.txt; release licensing is incomplete.")


def _validate_runtime(release: Path) -> None:
    if not (release / "SysDroid.exe").is_file():
        raise BuildError(f"COLLECT did not create the application executable: {release}")
    internal = release / "_internal"
    required = (
        internal / "python314.dll",
        internal / "base_library.zip",
        internal / "PySide6" / "plugins" / "platforms" / "qwindows.dll",
        internal / "PySide6" / "plugins" / "styles" / "qmodernwindowsstyle.dll",
        internal / "certifi" / "cacert.pem",
        internal / "assets" / "icons" / "sysdroid-16.png",
        internal / "assets" / "icons" / "sysdroid-256.png",
    )
    for path in required:
        if not path.is_file():
            raise BuildError(f"Frozen runtime asset was not collected: {path}")
    for directory, pattern in (("shiboken6", "*.pyd"), ("PIL", "_imaging*.pyd")):
        if not any((internal / directory).glob(pattern)):
            raise BuildError(f"Frozen runtime is missing {directory}/{pattern}")
    with zipfile.ZipFile(internal / "base_library.zip") as archive:
        if "encodings/__init__.pyc" not in archive.namelist() or "encodings/utf_8.pyc" not in archive.namelist():
            raise StdlibCollectionError("Frozen base_library.zip does not contain the required encodings package.")
    forbidden = ("qt6qml", "qt6quick", "qt6webengine", "qtwebengineprocess", "pyside6_addons")
    for path in internal.rglob("*"):
        _reject_link(path)
        if any(part.casefold() in {"__pycache__", "site-packages", "scripts", "qml"} for part in path.relative_to(internal).parts):
            raise BuildError(f"Development/cache/QML tree was unexpectedly bundled: {path}")
        if path.is_file() and any(term in path.name.casefold() for term in forbidden):
            raise BuildError(f"An unused Qt Addons/QML/WebEngine component was bundled: {path}")


def _tree(root: Path) -> tuple[dict[str, str], set[str]]:
    files = {}
    directories = {""}
    casefolded = set()
    for path in sorted(root.rglob("*")):
        _reject_link(path)
        relative = path.relative_to(root).as_posix()
        key = relative.casefold()
        if key in casefolded:
            raise BuildError(f"Case-insensitive duplicate release path: {relative}")
        casefolded.add(key)
        if path.is_file():
            files[relative] = _sha256(path)
        elif path.is_dir():
            directories.add(relative)
        else:
            raise BuildError(f"Nonregular release asset: {path}")
    return files, directories


def _archive_and_validate(stage: Path, release: Path) -> list[tuple[Path, str]]:
    files, _ = _tree(release)
    sums = release / "SHA256SUMS.txt"
    sums.write_text(
        "# SHA-256 of all release files except this self-referential manifest.\n"
        + "".join(f"{digest}  {name}\n" for name, digest in files.items()), encoding="utf-8",
    )
    files, directories = _tree(release)
    archive_path = stage / f"{RELEASE_NAME}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True) as archive:
        for name in sorted(directories):
            archive.write(release / name, f"{RELEASE_NAME}/{name}".rstrip("/") + "/")
        for name in files:
            archive.write(release / name, f"{RELEASE_NAME}/{name}")
    extracted = stage / "zip-validation"
    extracted.mkdir()
    with zipfile.ZipFile(archive_path) as archive:
        expected = {f"{RELEASE_NAME}/{name}" for name in files}
        expected |= {f"{RELEASE_NAME}/{name}".rstrip("/") + "/" for name in directories}
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != expected:
            raise BuildError("ZIP entry tree does not exactly match the release tree.")
        for name in names:
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
                raise BuildError(f"Unsafe ZIP entry: {name}")
        archive.extractall(extracted)
    extracted_files, extracted_dirs = _tree(extracted / RELEASE_NAME)
    if extracted_files != files or extracted_dirs != directories:
        raise BuildError("Extracted ZIP files/directories or SHA-256 values differ from the release directory.")
    external_files = stage / f"{RELEASE_NAME}.files.sha256"
    external_files.write_text(
        "".join(f"{digest}  {RELEASE_NAME}/{name}\n" for name, digest in files.items()), encoding="utf-8",
    )
    archive_sums = stage / f"{RELEASE_NAME}.zip.sha256"
    archive_digest = _sha256(archive_path)
    archive_sums.write_text(f"{archive_digest}  {archive_path.name}\n", encoding="ascii")
    _json(stage / "zip-validation.json", {"files": len(files), "directories": len(directories), "sha256": archive_digest})
    return [(release, RELEASE_NAME), (archive_path, archive_path.name),
            (external_files, external_files.name), (archive_sums, archive_sums.name)]


def _promote(stage: Path, artifacts: list[tuple[Path, str]]) -> Path | None:
    """Atomic renames per output, with old generations preserved and rollback on failure."""
    destination = SOURCE_ROOT / "dist"
    _owned_directory(destination)
    backup = destination / f".{RELEASE_NAME}-backup-{stage.name}"
    previous = []
    promoted = []
    for source, name in artifacts:
        target = destination / name
        _reject_link(target)
        if target.exists() and (target.is_dir() != source.is_dir()):
            raise BuildError(f"Existing output has the wrong file/directory type: {target}")
    _json(stage / "promotion.json", {"state": "prepared", "outputs": [name for _, name in artifacts], "backup": str(backup)})
    try:
        for _, name in artifacts:
            target = destination / name
            if target.exists():
                backup.mkdir(exist_ok=True)
                os.replace(target, backup / name)
                previous.append(name)
        for source, name in artifacts:
            os.replace(source, destination / name)
            promoted.append((source, name))
        _json(stage / "promotion.json", {"state": "committed", "outputs": [name for _, name in artifacts],
                                         "backup": str(backup) if previous else None})
    except BaseException as exc:
        rollback_errors = []
        for source, name in reversed(promoted):
            try:
                os.replace(destination / name, source)
            except OSError as error:
                rollback_errors.append(f"New output {name}: {error}")
        for name in reversed(previous):
            try:
                target = destination / name
                if target.exists():
                    raise OSError(f"New output still occupies {target}; preserving its old copy in {backup}")
                os.replace(backup / name, target)
            except OSError as error:
                rollback_errors.append(f"Old output {name}: {error}")
        message = f"Release promotion failed: {exc}. Old-output backup: {backup}. Stage: {stage}."
        if rollback_errors:
            message += " Rollback needs manual recovery; all copies were preserved: " + "; ".join(rollback_errors)
        else:
            message += " Previous outputs were restored."
        raise BuildError(message) from exc
    return backup if previous else None


def _stage_vc_entitlement(stage: Path, supplied: Path | None) -> None:
    path = (supplied or (BUILD_ROOT / "vc-license-entitlement.json")).resolve()
    if supplied is None and not path.exists():
        # Official provider-supplied DLLs may already satisfy the PE closure.
        # PortableAssets requires entitlement only for a newly extracted redist.
        return
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(
            f"Microsoft VC redistribution requires the actual licensed builder's eligibility record: {path}. "
            "Provide edition, licensing_basis and license_terms_accepted=true; an installed runtime "
            "does not establish redistribution rights. Use --vc-entitlement <existing record> or place "
            "it at build/sysdroid/vc-license-entitlement.json. "
            "See https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution"
        ) from exc
    if not isinstance(record, dict) or record.get("license_terms_accepted") is not True or not all(
        isinstance(record.get(name), str) and record[name].strip() for name in ("edition", "licensing_basis")
    ):
        raise BuildError(f"VC redistribution basis must contain actual edition/basis and accepted terms: {path}")
    _json(stage / "vc-license-entitlement.json", record)


def _build(vc_entitlement: Path | None = None) -> int:
    _require_interpreter()
    # Validate build-only module origins before prepending the application directory.
    for name in ("packaging", "PyInstaller", "pefile"):
        spec = importlib.util.find_spec(name)
        if spec is None or not spec.origin or not _beneath(Path(spec.origin), SITE_PACKAGES):
            raise BuildError(f"Build prerequisite must come from embedded site-packages: {name}")
    with _build_lock():
        token = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
        stage = BUILD_ROOT / token
        stage.mkdir()
        _json(stage / "build-stage.json", {"source_root": str(SOURCE_ROOT), "python": str(EMBEDDED_ROOT / "python.exe")})
        print(f"Building in dedicated stage: {stage}")
        try:
            _stage_vc_entitlement(stage, vc_entitlement)
            runtime, build = _dependency_locks(stage)
            stdlib_info = _extract_stdlib(stage)
            release = _freeze(stage, stdlib_info)
            _validate_runtime(release)
            _copy_interpreter_licenses(release, build)
            for filename in ("requirements-runtime.txt", "requirements-build.txt"):
                shutil.copy2(stage / filename, release / filename)
            info = {
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "target": "Windows 11 x64", "layout": "windowed onedir; replaceable DLLs in _internal",
                "python": {"version": sys.version, "executable_sha256": _sha256(EMBEDDED_ROOT / "python.exe"),
                           "dll_sha256": _sha256(EMBEDDED_ROOT / "python314.dll"),
                           "license_sha256": _sha256(EMBEDDED_ROOT / "LICENSE.txt")},
                "stdlib": stdlib_info,
                "qt_selection": json.loads((stage / "qt-selection.json").read_text(encoding="utf-8")),
                "runtime_distributions": _distribution_info(runtime),
                "build_distributions": _distribution_info(build),
                "dependency_exceptions": [QT_METADATA_EXCEPTION],
                "build_inputs": {path.name: _sha256(path) for path in (SOURCE_ROOT / "SysDroid.spec", Path(__file__).resolve(), SCRIPTS_DIR / "portable_assets.py", SOURCE_ROOT / "assets" / "SysDroid.ico")},
            }
            _json(release / "build-info.json", info)
            shutil.copy2(SOURCE_ROOT / "README.md", release / "README.md")
            sys.path.insert(0, str(SCRIPTS_DIR))
            from portable_assets import stage_portable_assets

            assets = stage_portable_assets(SOURCE_ROOT, release, stage)
            # Auditing/licenses must have been completed before any old output is moved.
            for filename in ("components.json", "pe-audit.json"):
                if not (release / "licenses" / filename).is_file():
                    raise BuildError(f"Portable asset staging omitted its required evidence: licenses/{filename}")
            _validate_runtime(release)
            _json(stage / "assets-result.json", assets)
            artifacts = _archive_and_validate(stage, release)
            # Publish build-side locks before committing release outputs; any I/O failure
            # here must leave the previous directory and ZIP untouched.
            for filename in ("requirements-runtime.txt", "requirements-build.txt"):
                temporary = BUILD_ROOT / f".{filename}.{token}.tmp"
                shutil.copy2(stage / filename, temporary)
                os.replace(temporary, BUILD_ROOT / filename)
            backup = _promote(stage, artifacts)
            print(f"Portable directory: {SOURCE_ROOT / 'dist' / RELEASE_NAME}")
            print(f"Portable ZIP: {SOURCE_ROOT / 'dist' / (RELEASE_NAME + '.zip')}")
            print(f"Checksums: {RELEASE_NAME}.files.sha256; {RELEASE_NAME}.zip.sha256")
            if backup:
                print(f"Previous release preserved: {backup}")
            return 0
        except BaseException as exc:
            _json(stage / "failure.json", {"error": str(exc), "traceback": traceback.format_exc()})
            print(f"Build failed; diagnostic stage preserved: {stage}", file=sys.stderr)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze-stage", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--attempt", type=int, choices=(1, 2), help=argparse.SUPPRESS)
    parser.add_argument("--vc-entitlement", type=Path,
                        help="Existing JSON record of the actual builder's eligible Visual Studio redistribution basis")
    args = parser.parse_args()
    _require_interpreter()
    if args.freeze_stage is not None:
        if args.attempt is None:
            parser.error("An internal freeze stage requires an attempt number.")
        return _freeze_child(args.freeze_stage.resolve(), args.attempt)
    if args.attempt is not None:
        parser.error("Attempt numbers are only valid with an internal freeze stage.")
    try:
        return _build(args.vc_entitlement)
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
