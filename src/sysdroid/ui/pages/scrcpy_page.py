from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QStandardPaths, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from sysdroid.core.backend import OUTPUT_LIMIT, TRUNCATED, STATUS_LABELS, TERMINAL_STATUSES, Task, TaskRunner, powershell_command
from sysdroid.ui import kit as ui_kit
@dataclass(frozen=True)
class ScrcpyConfig:
    max_size: int = 1080
    max_fps: int = 60
    video_bitrate: int = 8
    video_codec: str = "h264"
    video_buffer: int = 0
    orientation: int = 0
    audio_enabled: bool = True
    audio_source: str = "output"
    audio_bitrate: int = 128
    audio_buffer: int = 50
    control_enabled: bool = True
    clipboard_sync: bool = True
    always_on_top: bool = False
    fullscreen: bool = False
    borderless: bool = False
    record_enabled: bool = False
    record_path: str = "scrcpy-recording.mp4"
    record_format: str = "mp4"
    record_mode: str = "mirror"
    video_source: str = "display"
    camera_facing: str = "back"
    display_id: int = 0
    new_display: bool = False
    new_display_spec: str = ""
    crop: str = ""
    start_app: str = ""
    turn_screen_off: bool = False
    stay_awake: bool = False
    show_touches: bool = False
    keyboard_uhid: bool = False
    record_timestamp: bool = True

    @property
    def record_only(self) -> bool:
        return self.record_enabled and self.record_mode == "only"

    @property
    def device_control(self) -> bool:
        """Scrcpy keeps control enabled in record-only mode (no --no-control)."""
        return self.record_only or self.control_enabled


def build_scrcpy_args(serial: str, config: ScrcpyConfig) -> list[str]:
    """Return literal argv; PowerShell quoting belongs only in the preview."""
    if not serial or not serial.strip():
        raise ValueError("请先在顶部选择在线设备。")
    if "\0" in serial:
        raise ValueError("设备序列号不能包含空字符。")
    choices = (
        (config.max_size, (0, 720, 1080, 1280, 1920), "最长边限制"),
        (config.max_fps, (0, 30, 60, 90, 120), "帧率上限"),
        (config.video_codec, ("h264", "h265", "av1"), "视频编码"),
        (config.orientation, (0, 90, 180, 270), "显示方向"),
    )
    for value, allowed, label in choices:
        if value not in allowed:
            raise ValueError(f"{label}的取值无效。")
    ranges = ((config.video_bitrate, 1, 100, "视频码率"), (config.video_buffer, 0, 2000, "视频缓冲"))
    if config.audio_enabled:
        if config.audio_source not in ("output", "mic"):
            raise ValueError("音频来源的取值无效。")
        ranges += ((config.audio_bitrate, 32, 512, "音频码率"), (config.audio_buffer, 10, 1000, "音频缓冲"))
    for value, minimum, maximum, label in ranges:
        if not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{label}必须在 {minimum}–{maximum} 之间。")
    if config.record_enabled:
        if not config.record_path.strip() or "\0" in config.record_path:
            raise ValueError("启用录制后需要填写有效的输出文件。")
        if config.record_format not in ("mp4", "mkv") or config.record_mode not in ("mirror", "only"):
            raise ValueError("录制格式或模式无效。")
    camera = config.video_source == "camera"
    if config.video_source not in ("display", "camera"):
        raise ValueError("视频来源的取值无效。")
    if not isinstance(config.display_id, int) or not 0 <= config.display_id <= 9999:
        raise ValueError("显示屏 ID 必须在 0–9999 之间。")
    crop = config.crop.strip()
    new_display_spec = config.new_display_spec.strip()
    start_app = config.start_app.strip()
    if camera:
        if config.camera_facing not in ("back", "front", "external"):
            raise ValueError("摄像头朝向的取值无效。")
        if config.display_id or config.new_display or crop:
            raise ValueError("摄像头来源不能同时使用显示屏 ID、新建虚拟显示屏或裁剪。")
    if config.new_display:
        if config.display_id:
            raise ValueError("新建虚拟显示屏时不能同时指定显示屏 ID。")
        if not re.fullmatch(r"(?:\d{2,5}x\d{2,5})?(?:/\d{2,4})?", new_display_spec):
            raise ValueError("虚拟显示屏参数格式应为 宽x高、宽x高/DPI 或 /DPI，留空使用主屏尺寸。")
    if crop and not re.fullmatch(r"\d{1,5}:\d{1,5}:\d{1,5}:\d{1,5}", crop):
        raise ValueError("裁剪区域格式应为 宽:高:X:Y（设备自然方向的像素）。")
    if start_app and (re.search(r"[\0\r\n]", start_app) or not re.fullmatch(r"\+?\??[^\s+?].*", start_app)):
        raise ValueError("启动应用应填写包名；前缀 + 表示先强制停止，? 表示按应用名称匹配。")

    args = [f"--serial={serial}"]
    if config.max_size:
        args.append(f"--max-size={config.max_size}")
    if config.max_fps:
        args.append(f"--max-fps={config.max_fps}")
    args.extend((f"--video-bit-rate={config.video_bitrate}M", f"--video-codec={config.video_codec}"))
    if camera:
        args.extend(("--video-source=camera", f"--camera-facing={config.camera_facing}"))
    elif config.new_display:
        args.append("--new-display" + (f"={new_display_spec}" if new_display_spec else ""))
    elif config.display_id:
        args.append(f"--display-id={config.display_id}")
    if crop and not camera:
        args.append(f"--crop={crop}")
    if config.video_buffer:
        args.append(f"--video-buffer={config.video_buffer}")
    if config.orientation and not config.record_only:
        args.append(f"--orientation={config.orientation}")
    if config.audio_enabled:
        args.extend((f"--audio-source={config.audio_source}", f"--audio-bit-rate={config.audio_bitrate}K", f"--audio-buffer={config.audio_buffer}"))
    else:
        args.append("--no-audio")
    if config.record_only:
        args.extend(("--no-playback", "--no-window"))
    else:
        if not config.control_enabled:
            args.append("--no-control")
        elif not config.clipboard_sync:
            args.append("--no-clipboard-autosync")
        for enabled, flag in ((config.always_on_top, "--always-on-top"), (config.fullscreen, "--fullscreen"), (config.borderless, "--window-borderless")):
            if enabled:
                args.append(flag)
        if config.control_enabled and config.keyboard_uhid and not camera:
            args.append("--keyboard=uhid")
    if config.device_control:
        # Scrcpy rejects these when control is disabled (--no-control).
        for enabled, flag in ((config.turn_screen_off, "--turn-screen-off"), (config.stay_awake, "--stay-awake"), (config.show_touches, "--show-touches")):
            if enabled:
                args.append(flag)
        if start_app:
            args.append(f"--start-app={start_app}")
    if config.record_enabled:
        args.extend((f"--record={config.record_path}", f"--record-format={config.record_format}"))
    return args


