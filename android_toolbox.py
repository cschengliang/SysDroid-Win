"""Source launcher for SysDroid.

Kept at the repository root so start_android_toolbox.bat, the README commands and the
PyInstaller spec keep a stable entry point; the application lives in src/sysdroid.
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
