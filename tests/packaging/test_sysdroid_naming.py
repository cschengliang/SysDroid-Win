"""Everything ships under one name: SysDroid. The old AndroidToolbox name survives only
where the data-folder migration needs it."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OLD_NAME = re.compile(r"android[ _-]?toolbox", re.IGNORECASE)
SKIPPED_DIRS = {".git", "lib", "tool", "build", "dist", "__pycache__", ".pytest_cache", ".venv", "venv"}
TEXT_SUFFIXES = {".py", ".spec", ".bat", ".md", ".txt", ".yml", ".yaml", ".html", ".svg", ".ini", ".json", ".cfg", ".toml"}
# Legacy folder / variable names that the migration and its docs must keep.
ALLOWED = {
    "src/sysdroid/core/data_dir.py", "tests/core/test_data_dir.py",
    "tests/packaging/test_sysdroid_naming.py", "README.md",
}
LEGACY_TOKENS = ("AndroidToolbox", "ANDROID_TOOLBOX_DATA_DIR")


def _text_files():
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in SKIPPED_DIRS for part in relative.parts[:-1]) or relative.parts[0] in SKIPPED_DIRS:
            continue
        if path.is_file() and (path.suffix.lower() in TEXT_SUFFIXES or path.name == ".gitignore"):
            yield relative.as_posix(), path


def test_old_name_only_remains_for_the_data_migration():
    offenders = []
    for relative, path in _text_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in OLD_NAME.finditer(text):
            if relative in ALLOWED and any(text.startswith(token, match.start()) for token in LEGACY_TOKENS):
                continue
            offenders.append(f"{relative}: {match.group(0)}")
    assert not offenders, offenders


def test_build_outputs_use_the_sysdroid_name():
    spec = (ROOT / "SysDroid.spec").read_text(encoding="utf-8")
    assert 'name="SysDroid",' in spec and 'name="SysDroid-win-x64",' in spec
    assert "icon=[str(exe_icon)]" in spec and "version=version_info" in spec
    workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    assert "name: SysDroid-win-x64-${{ steps.pkg.outputs.version }}" in workflow
    assert "scripts\\build_sysdroid.py" in workflow
    for launcher in ("sysdroid.py", "start_sysdroid.bat", "scripts/build_sysdroid.py"):
        assert (ROOT / launcher).is_file()
    import build_sysdroid
    assert build_sysdroid.RELEASE_NAME == "SysDroid-win-x64"
    assert build_sysdroid.BUILD_ROOT == ROOT / "build" / "sysdroid"
