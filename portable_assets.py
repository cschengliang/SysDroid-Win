"""Verified redistributable assets and dependency closure for the portable build.

Only the caller's exclusive staging/release directories are written. Network access
is build-time only; a failed provenance, licensing, or dependency check aborts.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import mmap
import struct
import shutil
import subprocess
import tarfile
import zipfile

import requests
import pefile


TOOLS = (
    ("scrcpy-win64-v5.0", "5.0",
     "https://github.com/Genymobile/scrcpy/releases/download/v5.0/scrcpy-win64-v5.0.zip",
     "44c10d9e82f20ea67227d14d37bf9fbe3603117c5736df3f514544a02ba20a73"),
    ("terminal-1.25.2733.0", "1.25.2733.0",
     "https://api.github.com/repos/microsoft/terminal/releases/assets/604366415",
     "bf3ef2012f6c44d8340a4c58125acc9498d19b580f9890dc043cdf831852e796"),
)
VC_PATTERN = re.compile(r"^(?:vcruntime|msvcp|concrt|vcomp|vcamp|mfc|mfcm)\d.*\.dll$", re.I)
USER_DIRS = {"cache", "localstate", "roamingstate", "tempstate", "workspace", "__pycache__"}
USER_FILES = {"settings.json", "state.json", "commands.json", "history.json", "workspace.json", ".portable"}


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _fetch(url: str, cache: Path, expected: str | None = None) -> Path:
    """Use requests' workstation proxy/certificate configuration, never disable TLS."""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (hashlib.sha256(url.encode()).hexdigest() + "-" + url.split("/")[-1].split("?")[0])
    if target.is_file() and (expected is None or _sha(target) == expected):
        return target
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        headers = {"Accept": "application/octet-stream"} if "/releases/assets/" in url and url.startswith("https://api.github.com/") else None
        with requests.get(url, stream=True, headers=headers, timeout=(30, 180)) as response:
            response.raise_for_status()
            with temporary.open("wb") as stream:
                for chunk in response.iter_content(1024 * 1024):
                    stream.write(chunk)
        if expected and _sha(temporary) != expected:
            raise RuntimeError(f"Official asset SHA256 mismatch: {url}; expected {expected}, got {_sha(temporary)}")
        temporary.replace(target)
    except Exception as exc:
        raise RuntimeError(f"Required official asset unavailable: {url}: {exc}") from exc
    return target


def _github_file(repo: str, ref: str, name: str, cache: Path) -> tuple[bytes, str]:
    url = f"https://api.github.com/repos/{repo}/contents/{name}?ref={ref}"
    document = json.loads(_fetch(url, cache).read_text(encoding="utf-8"))
    if document.get("encoding") != "base64":
        raise RuntimeError(f"Official source file was not returned: {url}")
    return base64.b64decode(document["content"]), url


def _safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or any(":" in p for p in path.parts):
        raise RuntimeError(f"Unsafe archive member: {name}")
    return path


def _excluded(path: PurePosixPath) -> bool:
    return any(part.lower() in USER_DIRS for part in path.parts) or path.name.lower() in USER_FILES


def _tool(source: Path, release: Path, stage: Path, record: tuple) -> dict:
    name, version, url, digest = record
    archive = _fetch(url, stage / "asset-cache", digest)
    with zipfile.ZipFile(archive) as bundle:
        members = [(info, _safe_name(info.filename)) for info in bundle.infolist() if not info.is_dir()]
        # Both official assets may have a single wrapper directory; identify by executable.
        executable = "scrcpy.exe" if name.startswith("scrcpy") else "WindowsTerminal.exe"
        matches = [p.parent for _, p in members if p.name == executable]
        if len(matches) != 1:
            raise RuntimeError(f"Expected one {executable} in verified {url}")
        prefix = matches[0]
        official = {}
        for info, path in members:
            try:
                relative = path.relative_to(prefix)
            except ValueError:
                raise RuntimeError(f"Unexpected file outside official tool root: {path}")
            if not _excluded(relative):
                if relative.as_posix().casefold() in {p.casefold() for p in official}:
                    raise RuntimeError(f"Duplicate official tool member: {relative}")
                official[relative.as_posix()] = (info, hashlib.sha256(bundle.read(info)).hexdigest())
        local = source / "tool" / name
        local_hashes = {
            p.relative_to(local).as_posix(): _sha(p) for p in local.rglob("*")
            if p.is_file() and not _excluded(PurePosixPath(p.relative_to(local).as_posix()))
        } if local.is_dir() else {}
        same = local_hashes == {p: value[1] for p, value in official.items()}
        destination = release / "tool" / name
        if destination.exists():
            raise RuntimeError(f"Tool destination must be fresh staging directory: {destination}")
        for relative, (info, _) in official.items():
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            if same:
                shutil.copy2(local / relative, output)
            else:
                output.write_bytes(bundle.read(info))
    return {"name": name, "version": version, "source": url, "archive_sha256": digest,
            "local_files_match_official": same, "selected": "existing-verified" if same else "official-archive",
            "files": {f"tool/{name}/{p}": v[1] for p, v in official.items()}}


