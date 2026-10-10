"""Source launcher for SysDroid.

Kept at the repository root so start_sysdroid.bat and the README commands have a
stable entry point; the application lives in src/sysdroid. The frozen EXE starts from
src/sysdroid/__main__.py instead, because this file's name shadows the package.
"""
import sys
from pathlib import Path

# The embedded interpreter ignores PYTHONPATH and does not add the script directory to
# sys.path, so make src/ importable explicitly (absent, and harmless, in the frozen app).
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sysdroid.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
