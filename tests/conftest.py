import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def runner(qapp, monkeypatch, tmp_path):
    import android_backend
    monkeypatch.setattr(android_backend, "DATA_DIR", tmp_path)
    instance = android_backend.TaskRunner()
    yield instance
    for task in instance.active():
        instance.cancel(task.id, force=True)