def _save_source_licenses(archive: Path, destination: Path) -> list[str]:
    """Copy actual source copyright/license/attribution files, not invented texts."""
    selected = []
    def save(name: str, data: bytes) -> None:
        relative = _safe_name(name)
        if len(relative.parts) > 1:
            relative = PurePosixPath(*relative.parts[1:])
        upper = relative.name.upper()
        if (upper.startswith(("LICENSE", "COPYING", "COPYRIGHT", "NOTICE", "AUTHORS"))
                or upper == "QT_ATTRIBUTION.JSON" or "LICENSES" in (p.upper() for p in relative.parts)):
            output = destination / str(relative)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
            selected.append(relative.as_posix())
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                upper = PurePosixPath(member.filename).name.upper()
                if not member.is_dir() and (upper.startswith(("LICENSE", "COPYING", "COPYRIGHT", "NOTICE", "AUTHORS")) or upper == "QT_ATTRIBUTION.JSON" or "/LICENSES/" in member.filename.upper()):
                    save(member.filename, bundle.read(member))
    else:
        with tarfile.open(archive, "r:*") as bundle:
            for member in bundle:
                if member.isfile():
                    upper = PurePosixPath(member.name).name.upper()
                    if upper.startswith(("LICENSE", "COPYING", "COPYRIGHT", "NOTICE", "AUTHORS")) or upper == "QT_ATTRIBUTION.JSON" or "/LICENSES/" in member.name.upper():
                        with bundle.extractfile(member) as stream:
                            save(member.name, stream.read())
    if not selected:
        raise RuntimeError(f"No actual licensing files in required source archive: {archive}")
    return selected


def _scrcpy_licenses(release: Path, stage: Path) -> list[dict]:
    records = []
    cache = stage / "asset-cache"
    licensing = release / "licenses" / "scrcpy-components"
    for dep in ("adb_windows", "sdl", "ffmpeg", "libusb", "dav1d"):
        script, script_url = _github_file("Genymobile/scrcpy", "v5.0", f"app/deps/{dep}.sh", cache)
        text = script.decode("utf-8")
        version_match = re.search(r"^VERSION=(\S+)$", text, re.M)
        source_match = re.search(r'^URL="([^"]+)"$', text, re.M)
        sha_match = re.search(r"^SHA256SUM=([0-9a-f]{64})$", text, re.M)
        if not all((version_match, source_match, sha_match)):
            raise RuntimeError(f"Cannot identify official Scrcpy dependency provenance: {script_url}")
        version = version_match[1]
        url = source_match[1].replace("$VERSION", version)
        archive = _fetch(url, cache, sha_match[1])
        folder = licensing / dep
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "official-build-recipe.sh").write_bytes(script)
        if dep == "adb_windows":
            with zipfile.ZipFile(archive) as bundle:
                notice = bundle.read("platform-tools/NOTICE.txt")
                (folder / "NOTICE.txt").write_bytes(notice)
                for name in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"):
                    expected = hashlib.sha256(bundle.read("platform-tools/" + name)).hexdigest()
                    actual = _sha(release / "tool/scrcpy-win64-v5.0" / name)
                    if expected != actual:
                        raise RuntimeError(f"Scrcpy ADB does not match its official build recipe: {name}")
            files = ["NOTICE.txt", "official-build-recipe.sh"]
        else:
            files = _save_source_licenses(archive, folder)
            shutil.copy2(archive, folder / ("corresponding-source" + (".tar.xz" if url.endswith(".tar.xz") else ".tar.gz")))
        records.append({"name": dep, "version": version, "source": url,
                        "archive_sha256": _sha(archive), "build_recipe": script_url, "licenses": files})
    return records


def _font_notices(path: Path) -> dict[str, list[str]]:
    """Extract original sfnt version/copyright/license records without font tooling."""
    data = path.read_bytes()
    count = struct.unpack_from(">H", data, 4)[0]
    name_offset = None
    for position in range(12, 12 + count * 16, 16):
        tag, _, offset, size = struct.unpack_from(">4sIII", data, position)
        if tag == b"name":
            if offset + size > len(data):
                raise RuntimeError(f"Truncated font name table: {path}")
            name_offset = offset
            break
    if name_offset is None:
        raise RuntimeError(f"Font has no original name metadata: {path}")
    _, count, strings = struct.unpack_from(">HHH", data, name_offset)
    records = {"copyright": [], "identity": [], "version": [], "license": [], "license_url": []}
    labels = {0: "copyright", 3: "identity", 5: "version", 13: "license", 14: "license_url"}
    for position in range(name_offset + 6, name_offset + 6 + count * 12, 12):
        platform, _, _, identifier, size, offset = struct.unpack_from(">6H", data, position)
        if identifier not in labels or platform not in (0, 1, 3):
            continue
        start = name_offset + strings + offset
        if start + size > len(data):
            raise RuntimeError(f"Truncated font metadata string: {path}")
        value = data[start:start + size].decode("mac_roman" if platform == 1 else "utf-16-be")
        values = records[labels[identifier]]
        if value not in values:
            values.append(value)
    if not all(records[key] for key in ("version", "copyright", "license")):
        raise RuntimeError(f"Font lacks actual version/copyright/license metadata: {path}")
    return records


