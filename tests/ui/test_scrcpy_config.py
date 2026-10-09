import pytest

from sysdroid.ui.pages.scrcpy_page import ScrcpyConfig, build_scrcpy_args, parse_encoder_list, validate_recording_path


def test_record_only_suppresses_control_and_display_but_preserves_literal_path():
    path = "C:\\Recordings\\space ' $ literal.mp4"
    config = ScrcpyConfig(record_enabled=True, record_mode="only", record_path=path,
                          control_enabled=False, clipboard_sync=False, always_on_top=True,
                          fullscreen=True, borderless=True, orientation=90)
    args = build_scrcpy_args("serial", config)
    assert "--no-window" in args and "--no-playback" in args
    assert "--record=" + path in args
    assert not {"--no-control", "--no-clipboard-autosync", "--always-on-top", "--fullscreen", "--window-borderless", "--orientation=90"}.intersection(args)


def test_disabled_audio_ignores_audio_fields_but_active_audio_validates_them():
    disabled = ScrcpyConfig(audio_enabled=False, audio_source="invalid", audio_bitrate=0, audio_buffer=0)
    args = build_scrcpy_args("serial", disabled)
    assert "--no-audio" in args and not any(arg.startswith("--audio-") for arg in args)
    with pytest.raises(ValueError, match="音频来源"):
        build_scrcpy_args("serial", ScrcpyConfig(audio_enabled=True, audio_source="invalid"))


def test_recording_path_check_never_truncates_existing_file(tmp_path):
    path = tmp_path / "capture.mp4"
    path.write_bytes(b"existing recording")
    assert validate_recording_path(str(path)) == path
    assert path.read_bytes() == b"existing recording"
    with pytest.raises(ValueError, match="目录"):
        validate_recording_path(str(tmp_path / "missing" / "capture.mp4"))
    assert not (tmp_path / "missing").exists()


def test_encoder_list_groups_real_device_codecs_and_keeps_hardware_and_alias_details():
    output = """scrcpy 5.0
[server] INFO: List of video encoders:
    --video-codec=h264 --video-encoder=c2.vendor.avc.encoder     (hw) [vendor]
    --video-codec=h264 --video-encoder=c2.android.avc.encoder    (sw)
    --video-codec=h264 --video-encoder=OMX.vendor.avc           (hw) (alias for c2.vendor.avc.encoder)
    --video-codec=av1 --video-encoder=c2.android.av1.encoder     (sw)
    --video-codec=vp9 --video-encoder=c2.android.vp9.encoder     (sw)
[server] INFO: List of audio encoders:
    --audio-codec=opus --audio-encoder=c2.android.opus.encoder  (sw)
    --audio-codec=aac --audio-encoder=c2.android.aac.encoder    (sw)
INFO: Device disconnected
"""
    encoders = parse_encoder_list(output)
    assert encoders == {
        "video": {
            "h264": ["c2.vendor.avc.encoder (hw) [vendor]", "c2.android.avc.encoder (sw)",
                     "OMX.vendor.avc (hw) (alias for c2.vendor.avc.encoder)"],
            "av1": ["c2.android.av1.encoder (sw)"],
            "vp9": ["c2.android.vp9.encoder (sw)"],
        },
        "audio": {"opus": ["c2.android.opus.encoder (sw)"], "aac": ["c2.android.aac.encoder (sw)"]},
    }


def test_encoder_list_distinguishes_empty_audio_support_from_incomplete_output():
    output = """[server] INFO: List of video encoders:
    --video-codec=h264 --video-encoder=c2.vendor.avc.encoder (hw)
[server] INFO: List of audio encoders:
"""
    assert parse_encoder_list(output)["audio"] == {}
    with pytest.raises(ValueError):
        parse_encoder_list(output.split("[server] INFO: List of audio encoders:")[0])


@pytest.mark.parametrize("output", [
    "ERROR: Could not find any ADB device",
    "List of video encoders:\n--video-codec=h264\nList of audio encoders:",
    "List of video encoders:\n--audio-codec=aac --audio-encoder=c2.aac\nList of audio encoders:",
])
def test_invalid_encoder_output_is_not_reported_as_supported_or_empty(output):
    with pytest.raises(ValueError):
        parse_encoder_list(output)
