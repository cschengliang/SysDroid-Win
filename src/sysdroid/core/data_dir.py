"""Per-user data folder and the one-time move from the AndroidToolbox name.

Default: %LOCALAPPDATA%\\SysDroid. SYSDROID_DATA_DIR overrides it; the old
ANDROID_TOOLBOX_DATA_DIR is still honoured as a fallback so existing setups keep
their folder. Without an override, data left in %LOCALAPPDATA%\\AndroidToolbox is
copied (never moved or deleted) into the new folder once, recorded by a marker file.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil

APP_DIR_NAME = "SysDroid"
LEGACY_DIR_NAME = "AndroidToolbox"
ENV_VAR = "SYSDROID_DATA_DIR"
LEGACY_ENV_VAR = "ANDROID_TOOLBOX_DATA_DIR"
MIGRATION_MARKER = ".migrated-from-AndroidToolbox.json"
# Transient per-run output, not worth carrying over.
SKIPPED_ENTRIES = frozenset({"exec-out"})


def _override(environ: Mapping[str, str]) -> str:
    return environ.get(ENV_VAR) or environ.get(LEGACY_ENV_VAR) or ""


def _local_app_data(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("LOCALAPPDATA") or str(Path.home()))


def resolve_data_dir(environ: Mapping[str, str] = os.environ) -> Path:
    override = _override(environ)
    return Path(override).expanduser() if override else _local_app_data(environ) / APP_DIR_NAME


def legacy_data_dir(environ: Mapping[str, str] = os.environ) -> Path | None:
    """The AndroidToolbox folder to migrate from, or None when the user picked a folder explicitly."""
    return None if _override(environ) else _local_app_data(environ) / LEGACY_DIR_NAME


def migrate_legacy_data(target: Path, legacy: Path | None) -> str:
    """Copy legacy data into target once.

    Files that already exist in target win. Returns "none" (nothing to migrate),
    "already" (marker present), "migrated" or "failed" (no marker written, so the
    next start retries; the legacy folder is never modified).
    """
    if legacy is None or not legacy.is_dir() or legacy.resolve() == target.resolve():
        return "none"
    marker = target / MIGRATION_MARKER
    if marker.exists():
        return "already"
    copied = []
    try:
        target.mkdir(parents=True, exist_ok=True)
        for source in sorted(legacy.rglob("*")):
            relative = source.relative_to(legacy)
            if relative.parts[0] in SKIPPED_ENTRIES or source.is_symlink() or not source.is_file():
                continue
            destination = target / relative
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append(relative.as_posix())
        marker.write_text(json.dumps({
            "from": str(legacy), "copied": copied,
            "migrated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return "failed"
    return "migrated"