def _terminal_licenses(release: Path, stage: Path) -> list[dict]:
    cache = stage / "asset-cache"
    destination = release / "licenses" / "Terminal"
    destination.mkdir(parents=True, exist_ok=True)
    root = release / "tool" / "terminal-1.25.2733.0"
    notice = root / "NOTICE.html"
    if not notice.is_file() or "copyright" not in notice.read_text(encoding="utf-8-sig").lower():
        raise RuntimeError("Verified Terminal distribution lacks actual third-party NOTICE")
    shutil.copy2(notice, destination / notice.name)
    license_text, license_url = _github_file("microsoft/terminal", "v1.25.2733.0", "LICENSE", cache)
    (destination / "LICENSE").write_bytes(license_text)
    fonts = {p.name: _sha(p) for p in root.glob("*.ttf")}
    if not fonts:
        raise RuntimeError("Terminal release contains no Cascadia fonts")
    terminal_archive = _fetch(TOOLS[1][2], cache, TOOLS[1][3])
    with zipfile.ZipFile(terminal_archive) as bundle:
        for name, digest in fonts.items():
            originals = [member for member in bundle.infolist() if PurePosixPath(member.filename).name == name]
            if len(originals) != 1 or hashlib.sha256(bundle.read(originals[0])).hexdigest() != digest:
                raise RuntimeError(f"Font differs from the pinned official Terminal distribution: {name}")
    notices = {p.name: _font_notices(p) for p in root.glob("*.ttf")}
    versions = {value for font in notices.values() for value in font["version"]}
    if len(versions) != 1 or not (match := re.fullmatch(r"Version (\d{4})\.(\d{3})", next(iter(versions)))):
        raise RuntimeError(f"Unrecognized or mixed actual Cascadia font versions: {versions}")
    version = f"v{match[1]}.{int(match[2]):02d}"
    font_license, font_license_url = _github_file("microsoft/cascadia-code", version, "LICENSE", cache)
    font_folder = destination / "Cascadia"
    font_folder.mkdir(parents=True, exist_ok=True)
    (font_folder / "LICENSE").write_bytes(font_license)
    _json(font_folder / "font-metadata.json", notices)
    font_licenses = ["LICENSE", "font-metadata.json"]
    return [{"name": "Windows Terminal", "version": "1.25.2733.0", "source": license_url,
             "licenses": ["LICENSE", "NOTICE.html"]},
            {"name": "Cascadia fonts", "version": version, "source": TOOLS[1][2],
             "archive_sha256": TOOLS[1][3], "files": {f"tool/terminal-1.25.2733.0/{name}": digest for name, digest in fonts.items()},
             "provenance": "Exact font hashes from pinned official Terminal archive; version and original license/copyright read from font metadata",
             "licenses": font_licenses, "license_source": font_license_url}]