def config_to_dict(config: ScrcpyConfig) -> dict[str, object]:
    return asdict(config)


def config_from_dict(data: object) -> ScrcpyConfig:
    """Rebuild a config from saved JSON, keeping defaults for unknown or mistyped values."""
    if not isinstance(data, dict):
        return ScrcpyConfig()
    defaults = ScrcpyConfig()
    values: dict[str, object] = {}
    for field in fields(ScrcpyConfig):
        value = data.get(field.name)
        default = getattr(defaults, field.name)
        # bool is a subclass of int, so compare exact types.
        if type(value) is type(default):
            values[field.name] = value
    return replace(defaults, **values)


def timestamped_recording_path(filename: str, serial: str, now: datetime) -> str:
    """Append the device and start time so sessions never overwrite each other."""
    path = Path(filename)
    device = re.sub(r"[^A-Za-z0-9._-]+", "_", serial).strip("_.") or "device"
    return str(path.with_name(f"{path.stem}-{device}-{now:%Y%m%d-%H%M%S}{path.suffix}"))


def validate_recording_path(filename: str) -> Path:
    """Check an output location without creating or truncating the recording."""
    if not filename.strip() or "\0" in filename:
        raise ValueError("请填写有效的录制输出文件。")
    if os.name == "nt" and os.path.isreserved(filename):
        raise ValueError("录制路径包含 Windows 不允许的文件名或字符。")
    path = Path(os.path.abspath(filename))
    if not path.parent.is_dir():
        raise ValueError(f"录制目录不存在或不是目录：{path.parent}")
    if path.exists() and not path.is_file():
        raise ValueError(f"录制路径不是普通文件：{path}")
    if path.exists() and not os.access(path, os.W_OK):
        raise ValueError(f"现有录制文件不可写：{path}")
    if path.exists():
        try:
            with path.open("r+b"):
                pass
        except OSError as exc:
            raise ValueError(f"现有录制文件无法写入：{path}\n{exc}") from exc
    try:
        # A disposable sibling checks directory permissions without touching an
        # existing recording. The context manager closes and removes it.
        with tempfile.NamedTemporaryFile(prefix=".android-toolbox-", dir=path.parent):
            pass
    except OSError as exc:
        raise ValueError(f"录制目录不可写：{path.parent}\n{exc}") from exc
    return path


def parse_encoder_list(output: str) -> dict[str, dict[str, list[str]]]:
    """Group Scrcpy's device encoder listing by media and codec, retaining details."""
    encoders: dict[str, dict[str, list[str]]] = {"video": {}, "audio": {}}
    sections: set[str] = set()
    section = ""
    for line in output.splitlines():
        heading = re.search(r"List of (video|audio) encoders:\s*$", line)
        if heading:
            section = heading[1]
            sections.add(section)
            continue
        entry = re.search(r"--(video|audio)-codec=(\S+)\s+--\1-encoder=(\S+)(.*)", line)
        if entry:
            media, codec, name, details = entry.groups()
            if media != section:
                raise ValueError("编码器记录与列表分区不匹配")
            encoders[media].setdefault(codec, []).append(name + (" " + details.strip() if details.strip() else ""))
        elif section and re.search(r"--(?:video|audio)-(?:codec|encoder)=", line):
            raise ValueError("无法解析设备返回的编码器记录")
    if sections != {"video", "audio"}:
        raise ValueError("Scrcpy 未返回完整的视频和音频编码列表")
    return encoders


