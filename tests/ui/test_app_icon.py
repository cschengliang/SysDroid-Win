import struct
from pathlib import Path

from PySide6.QtCore import QSize

from sysdroid import app as app_module
from sysdroid import runtime_paths
from sysdroid.app_info import APP_USER_MODEL_ID
from sysdroid.ui import theme

ROOT = Path(__file__).resolve().parents[2]


def test_bundled_icon_has_every_size_and_loads_into_qicon(qapp):
    files = theme.app_icon_files()
    assert sorted(files) == list(theme.APP_ICON_SIZES)
    icon = theme.app_icon()
    assert not icon.isNull()
    available = {size.width() for size in icon.availableSizes()}
    assert set(theme.APP_ICON_SIZES) <= available
    for size in (16, 24, 32, 48):
        pixmap = icon.pixmap(QSize(size, size))
        assert pixmap.width() == size and not pixmap.toImage().isNull()


def test_missing_icon_files_give_an_empty_icon_instead_of_failing(qapp, tmp_path):
    assert theme.app_icon_files(tmp_path) == {}
    assert theme.app_icon(tmp_path).isNull()


def test_exe_icon_is_multi_size_with_a_png_256_frame():
    data = (ROOT / "assets" / "SysDroid.ico").read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    assert (reserved, kind) == (0, 1)
    frames = {}
    for index in range(count):
        width, _, _, _, _, bits, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * index)
        frames[width or 256] = (bits, data[offset:offset + length])
    assert sorted(frames) == list(theme.APP_ICON_SIZES)
    assert frames[256][1].startswith(b"\x89PNG\r\n\x1a\n")
    for size in (16, 24, 32, 48, 64, 128):
        bits, blob = frames[size]
        assert bits == 32 and struct.unpack_from("<I", blob)[0] == 40  # BITMAPINFOHEADER


def test_frozen_assets_live_under_the_pyinstaller_internal_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime_paths.sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    assert runtime_paths.assets_dir() == (tmp_path / "_internal").resolve() / "assets"
    monkeypatch.delattr(runtime_paths.sys, "frozen")
    assert runtime_paths.assets_dir() == ROOT / "assets"


def test_app_user_model_id_is_set_explicitly():
    calls = []

    class Shell32:
        def SetCurrentProcessExplicitAppUserModelID(self, value):
            calls.append(value)
            return 0

    assert app_module.set_app_user_model_id(Shell32()) is True
    assert calls == [APP_USER_MODEL_ID] and APP_USER_MODEL_ID == "cschengliang.SysDroid"

    class Broken:
        def SetCurrentProcessExplicitAppUserModelID(self, value):
            raise OSError("unsupported")

    assert app_module.set_app_user_model_id(Broken()) is False