def _python_licenses(release: Path, stage: Path) -> list[dict]:
    lock = stage / "requirements-runtime.txt"
    if not lock.is_file():
        raise RuntimeError("Missing exact runtime dependency lock: stage/requirements-runtime.txt")
    records = []
    for raw in lock.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line)
        if not match:
            raise RuntimeError(f"Runtime lock must contain exact distribution==version: {line}")
        name, version = match.groups()
        dist = metadata.distribution(name)
        if dist.version != version:
            raise RuntimeError(f"Runtime version changed for {name}: locked {version}, installed {dist.version}")
        folder = release / "licenses" / "python-packages" / name
        found = []
        for entry in dist.files or ():
            path = PurePosixPath(str(entry).replace("\\", "/"))
            if ".dist-info" not in str(path):
                continue
            if path.name.upper().startswith(("LICENSE", "COPYING", "COPYRIGHT", "NOTICE", "AUTHORS")) or "licenses" in path.parts:
                actual = Path(dist.locate_file(entry))
                if actual.is_file():
                    relative = PurePosixPath(*path.parts[1:])
                    output = folder / str(relative)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(actual, output)
                    found.append(relative.as_posix())
        if not found and name.lower().replace("_", "-") != "pyside6":
            raise RuntimeError(f"Runtime distribution has no installed license/NOTICE: {name} {version}")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "METADATA.txt").write_text(dist.read_text("METADATA") or "", encoding="utf-8")
        records.append({"name": name, "version": version, "source": dist.metadata.get_all("Project-URL") or dist.metadata.get("Home-page"), "licenses": found})
    qt_version = metadata.version("PySide6_Essentials")
    qt_sources = [("Qt", "qt/qtbase"), ("PySide-shiboken", "pyside/pyside-setup")]
    if any(p.name.casefold().startswith("qt6svg") for p in (release / "_internal").rglob("*.dll")):
        qt_sources.append(("QtSvg", "qt/qtsvg"))
    for name, repo in qt_sources:
        url = f"https://codeload.github.com/{repo}/tar.gz/refs/tags/v{qt_version}"
        archive = _fetch(url, stage / "asset-cache")
        licenses = _save_source_licenses(archive, release / "licenses" / name)
        if not any("LGPL" in p.upper() and "3" in p for p in licenses) or not any(re.search(r"(?:^|[/.])(?:COPYING[.-])?GPL[.-]?3", p, re.I) for p in licenses):
            raise RuntimeError(f"Official {name} {qt_version} source lacks required LGPLv3/GPLv3 texts")
        # Actual copyright-bearing source headers for the linked modules/bindings.
        headers = []
        with tarfile.open(archive, "r:*") as bundle:
            for member in bundle:
                if not member.isfile() or not member.name.endswith((".cpp", ".h", ".py")):
                    continue
                if not any(part in member.name for part in ("/src/", "/libpyside/", "/libshiboken/")):
                    continue
                with bundle.extractfile(member) as stream:
                    start = stream.read(4096).decode("utf-8", errors="replace")
                lines = [line for line in start.splitlines()[:50] if "copyright" in line.lower() or "SPDX-" in line]
                if lines:
                    headers.append({"source_file": member.name.split("/", 1)[-1], "notices": lines})
        if not headers:
            raise RuntimeError(f"No actual copyright source notices for {name} {qt_version}")
        _json(release / "licenses" / name / "source-copyrights.json", headers)
        source_copy = release / "licenses" / name / "corresponding-source.tar.gz"
        shutil.copy2(archive, source_copy)
        records.append({"name": name, "version": qt_version, "source": url, "archive_sha256": _sha(archive), "licenses": licenses,
                        "linkage": "Dynamic DLLs in _internal; replaceable. LGPLv3 terms and corresponding source reference supplied; no commercial entitlement claimed."})
    return records


def _system_dll(name: str, machine: int, cache: dict) -> str | None:
    if name.startswith(("api-ms-", "ext-ms-")):
        return "Windows 11 API-set contract"
    if VC_PATTERN.fullmatch(name):
        return None
    key = (name, machine)
    if key in cache:
        return cache[key]
    system = Path(os.environ.get("SystemRoot", "C:/Windows"))
    candidate = system / ("SysWOW64" if machine == 0x14C else "System32") / name
    result = None
    if candidate.is_file():
        try:
            with pefile.PE(str(candidate), fast_load=False) as pe:
                values = [value.decode("utf-8", errors="replace") for group in getattr(pe, "FileInfo", [])
                          for info in group for table in getattr(info, "StringTable", [])
                          for key, value in table.entries.items() if key == b"ProductName"]
                if pe.FILE_HEADER.Machine == machine and any("Windows" in v and "Operating System" in v for v in values):
                    result = str(candidate)
                elif pe.FILE_HEADER.Machine == machine:
                    # ICU and Windows Search carry their upstream product names,
                    # but are Windows components signed through the OS catalog.
                    powershell = system / "System32/WindowsPowerShell/v1.0/powershell.exe"
                    quoted = str(candidate).replace("'", "''")
                    command = (f"$s=Get-AuthenticodeSignature -LiteralPath '{quoted}'; "
                               "if($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject "
                               "-match '^CN=Microsoft Windows, O=Microsoft Corporation,'){exit 0};exit 1")
                    signature = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
                                               capture_output=True, timeout=30)
                    if signature.returncode == 0:
                        result = str(candidate)
        except pefile.PEFormatError:
            pass
    cache[key] = result
    return result