class ScrcpyPage(QWidget):
    """Configure one external Scrcpy process; settings affect the next launch."""

    show_output = Signal(str)

    PROFILES = {
        "balanced": (1080, 60, 8, True),
        "smooth": (720, 60, 4, False),
        "quality": (1920, 60, 16, True),
    }

    COMBO_FIELDS = ("max_size", "max_fps", "video_codec", "orientation", "audio_source", "record_format", "record_mode", "video_source", "camera_facing")
    SPIN_FIELDS = ("video_bitrate", "video_buffer", "audio_bitrate", "audio_buffer", "display_id")
    CHECK_FIELDS = ("audio_enabled", "control_enabled", "clipboard_sync", "always_on_top", "fullscreen", "borderless", "record_enabled",
                    "new_display", "turn_screen_off", "stay_awake", "show_touches", "keyboard_uhid", "record_timestamp")

    def __init__(self, runner: TaskRunner, parent: QWidget | None = None, *, config_path: Path | None = None) -> None:
        super().__init__(parent)
        self.runner = runner
        self.config_path = config_path
        self.now = datetime.now
        self._loading = True
        self._saved_config: dict[str, object] | None = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self.save_config)
        self._serial = ""
        self._state = ""
        self._model = ""
        self._updating = False
        self._scrcpy_program = ""
        self._adb_program = ""
        self._tool_error = ""
        self._active_task_id = ""
        self.last_task_id = ""
        self._encoder_task_id = ""
        self._active = False
        self._generation = 0
        self._encoder_generation = 0
        self._encoders_dirty = True
        self._output_task: Task | None = None
        self._output_dirty: set[str] = set()
        self._rendered_output: dict[str, str | None] = {"stdout": None, "stderr": None}
        self._output_timer = QTimer(self)
        self._output_timer.setSingleShot(True)
        self._output_timer.setInterval(50)
        self._output_timer.timeout.connect(self._flush_output)
        self.session_config: ScrcpyConfig | None = None
        self._build_ui()
        self.runner.task_changed.connect(self._task_changed)
        self.runner.task_finished.connect(self._task_finished)
        self.runner.task_output.connect(self._task_output)
        self.reset_defaults()
        self.load_config()
        # Only explicit changes are written; untouched defaults leave no file behind.
        self._saved_config = self._config_snapshot()
        self._loading = False
        self.detect_tools()
        self.set_device("")

    @property
    def active_task_id(self) -> str:
        return self._active_task_id

    @staticmethod
    def _combo(items: tuple[tuple[str, object], ...]) -> QComboBox:
        combo = QComboBox()
        for label, value in items:
            combo.addItem(label, value)
        return combo

    @staticmethod
    def _spin(minimum: int, maximum: int, step: int = 1) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        return spin

    @staticmethod
    def _note(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def _tab_body(self, title: str) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        self.tabs.addTab(scroll, title)
        return body

    def _build_ui(self) -> None:
        layout = ui_kit.page_layout(QVBoxLayout(self))
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("当前设备"))
        self.current_device = QLineEdit()
        self.current_device.setReadOnly(True)
        self.current_device.setPlaceholderText("请在顶部选择在线设备")
        toolbar.addWidget(self.current_device, 1)
        self.status_label = QLabel("未运行")
        toolbar.addWidget(self.status_label)
        self.start_button = QPushButton("启动独立窗口")
        self.stop_button = QPushButton("停止")
        self.force_stop_button = QPushButton("强制结束")
        self.reset_button = QPushButton("恢复默认")
        self.start_button.clicked.connect(self.start_session)
        self.stop_button.clicked.connect(self.stop_session)
        self.force_stop_button.clicked.connect(self.force_stop_session)
        self.reset_button.clicked.connect(self.reset_defaults)
        for button in (self.start_button, self.stop_button, self.force_stop_button, self.reset_button):
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        self.error_label = self._note("")
        ui_kit.set_role(self.error_label, "error")
        self.error_label.hide()
        self.error_scroll = QScrollArea()
        ui_kit.set_role(self.error_scroll, "banner")
        self.error_scroll.setWidgetResizable(True)
        self.error_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.error_scroll.setMinimumHeight(50)
        self.error_scroll.setMaximumHeight(110)
        self.error_scroll.setWidget(self.error_label)
        self.error_scroll.hide()
        layout.addWidget(self.error_scroll)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        session = self._tab_body("投屏会话")
        session_layout = QVBoxLayout(session)
        session_layout.addWidget(ui_kit.info_note("画面在 Scrcpy 独立窗口中显示；配置修改仅影响下一次启动。", "画面在 Scrcpy 原生独立窗口中显示，本工具只管理启动配置和进程，不内嵌设备画面。配置修改仅影响下一次启动。"))
        session_panels = QHBoxLayout()
        device_group = QGroupBox("目标设备与会话")
        device_form = QFormLayout(device_group)
        self.device_model = self._note("未选择设备")
        self.device_serial = self._note("—")
        self.device_state = self._note("未选择设备")
        self.supported_video_label = self._note("未选择设备")
        self.supported_video_label.setObjectName("supportedVideoCodecs")
        self.supported_audio_label = self._note("未选择设备")
        self.supported_audio_label.setObjectName("supportedAudioCodecs")
        self.session_label = self._note("未运行")
        self.recording_label = self._note("未开始")
        for label, widget in (("设备型号", self.device_model), ("设备序列号", self.device_serial), ("设备状态", self.device_state), ("支持的视频编码", self.supported_video_label), ("支持的音频编码", self.supported_audio_label), ("会话状态", self.session_label), ("录制状态", self.recording_label)):
            device_form.addRow(label, widget)
        session_panels.addWidget(device_group, 1)
        profile_group = QGroupBox("快捷配置")
        profile_layout = QVBoxLayout(profile_group)
        profile_form = QFormLayout()
        self.profile = self._combo((("均衡 · 最长边 1080", "balanced"), ("流畅 · 最长边 720", "smooth"), ("高清 · 最长边 1920", "quality"), ("自定义配置", "custom")))
        profile_form.addRow("画质方案", self.profile)
        self.summary_labels: dict[str, QLabel] = {}
        for key, title in (("size", "最长边限制"), ("fps", "帧率上限"), ("bitrate", "视频码率"), ("audio", "音频转发"), ("control", "设备控制")):
            self.summary_labels[key] = QLabel()
            profile_form.addRow(title, self.summary_labels[key])
        profile_layout.addLayout(profile_form)
        self.media_jump_button = QPushButton("视频与音频设置")
        self.control_jump_button = QPushButton("控制与录制设置")
        self.media_jump_button.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        self.control_jump_button.clicked.connect(lambda: self.tabs.setCurrentIndex(2))
        profile_layout.addWidget(self.media_jump_button)
        profile_layout.addWidget(self.control_jump_button)
        profile_layout.addWidget(ui_kit.info_note("方案只是启动参数预设，不代表实测画质或延迟。", "方案是启动参数预设，不代表实测画质或延迟。适用于 Scrcpy 3 及更新版本；参数不兼容时请查看真实 stderr。"))
        profile_layout.addStretch()
        session_panels.addWidget(profile_group, 1)
        session_layout.addLayout(session_panels)
        session_layout.addStretch()

        media = self._tab_body("视频与音频")
        media_layout = QHBoxLayout(media)
        video_group = QGroupBox("视频参数 · 启动时应用")
        video_layout = QVBoxLayout(video_group)
        video_form = QFormLayout()
        self.max_size = self._combo((("原始尺寸（不限制）", 0), ("720 px", 720), ("1080 px", 1080), ("1280 px", 1280), ("1920 px", 1920)))
        self.max_fps = self._combo((("不限制", 0), ("30 FPS", 30), ("60 FPS", 60), ("90 FPS", 90), ("120 FPS", 120)))
        self.video_bitrate = self._spin(1, 100)
        self.video_codec = self._combo((("H.264（兼容性优先）", "h264"), ("H.265", "h265"), ("AV1", "av1")))
        self.video_buffer = self._spin(0, 2000, 10)
        self.orientation = self._combo((("默认方向", 0), ("旋转 90°", 90), ("旋转 180°", 180), ("旋转 270°", 270)))
        for title, widget in (("最长边限制", self.max_size), ("帧率上限", self.max_fps), ("视频码率（Mbps）", self.video_bitrate), ("视频编码", self.video_codec), ("视频缓冲（毫秒）", self.video_buffer), ("显示方向", self.orientation)):
            video_form.addRow(title, widget)
        video_layout.addLayout(video_form)
        video_layout.addWidget(ui_kit.info_note("H.265 / AV1 取决于设备编码器；缓冲越大延迟越高。", "最长边限制保留画面比例。H.265 / AV1 取决于设备编码器；增大缓冲会增加延迟。显示方向只改变显示，不改变设备方向。"))
        video_layout.addStretch()
        media_layout.addWidget(video_group, 1)
        audio_group = QGroupBox("音频转发")
        audio_layout = QVBoxLayout(audio_group)
        self.audio_enabled = QCheckBox("转发设备音频（关闭时使用 --no-audio）")
        audio_layout.addWidget(self.audio_enabled)
        self.audio_settings = QWidget()
        audio_form = QFormLayout(self.audio_settings)
        audio_form.setContentsMargins(0, 0, 0, 0)
        self.audio_source = self._combo((("设备输出（设备端停止播放）", "output"), ("设备麦克风", "mic")))
        self.audio_bitrate = self._spin(32, 512, 32)
        self.audio_buffer = self._spin(10, 1000, 10)
        audio_form.addRow("音频来源", self.audio_source)
        audio_form.addRow("音频码率（Kbps）", self.audio_bitrate)
        audio_form.addRow("音频缓冲（毫秒）", self.audio_buffer)
        audio_layout.addWidget(self.audio_settings)
        audio_layout.addWidget(ui_kit.info_note("需要 Android 11+；麦克风来源会录到设备周围的声音。", "音频转发需要 Android 11 或更新系统；Android 11 启动时需解锁屏幕。麦克风来源会捕获设备周围的声音，请注意隐私。"))
        audio_layout.addStretch()
        media_layout.addWidget(audio_group, 1)

        control = self._tab_body("控制与录制")
        control_layout = QHBoxLayout(control)
        window_group = QGroupBox("设备控制与窗口")
        window_layout = QVBoxLayout(window_group)
        self.control_settings = QWidget()
        control_checks = QVBoxLayout(self.control_settings)
        control_checks.setContentsMargins(0, 0, 0, 0)
        self.control_enabled = QCheckBox("允许键盘和鼠标控制")
        self.clipboard_sync = QCheckBox("自动同步剪贴板")
        control_checks.addWidget(self.control_enabled)
        control_checks.addWidget(self.clipboard_sync)
        window_layout.addWidget(self.control_settings)
        window_layout.addWidget(ui_kit.info_note("关闭控制即只读投屏；剪贴板同步可能带出敏感内容。", "关闭控制后为只读投屏。同步剪贴板可能将设备敏感内容复制到电脑。"))
        self.window_settings = QWidget()
        window_checks = QVBoxLayout(self.window_settings)
        window_checks.setContentsMargins(0, 0, 0, 0)
        self.always_on_top = QCheckBox("窗口置顶")
        self.fullscreen = QCheckBox("以全屏模式启动")
        self.borderless = QCheckBox("隐藏窗口边框")
        for widget in (self.always_on_top, self.fullscreen, self.borderless):
            window_checks.addWidget(widget)
        window_layout.addWidget(self.window_settings)
        window_layout.addWidget(ui_kit.info_note("运行中的会话不会随这里的设置变化。", "仅录制不创建窗口，也不启用控制。运行中的会话不会随此处设置变化。"))
        window_layout.addStretch()
        control_layout.addWidget(window_group, 1)
        record_group = QGroupBox("录制设置 · 保存到电脑")
        record_layout = QVBoxLayout(record_group)
        self.record_enabled = QCheckBox("启动时录制")
        record_layout.addWidget(self.record_enabled)
        self.record_settings = QWidget()
        record_form = QFormLayout(self.record_settings)
        record_form.setContentsMargins(0, 0, 0, 0)
        path_widget = QWidget()
        path_layout = QHBoxLayout(path_widget)
        path_layout.setContentsMargins(0, 0, 0, 0)
        self.record_path = QLineEdit()
        self.record_path.setPlaceholderText("例如 D:\\Recordings\\screen.mp4")
        self.browse_button = QPushButton("浏览…")
        self.browse_button.clicked.connect(self.choose_record_file)
        path_layout.addWidget(self.record_path, 1)
        path_layout.addWidget(self.browse_button)
        self.record_format = self._combo((("MP4", "mp4"), ("MKV", "mkv")))
        self.record_mode = self._combo((("投屏并录制", "mirror"), ("仅录制（不显示窗口）", "only")))
        record_form.addRow("输出文件", path_widget)
        record_form.addRow("封装格式", self.record_format)
        record_form.addRow("录制模式", self.record_mode)
        self.record_timestamp = QCheckBox("文件名追加设备序列号和启动时间")
        self.record_timestamp.setToolTip("例如 screen-SERIAL-20261009-213000.mp4；多台设备同时录制时不会互相覆盖。")
        record_form.addRow("", self.record_timestamp)
        record_layout.addWidget(self.record_settings)
        record_layout.addWidget(ui_kit.info_note("启动前检查目录可写并确认覆盖；强制结束可能损坏录制文件。", "支持包含空格、单引号和 $ 的文件名。启动前检查目录可写并确认覆盖现有文件；不会创建目录。正常结束有助于完成文件封装，强制结束可能损坏录制。相对路径基于应用当前工作目录。"))
        record_layout.addStretch()
        control_layout.addWidget(record_group, 1)

        source = self._tab_body("画面来源与设备")
        source_layout = QHBoxLayout(source)
        source_group = QGroupBox("画面来源 · 显示屏或摄像头")
        source_box = QVBoxLayout(source_group)
        source_form = QFormLayout()
        self.video_source = self._combo((("设备显示屏", "display"), ("设备摄像头（Android 12+）", "camera")))
        self.camera_facing = self._combo((("后置摄像头", "back"), ("前置摄像头", "front"), ("外接摄像头", "external")))
        self.display_id = self._spin(0, 9999)
        self.display_id.setSpecialValueText("主显示屏（0）")
        self.new_display = QCheckBox("新建虚拟显示屏（--new-display）")
        self.new_display_spec = QLineEdit()
        self.new_display_spec.setPlaceholderText("留空=主屏尺寸；例如 1920x1080/420 或 /240")
        self.crop = QLineEdit()
        self.crop.setPlaceholderText("宽:高:X:Y，例如 1224:1440:0:0；留空不裁剪")
        for title, widget in (("视频来源", self.video_source), ("摄像头朝向", self.camera_facing), ("显示屏 ID", self.display_id), ("", self.new_display), ("虚拟显示屏参数", self.new_display_spec), ("裁剪区域", self.crop)):
            source_form.addRow(title, widget)
        source_box.addLayout(source_form)
        source_box.addWidget(ui_kit.info_note("摄像头来源不支持显示屏 ID、虚拟显示屏和裁剪。", "显示屏 ID 可通过 scrcpy --list-displays 查看。新建虚拟显示屏与显示屏 ID 互斥。裁剪按设备自然方向（手机通常为竖屏）计算。摄像头来源需要 Android 12+，且不转发键鼠输入。"))
        source_box.addStretch()
        source_layout.addWidget(source_group, 1)
        behavior_group = QGroupBox("设备行为 · 需要启用控制")
        behavior_box = QVBoxLayout(behavior_group)
        self.behavior_settings = QWidget()
        behavior_checks = QVBoxLayout(self.behavior_settings)
        behavior_checks.setContentsMargins(0, 0, 0, 0)
        self.turn_screen_off = QCheckBox("启动后关闭设备屏幕（--turn-screen-off）")
        self.stay_awake = QCheckBox("插电时保持唤醒（--stay-awake）")
        self.show_touches = QCheckBox("显示触摸点（--show-touches）")
        self.keyboard_uhid = QCheckBox("模拟物理键盘 UHID（--keyboard=uhid）")
        for widget in (self.turn_screen_off, self.stay_awake, self.show_touches, self.keyboard_uhid):
            behavior_checks.addWidget(widget)
        start_form = QFormLayout()
        start_form.setContentsMargins(0, 0, 0, 0)
        self.start_app = QLineEdit()
        self.start_app.setPlaceholderText("包名，例如 com.android.settings；+ 前缀先强停，? 前缀按名称")
        start_form.addRow("启动应用", self.start_app)
        behavior_checks.addLayout(start_form)
        behavior_box.addWidget(self.behavior_settings)
        behavior_box.addWidget(ui_kit.info_note("关闭控制时这些选项不会生效；退出时 Scrcpy 会恢复触摸点和唤醒设置。", "Scrcpy 在关闭控制（--no-control）时拒绝这些选项，因此本页会自动省略。仅录制模式仍保留控制，可配合关闭屏幕使用。UHID 键盘仅在窗口投屏且启用控制时生效。"))
        behavior_box.addStretch()
        source_layout.addWidget(behavior_group, 1)

        self._panel_layouts = (session_panels, media_layout, control_layout, source_layout)
        for form in (device_form, profile_form, video_form, audio_form, record_form, source_form, start_form):
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        command_group = QGroupBox("启动命令 · PowerShell")
        command_layout = QVBoxLayout(command_group)
        command_row = QHBoxLayout()
        self.command_note = self._note("配置修改会实时更新命令；尚未执行。")
        command_row.addWidget(self.command_note, 1)
        self.copy_button = QPushButton("复制启动命令")
        self.copy_button.clicked.connect(self.copy_command)
        command_row.addWidget(self.copy_button)
        command_layout.addLayout(command_row)
        self.command_preview = QPlainTextEdit()
        self.command_preview.setReadOnly(True)
        self.command_preview.setMaximumHeight(100)
        self.command_preview.setMinimumHeight(self.command_preview.fontMetrics().lineSpacing() * 3 + 16)
        command_layout.addWidget(self.command_preview)
        layout.addWidget(command_group)

        output_row = QHBoxLayout()
        self.output_toggle = QCheckBox("显示本次会话原始输出")
        self.output_button = QPushButton("在任务面板查看完整输出")
        self.output_button.clicked.connect(self.open_output)
        output_row.addWidget(self.output_toggle)
        output_row.addStretch()
        output_row.addWidget(self.output_button)
        layout.addLayout(output_row)
        self.output_tabs = QTabWidget()
        self.stdout_output = QPlainTextEdit()
        self.stderr_output = QPlainTextEdit()
        for title, output in (("stdout", self.stdout_output), ("stderr", self.stderr_output)):
            output.setReadOnly(True)
            output.setMaximumBlockCount(5000)
            self.output_tabs.addTab(output, title)
        self.output_tabs.setMaximumHeight(150)
        self.output_tabs.hide()
        self.output_toggle.toggled.connect(self._output_visibility_changed)
        layout.addWidget(self.output_tabs)

        self.profile.currentIndexChanged.connect(self._profile_changed)
        for widget in (self.max_size, self.max_fps, self.video_codec):
            widget.currentIndexChanged.connect(self._profile_setting_changed)
        for widget in (self.video_bitrate, self.video_buffer):
            widget.valueChanged.connect(self._profile_setting_changed)
        self.audio_enabled.toggled.connect(self._profile_setting_changed)
        for widget in (self.orientation, self.audio_source, self.record_mode, self.video_source, self.camera_facing):
            widget.currentIndexChanged.connect(self._config_changed)
        for widget in (self.audio_bitrate, self.audio_buffer, self.display_id):
            widget.valueChanged.connect(self._config_changed)
        for widget in (self.control_enabled, self.clipboard_sync, self.always_on_top, self.fullscreen, self.borderless, self.record_enabled,
                       self.new_display, self.turn_screen_off, self.stay_awake, self.show_touches, self.keyboard_uhid,
                       self.record_timestamp):
            widget.toggled.connect(self._config_changed)
        for widget in (self.record_path, self.new_display_spec, self.crop, self.start_app):
            widget.textChanged.connect(self._config_changed)
        self.record_format.currentIndexChanged.connect(self._record_format_changed)

    def set_device(self, serial: str, state: str = "device", model: str = "") -> None:
        changed = (serial, state) != (self._serial, self._state)
        self._serial, self._state, self._model = serial, state, model
        if not changed:
            self.device_model.setText(model or ("型号未知" if serial else "未选择设备"))
            return
        self.current_device.setText(serial)
        self.device_serial.setText(serial or "—")
        self.device_model.setText(model or ("型号未知" if serial else "未选择设备"))
        self.device_state.setText({"device": "在线", "offline": "离线", "unauthorized": "未授权，请在设备上允许 USB 调试"}.get(state, state or "未选择设备") if serial else "未选择设备")
        self._refresh_config()
        if changed:
            self._generation += 1
            previous_id = self._encoder_task_id
            self._encoder_task_id = ""
            self._encoders_dirty = True
            self._encoder_status("尚未查询" if serial and state == "device" else "设备不在线，无法查询")
            if previous_id:
                self.runner.cancel(previous_id)
            if self._active:
                self._query_encoders()

    def set_active(self, active: bool) -> None:
        self._active = active
        if active:
            self._query_encoders()
            self._schedule_output()
        else:
            self._output_timer.stop()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        direction = QBoxLayout.Direction.TopToBottom if self.width() < 900 else QBoxLayout.Direction.LeftToRight
        for panel in self._panel_layouts:
            panel.setDirection(direction)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._schedule_output()

    def _encoder_status(self, text: str, detail: str = "") -> None:
        for label in (self.supported_video_label, self.supported_audio_label):
            label.setText(text)
            label.setToolTip(detail)

    def _query_encoders(self) -> None:
        if not self._active or not self._encoders_dirty or self._encoder_task_id:
            return
        if not self._serial:
            self._encoder_status("未选择设备")
            return
        if self._state != "device":
            self._encoder_status("设备不在线，无法查询")
            return
        self._encoders_dirty = False
        generation = self._generation
        if not self.detect_tools():
            self._encoder_status("查询失败", self._tool_error)
            return
        self._encoder_status("正在查询…")
        try:
            task = self.runner.start_process(
                "Scrcpy 查询支持的编码", self._scrcpy_program,
                [f"--serial={self._serial}", "--list-encoders"],
                serial=self._serial, kind="scrcpy-info", timeout=10,
                env={"ADB": self._adb_program},
            )
        except (ValueError, OSError, RuntimeError) as exc:
            if generation == self._generation:
                self._encoder_status("查询失败", str(exc))
            return
        if generation != self._generation:
            return
        self._encoder_generation = generation
        self._encoder_task_id = task.id

    def _encoders_received(self, task: Task) -> None:
        self._encoder_task_id = ""
        if (self._encoder_generation != self._generation
                or task.serial != self._serial or self._state != "device"):
            return
        output = task.stdout + "\n" + task.stderr
        if task.status != "succeeded" or task.exit_code != 0:
            self._encoder_status("查询失败，详见任务输出", output)
            return
        try:
            encoders = parse_encoder_list(output)
        except ValueError as exc:
            self._encoder_status("无法读取编码列表", f"{exc}\n{output}")
            return
        names = {"h264": "H.264", "h265": "H.265"}
        for media, label in (("video", self.supported_video_label), ("audio", self.supported_audio_label)):
            supported = encoders[media]
            label.setText("、".join(names.get(codec, codec.upper()) for codec in supported) or "未列出可用编码")
            details = "\n\n".join(names.get(codec, codec.upper()) + "\n" + "\n".join(entries) for codec, entries in supported.items())
            label.setToolTip((details + "\n\n" if details else "") + "设备返回的编码器列表；(hw) 为硬件，(sw) 为软件。可选启动编码以“视频与音频”设置为准，列出编码器不保证当前参数可用。")

    def current_config(self) -> ScrcpyConfig:
        return ScrcpyConfig(
            max_size=self.max_size.currentData(), max_fps=self.max_fps.currentData(),
            video_bitrate=self.video_bitrate.value(), video_codec=self.video_codec.currentData(),
            video_buffer=self.video_buffer.value(), orientation=self.orientation.currentData(),
            audio_enabled=self.audio_enabled.isChecked(), audio_source=self.audio_source.currentData(),
            audio_bitrate=self.audio_bitrate.value(), audio_buffer=self.audio_buffer.value(),
            control_enabled=self.control_enabled.isChecked(), clipboard_sync=self.clipboard_sync.isChecked(),
            always_on_top=self.always_on_top.isChecked(), fullscreen=self.fullscreen.isChecked(),
            borderless=self.borderless.isChecked(), record_enabled=self.record_enabled.isChecked(),
            record_path=self.record_path.text(), record_format=self.record_format.currentData(),
            record_mode=self.record_mode.currentData(),
            video_source=self.video_source.currentData(), camera_facing=self.camera_facing.currentData(),
            display_id=self.display_id.value(), new_display=self.new_display.isChecked(),
            new_display_spec=self.new_display_spec.text(), crop=self.crop.text(), start_app=self.start_app.text(),
            turn_screen_off=self.turn_screen_off.isChecked(), stay_awake=self.stay_awake.isChecked(),
            show_touches=self.show_touches.isChecked(), keyboard_uhid=self.keyboard_uhid.isChecked(),
            record_timestamp=self.record_timestamp.isChecked(),
        )

    def build_args(self) -> list[str]:
        return build_scrcpy_args(self._serial, self.current_config())

    def reset_defaults(self) -> None:
        defaults = ScrcpyConfig()
        movies = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
        self.apply_config(replace(defaults, record_path=str(Path(movies) / defaults.record_path)), "balanced")

    def apply_config(self, config: ScrcpyConfig, profile: str = "custom") -> None:
        """Load values into the form; values a widget does not offer keep the current choice."""
        self._updating = True
        try:
            for name in self.SPIN_FIELDS:
                getattr(self, name).setValue(getattr(config, name))
            for name in self.CHECK_FIELDS:
                getattr(self, name).setChecked(getattr(config, name))
            for name in self.COMBO_FIELDS:
                widget = getattr(self, name)
                index = widget.findData(getattr(config, name))
                if index >= 0:
                    widget.setCurrentIndex(index)
            for name in ("new_display_spec", "crop", "start_app", "record_path"):
                getattr(self, name).setText(getattr(config, name))
            index = self.profile.findData(profile)
            self.profile.setCurrentIndex(index if index >= 0 else self.profile.findData("custom"))
        finally:
            self._updating = False
        self._refresh_config()

    def load_config(self) -> bool:
        """Restore the last saved launch configuration; a broken file keeps defaults."""
        if self.config_path is None:
            return False
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        profile = data.get("profile")
        self.apply_config(config_from_dict(data.get("config")), profile if isinstance(profile, str) else "custom")
        return True

    def _config_snapshot(self) -> dict[str, object]:
        return {"version": 1, "profile": self.profile.currentData(), "config": config_to_dict(self.current_config())}

    def save_config(self) -> bool:
        self._save_timer.stop()
        if self.config_path is None:
            return False
        snapshot = self._config_snapshot()
        if snapshot == self._saved_config:
            return True
        temporary = self.config_path.with_name(self.config_path.name + ".tmp")
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.config_path)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            return False
        self._saved_config = snapshot
        return True

    def _profile_changed(self) -> None:
        if self._updating:
            return
        preset = self.PROFILES.get(self.profile.currentData())
        if preset is not None:
            self._updating = True
            try:
                size, fps, bitrate, audio = preset
                self.max_size.setCurrentIndex(self.max_size.findData(size))
                self.max_fps.setCurrentIndex(self.max_fps.findData(fps))
                self.video_bitrate.setValue(bitrate)
                self.video_codec.setCurrentIndex(self.video_codec.findData("h264"))
                self.video_buffer.setValue(0)
                self.audio_enabled.setChecked(audio)
            finally:
                self._updating = False
        self._refresh_config()

    def _profile_setting_changed(self) -> None:
        if self._updating:
            return
        self._updating = True
        self.profile.setCurrentIndex(self.profile.findData("custom"))
        self._updating = False
        self._refresh_config()

    def _config_changed(self) -> None:
        if not self._updating:
            self._set_error(self._tool_error)
            self._refresh_config()

    def _record_format_changed(self) -> None:
        if self._updating:
            return
        filename = self.record_path.text()
        if filename.strip():
            self._updating = True
            self.record_path.setText(re.sub(r"\.(mp4|mkv)$", "", filename, flags=re.IGNORECASE) + "." + self.record_format.currentData())
            self._updating = False
        self._refresh_config()

    def _refresh_config(self) -> None:
        config = self.current_config()
        if (not self._loading and not self._updating and self.config_path is not None
                and self._config_snapshot() != self._saved_config):
            self._save_timer.start()
        self.audio_settings.setEnabled(config.audio_enabled)
        self.record_settings.setEnabled(config.record_enabled)
        self.control_settings.setEnabled(not config.record_only)
        self.window_settings.setEnabled(not config.record_only)
        self.orientation.setEnabled(not config.record_only)
        self.clipboard_sync.setEnabled(config.control_enabled and not config.record_only)
        camera = config.video_source == "camera"
        self.camera_facing.setEnabled(camera)
        self.display_id.setEnabled(not camera and not config.new_display)
        self.new_display.setEnabled(not camera)
        self.new_display_spec.setEnabled(not camera and config.new_display)
        self.crop.setEnabled(not camera)
        self.behavior_settings.setEnabled(config.device_control)
        self.keyboard_uhid.setEnabled(config.control_enabled and not config.record_only and not camera)
        self.summary_labels["size"].setText(f"{config.max_size} px" if config.max_size else "不限制")
        self.summary_labels["fps"].setText(f"{config.max_fps} FPS" if config.max_fps else "不限制")
        self.summary_labels["bitrate"].setText(f"{config.video_bitrate} Mbps")
        self.summary_labels["audio"].setText(("麦克风" if config.audio_source == "mic" else "设备输出") if config.audio_enabled else "已关闭")
        self.summary_labels["control"].setText("已启用" if config.control_enabled and not config.record_only else "只读 / 不控制")
        self.start_button.setText("启动仅录制" if config.record_only else "启动独立窗口")
        try:
            args = self.build_args()
            command = powershell_command(self._scrcpy_program or "scrcpy", args)
            if self._adb_program:
                command = "$env:ADB = '" + self._adb_program.replace("'", "''") + "'\n" + command
            self.command_preview.setPlainText(command)
            self.copy_button.setEnabled(True)
            note = "配置修改仅应用于下次启动；命令未执行。"
            if self._tool_error:
                note = "程序检测失败：" + self._tool_error
            elif self._state != "device":
                note = "当前设备不在线，不能启动；命令仅供预览。"
            self.command_note.setText(note)
            valid = True
        except ValueError as exc:
            self.command_preview.setPlainText(str(exc))
            self.command_note.setText("配置未完成，暂不能复制启动命令。")
            self.copy_button.setEnabled(False)
            valid = False
        active = bool(self._active_task_id)
        self.start_button.setEnabled(valid and self._state == "device" and not active)
        self.stop_button.setEnabled(active)
        self.force_stop_button.setEnabled(active)
        self.output_button.setEnabled(bool(self.last_task_id))

    def detect_tools(self) -> bool:
        errors: list[str] = []
        for name, attribute in (("scrcpy", "_scrcpy_program"), ("adb", "_adb_program")):
            try:
                setattr(self, attribute, self.runner.resolve(name))
            except (ValueError, OSError) as exc:
                setattr(self, attribute, "")
                errors.append(f"{name}: {exc}")
        self._tool_error = "；".join(errors)
        self._set_error(self._tool_error)
        self._refresh_config()
        return not errors

    def choose_record_file(self) -> None:
        file_format = self.record_format.currentData()
        filters = "MP4 视频 (*.mp4);;MKV 视频 (*.mkv)"
        filename, selected_filter = QFileDialog.getSaveFileName(
            self, "选择录制输出文件", self.record_path.text(), filters,
            "MP4 视频 (*.mp4)" if file_format == "mp4" else "MKV 视频 (*.mkv)",
            QFileDialog.Option.DontConfirmOverwrite,
        )
        if not filename:
            return
        file_format = "mkv" if selected_filter.startswith("MKV") else "mp4"
        if not filename.lower().endswith((".mp4", ".mkv")):
            filename += "." + file_format
        elif filename.lower().endswith(".mkv"):
            file_format = "mkv"
        else:
            file_format = "mp4"
        self._updating = True
        self.record_format.setCurrentIndex(self.record_format.findData(file_format))
        self.record_path.setText(filename)
        self._updating = False
        self._refresh_config()

    def copy_command(self) -> None:
        if self.copy_button.isEnabled():
            QApplication.clipboard().setText(self.command_preview.toPlainText())
            self.command_note.setText("已复制 PowerShell 命令，未执行。")

    def start_session(self) -> Task | None:
        if self._active_task_id:
            self._set_error("已有 Scrcpy 会话，请先停止该会话。")
            return None
        if not self._serial or self._state != "device":
            self._set_error("请选择在线且已授权的设备；当前状态：" + (self._state or "未选择设备"))
            return None
        if not self.detect_tools():
            return None
        config = self.current_config()
        serial = self._serial
        if config.record_enabled and config.record_timestamp and config.record_path.strip():
            config = replace(config, record_path=timestamped_recording_path(config.record_path, serial, self.now()))
        try:
            args = build_scrcpy_args(serial, config)
            if config.record_enabled:
                output_path = validate_recording_path(config.record_path)
                if output_path.exists():
                    answer = QMessageBox.warning(
                        self, "确认覆盖录制文件",
                        f"输出文件已存在：\n{output_path}\n\n启动 Scrcpy 将覆盖此文件。是否继续？",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if answer != QMessageBox.StandardButton.Yes:
                        self._set_error("已取消启动；现有录制文件未被修改。")
                        return None
            task = self.runner.start_process(
                "Scrcpy 仅录制" if config.record_only else "Scrcpy 独立窗口",
                self._scrcpy_program, args, serial=serial, kind="scrcpy", timeout=0,
                env={"ADB": self._adb_program},
            )
        except (ValueError, OSError, RuntimeError) as exc:
            self._set_error(str(exc))
            return None
        self.session_config = config
        self.last_task_id = task.id
        self._active_task_id = task.id if task.status not in TERMINAL_STATUSES else ""
        self._output_task = task
        self._rendered_output = {"stdout": None, "stderr": None}
        self._output_dirty.update(("stdout", "stderr"))
        self._schedule_output()
        self._set_error("")
        self._render_task(task)
        self._refresh_config()
        return task

    def stop_session(self) -> None:
        if self._active_task_id:
            self.runner.cancel(self._active_task_id)

    def force_stop_session(self) -> None:
        if not self._active_task_id:
            return
        answer = QMessageBox.warning(
            self, "强制结束 Scrcpy",
            "强制结束会立即终止 Scrcpy，录制文件可能未完成封装或损坏。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.runner.cancel(self._active_task_id, force=True)

    def open_output(self) -> None:
        if self.last_task_id:
            self.output_toggle.setChecked(True)
            self.show_output.emit(self.last_task_id)

    def _set_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))
        self.error_scroll.setVisible(bool(text))

    def _render_task(self, task: Task) -> None:
        status = STATUS_LABELS.get(task.status, task.status)
        self.status_label.setText(status)
        self.session_label.setText(f"{status} · {task.serial}\n任务 {task.id}" + (f" · 退出码 {task.exit_code}" if task.exit_code is not None else ""))
        config = self.session_config
        if config is None or not config.record_enabled:
            self.recording_label.setText("此会话未启用录制")
        else:
            recording_status = {
                "starting": "录制会话正在启动", "running": "录制会话运行中", "stopping": "正在停止，请等待文件封装",
                "succeeded": "录制会话正常结束", "failed": "录制会话失败，请检查输出和文件",
                "cancelled": "录制会话已停止，请检查文件完整性", "timed_out": "录制会话超时，请检查文件完整性",
            }.get(task.status, status)
            self.recording_label.setText(recording_status + "\n" + config.record_path)
        if task.status == "failed":
            self._set_error("Scrcpy 启动或运行失败。" + ("\n" + task.stderr if task.stderr else "请查看会话原始输出和退出码。"))
            self.output_toggle.setChecked(True)
            self.output_tabs.setCurrentIndex(1)

    def _task_changed(self, task: Task) -> None:
        if task.id == self.last_task_id:
            self._render_task(task)

    def _task_finished(self, task: Task) -> None:
        if task.id == self._encoder_task_id:
            self._encoders_received(task)
            return
        if task.id != self.last_task_id:
            return
        if task.id == self._active_task_id:
            self._active_task_id = ""
        self._output_task = task
        self._output_dirty.update(("stdout", "stderr"))
        self._schedule_output()
        self._render_task(task)
        self._refresh_config()

    def _task_output(self, task_id: str, stream: str, text: str) -> None:
        if task_id != self.last_task_id:
            return
        self._output_dirty.add(stream)
        self._schedule_output()

    def _output_visibility_changed(self, visible: bool) -> None:
        self.output_tabs.setVisible(visible)
        if visible and self._active and self.isVisible():
            self._flush_output()
        elif not visible:
            self._output_timer.stop()

    def _schedule_output(self) -> None:
        if (self._active and self.isVisible() and self.output_toggle.isChecked()
                and self._output_dirty and not self._output_timer.isActive()):
            self._output_timer.start()

    def _flush_output(self) -> None:
        if not self._active or not self.isVisible() or not self.output_toggle.isChecked():
            return
        task = self._output_task
        if task is None:
            return
        for stream in tuple(self._output_dirty):
            output = self.stderr_output if stream == "stderr" else self.stdout_output
            text = getattr(task, stream)
            if len(text) > OUTPUT_LIMIT:
                text = TRUNCATED + text[-(OUTPUT_LIMIT - len(TRUNCATED)):]
            previous = self._rendered_output[stream]
            bar = output.verticalScrollBar()
            position = bar.value()
            at_bottom = position >= bar.maximum() - 2
            if text != previous:
                if previous is not None and text.startswith(previous):
                    cursor = QTextCursor(output.document())
                    cursor.movePosition(QTextCursor.MoveOperation.End)
                    cursor.insertText(text[len(previous):])
                else:
                    output.setPlainText(text)
                excess = output.document().characterCount() - 1 - OUTPUT_LIMIT
                if excess > 0:
                    cursor = QTextCursor(output.document())
                    cursor.setPosition(0)
                    cursor.setPosition(excess, QTextCursor.MoveMode.KeepAnchor)
                    cursor.removeSelectedText()
                self._rendered_output[stream] = text
                bar.setValue(bar.maximum() if at_bottom else min(position, bar.maximum()))
            self._output_dirty.discard(stream)
