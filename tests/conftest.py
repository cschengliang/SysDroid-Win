import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Application package (src/sysdroid) and build tooling (scripts/) are not on sys.path by default.
for _path in (ROOT / "scripts", ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


@pytest.fixture
def runner(qapp, monkeypatch, tmp_path):
    from sysdroid.core import backend as android_backend
    monkeypatch.setattr(android_backend, "DATA_DIR", tmp_path)
    instance = android_backend.TaskRunner()
    yield instance
    for task in instance.active():
        instance.cancel(task.id, force=True)