def _audit(release: Path) -> dict:
    records, missing, systems = [], [], {}
    all_files = list(release.rglob("*"))
    pes = [p for p in all_files if p.is_file() and p.suffix.lower() in {".exe", ".dll", ".pyd"}]
    index = {}
    for path in all_files:
        if path.is_file():
            index[(path.parent, path.name.casefold())] = path
    export_cache = {}

    def export_available(target: Path, symbol: bytes | int, machine: int,
                         search: list[Path], visiting: set) -> bool:
        key = (target, symbol)
        if key in visiting:
            return False
        if target not in export_cache:
            with pefile.PE(str(target), fast_load=True, max_symbol_exports=target.stat().st_size // 4) as dependency:
                if dependency.FILE_HEADER.Machine != machine:
                    raise RuntimeError(f"Wrong architecture dependency: {target}")
                dependency.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]])
                if any("export" in warning.casefold() for warning in dependency.get_warnings()):
                    raise RuntimeError(f"Incomplete PE export parsing: {target}: {dependency.get_warnings()}")
                exports = getattr(getattr(dependency, "DIRECTORY_ENTRY_EXPORT", None), "symbols", [])
                table = {}
                for exported in exports:
                    if not exported.address:
                        continue
                    table[exported.ordinal] = exported.forwarder
                    if exported.name:
                        table[exported.name] = exported.forwarder
                export_cache[target] = (dependency.FILE_HEADER.Machine, table)
        actual_machine, table = export_cache[target]
        if actual_machine != machine:
            raise RuntimeError(f"Wrong architecture dependency: {target}")
        if symbol not in table:
            return False
        forwarder = table[symbol]
        if not forwarder:
            return True
        module, separator, exported = forwarder.decode("ascii").rpartition(".")
        if not separator or not module or not exported:
            raise RuntimeError(f"Malformed PE export forwarder: {target}: {forwarder!r}")
        name = module.casefold()
        if not name.endswith(".dll"):
            name += ".dll"
        forwarded_symbol = int(exported[1:]) if exported.startswith("#") else exported.encode("ascii")
        roots = [target.parent, *search]
        dependency = next((index[(directory, name)] for directory in roots if (directory, name) in index), None)
        row = {"importer": target.relative_to(release).as_posix(), "machine": machine,
               "kind": "forwarder", "name": name, "export": repr(symbol), "forwarder": forwarder.decode("ascii")}
        if dependency:
            available = export_available(dependency, forwarded_symbol, machine, roots, visiting | {key})
            row.update(classification="app-local" if available else "missing-symbols",
                       resolved=dependency.relative_to(release).as_posix())
            if not available:
                row["missing_symbols"] = [exported]
        elif system := _system_dll(name, machine, systems):
            available = True
            row.update(classification="Windows-system", resolved=system)
        else:
            available = False
            row.update(classification="missing")
        records.append(row)
        if not available:
            missing.append(row)
        return available

    for path in pes:
        try:
            with pefile.PE(str(path), fast_load=True) as pe:
                # pefile counts ILT and IAT traversals together; Qt bindings exceed
                # its malware-oriented default. Keep parsing bounded by file bytes.
                import_limit = pefile.MAX_IMPORT_SYMBOLS
                try:
                    pefile.MAX_IMPORT_SYMBOLS = max(import_limit, path.stat().st_size)
                    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"], pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
                finally:
                    pefile.MAX_IMPORT_SYMBOLS = import_limit
                if any("import" in warning.casefold() for warning in pe.get_warnings()):
                    raise RuntimeError(f"Incomplete PE import parsing: {path}: {pe.get_warnings()}")
                machine = pe.FILE_HEADER.Machine
                if machine not in (0x8664, 0x14C):
                    raise RuntimeError(f"Unsupported PE machine {machine:#x}: {path}")
                groups = [("normal", getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])), ("delay", getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", []))]
                relative = path.relative_to(release)
                if relative.parts[0] == "tool":
                    app = release / "tool" / relative.parts[1]
                    search = [path.parent, app]
                else:
                    internal = release / "_internal"
                    search = [path.parent, release, internal, internal / "PySide6", internal / "shiboken6", internal / "PIL"]
                for kind, entries in groups:
                    for entry in entries:
                        name = entry.dll.decode("ascii").casefold()
                        target = next((index[(directory, name)] for directory in search if (directory, name) in index), None)
                        row = {"importer": relative.as_posix(), "machine": machine, "kind": kind, "name": name}
                        if target:
                            absent = [symbol.name.decode("ascii", errors="replace") if symbol.name else f"ordinal:{symbol.ordinal}"
                                      for symbol in entry.imports
                                      if not export_available(target, symbol.name if symbol.name else symbol.ordinal,
                                                              machine, search, set())]
                            if absent:
                                row["missing_symbols"] = absent
                                missing.append(row)
                            row.update(classification="missing-symbols" if absent else "app-local",
                                       resolved=target.relative_to(release).as_posix())
                        elif system := _system_dll(name, machine, systems):
                            row.update(classification="Windows-system", resolved=system)
                        else:
                            row.update(classification="missing")
                            missing.append(row)
                        records.append(row)
        except pefile.PEFormatError as exc:
            raise RuntimeError(f"Invalid required PE asset: {path}: {exc}") from exc
    return {"platform": "Windows 11 x64", "imports": records, "missing": missing,
            "policy": "Normal/delay imports and transitive export forwarders by name/ordinal; application-local search roots only. VC runtime is never accepted from host system directories. API sets are OS contracts. Windows system DLLs require OS product provenance or a trusted Microsoft Windows component signature.",
            "limitations": ["Static import closure does not prove runtime LoadLibrary/plugin behavior; main integration must exercise Qt, Pillow and external tools.", "System prerequisites include Windows 11 API/UCRT and user-installed device drivers; no driver installation is performed."]}


def _stage_vc(release: Path, stage: Path, missing: list[dict]) -> dict:
    if any(row["machine"] != 0x8664 for row in missing):
        raise RuntimeError(f"Official x64 VC redist cannot satisfy non-x64 imports: {missing}")
    entitlement_path = stage / "vc-license-entitlement.json"
    if not entitlement_path.is_file():
        raise RuntimeError("Microsoft VC redistribution requires an eligible licensed Visual Studio user. Supply stage/vc-license-entitlement.json with edition, licensing_basis, and license_terms_accepted=true; the runtime use EULA alone does not grant redistribution rights. See https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution")
    entitlement = json.loads(entitlement_path.read_text(encoding="utf-8"))
    if entitlement.get("license_terms_accepted") is not True or not entitlement.get("edition") or not entitlement.get("licensing_basis"):
        raise RuntimeError("Microsoft VC redistribution entitlement must record actual edition, licensing basis, and acceptance; no entitlement may be fabricated")
    url = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    installer = _fetch(url, stage / "asset-cache")
    # Verify publisher with the Windows trust provider before executing Microsoft's installer.
    powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = f"$s=Get-AuthenticodeSignature -LiteralPath '{str(installer).replace(chr(39), chr(39) * 2)}'; if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation'){{exit 1}}; $s.SignerCertificate.Subject"
    result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"Microsoft VC installer publisher verification failed: {result.stderr or result.stdout}")
    publisher = result.stdout.strip()
    # Burn /layout may only copy the EXE. Extract its verified embedded CAB
    # containers without installing anything or requiring third-party software.
    layout = stage / "vc-redist-layout"
    layout.mkdir(parents=True, exist_ok=False)
    cabinets = []
    with installer.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        offset = 0
        while (offset := data.find(b"MSCF", offset)) >= 0:
            if offset + 36 <= len(data):
                size = struct.unpack_from("<I", data, offset + 8)[0]
                reserved = [struct.unpack_from("<I", data, offset + n)[0] for n in (4, 12, 20)]
                folders, files = struct.unpack_from("<HH", data, offset + 26)
                if not any(reserved) and 36 <= size <= len(data) - offset and data[offset + 24:offset + 26] == b"\x03\x01" and 0 < folders < 10000 and 0 < files < 100000:
                    cabinet = layout / f"embedded-{len(cabinets)}.cab"
                    cabinet.write_bytes(data[offset:offset + size])
                    cabinets.append(cabinet)
            offset += 4
    if not cabinets:
        raise RuntimeError("Official Microsoft runtime installer contains no valid CAB payloads")
    expand = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/expand.exe"
    number = 0
    while number < len(cabinets):
        cabinet = cabinets[number]
        extracted = stage / f"vc-cab-{number}"
        extracted.mkdir()
        result = subprocess.run([str(expand), "-F:*", str(cabinet), str(extracted)], capture_output=True, timeout=120)
        if result.returncode:
            raise RuntimeError(f"Official VC cabinet extraction failed: {cabinet}: {result.stderr!r}")
        for member in extracted.rglob("*"):
            if member.is_file():
                with member.open("rb") as stream:
                    if stream.read(4) == b"MSCF":
                        cabinets.append(member)
        number += 1
    dlls = {}
    # CAB member names may be MSI identifiers rather than original filenames;
    # resolve OriginalFilename from the signed Microsoft PE version resource.
    for path in stage.glob("vc-cab-*/*"):
        if not path.is_file():
            continue
        try:
            with pefile.PE(str(path)) as pe:
                if pe.FILE_HEADER.Machine != 0x8664:
                    continue
                for group in getattr(pe, "FileInfo", []):
                    for info in group:
                        for table in getattr(info, "StringTable", []):
                            original = table.entries.get(b"OriginalFilename", b"").decode("ascii", errors="ignore").casefold()
                            if VC_PATTERN.fullmatch(original):
                                dlls[original] = path
        except pefile.PEFormatError:
            continue
    license_url = "https://visualstudio.microsoft.com/wp-content/uploads/2021/09/Visual-C-Runtime-2015-2022-License-1.docx"
    license_document = _fetch(license_url, stage / "asset-cache")
    with zipfile.ZipFile(license_document) as document:
        license_xml = document.read("word/document.xml").decode("utf-8")
    if "MICROSOFT" not in license_xml.upper() or "RUNTIME" not in license_xml.upper():
        raise RuntimeError("Official VC runtime license lacks expected product terms")
    redist_url = "https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution"
    redist_list = _fetch(redist_url, stage / "asset-cache")
    if "licensed Visual Studio users" not in redist_list.read_text(encoding="utf-8"):
        raise RuntimeError("Official Microsoft redistribution list lacks expected licensing conditions")
    licenses = [license_document, redist_list]
    folder = release / "licenses" / "Microsoft-VC-runtime"
    folder.mkdir(parents=True, exist_ok=True)
    _json(folder / "licensed-builder-basis.json", entitlement)
    for number, license_path in enumerate(licenses):
        shutil.copy2(license_path, folder / f"{number}-{license_path.name}")
    copied, versions = {}, {}
    targets = {(release / row["importer"]).parent / row["name"] for row in missing}
    targets.update(p for p in release.rglob("*.dll") if VC_PATTERN.fullmatch(p.name))
    pending = list(targets)
    staged = set()
    while pending:
        destination = pending.pop()
        if destination in staged:
            continue
        staged.add(destination)
        name = destination.name.casefold()
        if name not in dlls:
            raise RuntimeError(f"Required VC DLL absent in official redistributable: {name}")
        shutil.copy2(dlls[name], destination)
        copied[destination.relative_to(release).as_posix()] = _sha(destination)
        with pefile.PE(str(destination), fast_load=False) as pe:
            fixed = pe.VS_FIXEDFILEINFO[0]
            versions[name] = f"{fixed.FileVersionMS >> 16}.{fixed.FileVersionMS & 65535}.{fixed.FileVersionLS >> 16}.{fixed.FileVersionLS & 65535}"
            for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) + getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", []):
                dependency = entry.dll.decode("ascii").casefold()
                if VC_PATTERN.fullmatch(dependency):
                    pending.append(destination.parent / dependency)
    return {"name": "Microsoft Visual C++ runtime", "source": url, "archive_sha256": _sha(installer),
            "publisher": publisher, "files": copied, "dll_versions": versions, "license_source": license_url,
            "version": "PE version resources in redistributed DLLs; official VS17 redist", "licenses": [p.name for p in folder.iterdir()]}


def stage_portable_assets(source_root: Path, release_root: Path, stage: Path) -> dict:
    """Stage officially verified resources/licenses and require closed PE imports.

    ``stage/requirements-runtime.txt`` is supplied by the build orchestrator.
    Python and PyInstaller license files are prepared by that orchestrator in
    ``release_root/licenses`` before this call. No source/worktree asset is edited.
    """
    source_root, release_root, stage = map(lambda p: Path(p).resolve(), (source_root, release_root, stage))
    if release_root == source_root or stage == source_root or source_root in release_root.parents and "tool" in release_root.relative_to(source_root).parts:
        raise ValueError("Asset staging must not target the source/tool worktree")
    components = [_tool(source_root, release_root, stage, record) for record in TOOLS]
    components.extend(_scrcpy_licenses(release_root, stage))
    # Preserve the existing adbutils hook, but give its otherwise independent
    # fallback binaries the same verified official provenance as the primary ADB.
    primary_adb = release_root / "tool/scrcpy-win64-v5.0"
    internal_adb = []
    for binary in (release_root / "_internal").rglob("adb.exe"):
        for name in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"):
            target = binary.parent / name
            shutil.copy2(primary_adb / name, target)
            internal_adb.append(target.relative_to(release_root).as_posix())
    if internal_adb:
        components.append({"name": "adbutils bundled ADB", "version": "37.0.1",
                           "source": "Same hash-verified Google platform-tools archive as Scrcpy's official build recipe",
                           "files": {name: _sha(release_root / name) for name in internal_adb}})
    components.extend(_terminal_licenses(release_root, stage))
    components.extend(_python_licenses(release_root, stage))
    for name in ("Python", "PyInstaller"):
        folder = release_root / "licenses" / name
        if not folder.is_dir() or not any(p.is_file() for p in folder.rglob("*")):
            raise RuntimeError(f"Build orchestrator must supply actual {name} license files before asset staging")
    for name, _, _, _ in TOOLS:
        tool = release_root / "tool" / name
        destination = release_root / "licenses" / name
        destination.mkdir(parents=True, exist_ok=True)
        for path in tool.rglob("*"):
            if path.is_file() and path.name.upper().startswith(("LICENSE", "NOTICE", "COPYING")):
                target = destination / path.relative_to(tool)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    # Existing vendor-provided runtimes are a different redistribution path
    # from extracting Microsoft's standalone VS redistributable. Preserve them
    # and record their exact provider hashes; do not impose a VS-license claim
    # or replace already closed dependencies with a newly extracted runtime.
    provider_files = {}
    interpreter = source_root / "lib/python-3.14.8-embed-amd64"
    for path in interpreter.glob("*.dll"):
        if VC_PATTERN.fullmatch(path.name):
            provider_files.setdefault(_sha(path), []).append({"provider": "CPython Windows embedded distribution", "file": str(path), "license": "licenses/Python/LICENSE.txt, Microsoft Distributable Code section"})
    for package in ("PySide6_Essentials", "shiboken6"):
        dist = metadata.distribution(package)
        for entry in dist.files or ():
            path = Path(dist.locate_file(entry))
            if path.is_file() and VC_PATTERN.fullmatch(path.name):
                provider_files.setdefault(_sha(path), []).append({"provider": f"{package} {dist.version}", "file": str(entry), "license": f"licenses/python-packages/{package}; corresponding official Qt/PySide source notices"})
    for component in components[:2]:
        for name, digest in component["files"].items():
            if VC_PATTERN.fullmatch(PurePosixPath(name).name):
                provider_files.setdefault(digest, []).append({"provider": component["name"], "file": name,
                                                            "source": component["source"], "archive_sha256": component["archive_sha256"],
                                                            "license": "Original verified official distribution licenses/NOTICE preserved"})
    runtime_files = {}
    for path in release_root.rglob("*.dll"):
        if not VC_PATTERN.fullmatch(path.name):
            continue
        digest = _sha(path)
        if digest not in provider_files:
            raise RuntimeError(f"Existing VC runtime has no matching actual interpreter/wheel provider provenance: {path}")
        runtime_files[path.relative_to(release_root).as_posix()] = {"sha256": digest, "providers": provider_files[digest]}
    if runtime_files:
        components.append({"name": "Vendor-bundled Microsoft runtime", "files": runtime_files,
                           "redistribution_paths": "CPython LICENSE Microsoft Distributable Code clauses and original package license/third-party notices; not newly extracted from VS REDIST. Preserve all original notices and Microsoft-platform restrictions."})
    audit = _audit(release_root)
    vc_missing = [row for row in audit["missing"] if VC_PATTERN.fullmatch(row["name"])]
    if vc_missing:
        replacement = _stage_vc(release_root, stage, vc_missing)
        for component in components:
            files = component.get("files", {})
            for name in tuple(files):
                if name not in replacement["files"]:
                    continue
                original = files[name]
                digest = original["sha256"] if isinstance(original, dict) else original
                if digest != replacement["files"][name]:
                    component.setdefault("replaced_files", {})[name] = {
                        "original": files.pop(name), "replacement_sha256": replacement["files"][name],
                        "replacement_provider": replacement["name"]}
        components.append(replacement)
        audit = _audit(release_root)
    audit_path = release_root / "licenses" / "pe-audit.json"
    _json(audit_path, audit)
    if audit["missing"]:
        raise RuntimeError("Portable PE dependency closure failed; required imports: " + json.dumps(audit["missing"], ensure_ascii=False))
    (release_root / "THIRD-PARTY-NOTICES.txt").write_text(
        "Android Toolbox uses the Qt, PySide6 and Shiboken libraries under the applicable open-source LGPLv3 terms.\n"
        "No commercial Qt entitlement is asserted. LGPLv3 and GPLv3 texts, original copyright notices, and\n"
        "corresponding tagged source archives are in licenses/Qt, licenses/PySide-shiboken and licenses/QtSvg.\n"
        "The dynamically linked libraries remain replaceable under _internal; close the application and preserve\n"
        "compatible names, architecture and ABI when installing your modified versions. This distribution adds\n"
        "no restriction on modifying/replacing LGPL libraries or reverse engineering to debug those modifications.\n"
        "Scrcpy and its FFmpeg/SDL/libusb/dav1d dependencies have their original notices, exact source archives\n"
        "and upstream build recipes in licenses/scrcpy-components. Terminal and Cascadia notices are in licenses/Terminal.\n"
        "Python, PyInstaller (bootloader exception), Python package licenses and supplier-provided Microsoft runtime\n"
        "notices are preserved under licenses. Microsoft-specific platform restrictions remain applicable.\n"
        "licenses/components.json records versions, official provenance and hashes; licenses/pe-audit.json records\n"
        "normal/delay imports and forwarded-export closure. These records are not a substitute for license terms.\n"
        "User device drivers and Windows 11 system/API/UCRT components are prerequisites, not packaged assets.\n",
        encoding="utf-8")
    license_files = {p.relative_to(release_root).as_posix(): _sha(p) for p in (release_root / "licenses").rglob("*") if p.is_file()}
    manifest = release_root / "licenses" / "components.json"
    release_hashes = {p.relative_to(release_root).as_posix(): _sha(p) for p in release_root.rglob("*") if p.is_file() and p != manifest}
    _json(manifest, {"components": components, "license_file_sha256": license_files,
                     "release_file_sha256": release_hashes,
                     "source_policy": "Official matching-version distributions and source licenses; archives SHA256 recorded. Scrcpy/Terminal binaries checked against pinned official hashes. No commercial Qt entitlement claimed.",
                     "platform": "Windows 11 x64"})
    return {"components": components, "manifest": str(manifest), "pe_audit": str(audit_path)}
