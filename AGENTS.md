# 项目开发说明

## 环境

- Windows Python 3.14.8 embedded：`lib/python-3.14.8-embed-amd64/python.exe`
- GUI 框架：PySide6（Qt 6）
- pip 镜像配置：`lib/python-3.14.8-embed-amd64/pip.ini`

## 运行与安装

在项目根目录执行：

```bat
lib\python-3.14.8-embed-amd64\python.exe your_script.py
lib\python-3.14.8-embed-amd64\python.exe -m pip install 包名
```

始终使用 `python.exe -m pip`，不要依赖 `Scripts` 目录是否已加入 PATH。

## 代码布局

- 应用代码在 `src/sysdroid/`：`core/` 放不依赖界面的设备逻辑，`ui/` 放主窗口、主题和通用控件，`ui/pages/` 每个导航页一个模块；入口是 `sysdroid.app.main()`。
- 根目录 `sysdroid.py` 只是启动器（嵌入式 Python 不读 PYTHONPATH，由它把 `src/` 加入 `sys.path`），不要在里面写业务代码。
- 模块之间使用 `sysdroid.*` 绝对导入。
- 构建脚本在 `scripts/`，设计文档在 `docs/design/`，测试按 `tests/core|ui|packaging` 分组；测试文件名保持全局唯一（tests 下没有 `__init__.py`）。
- 运行测试：`lib\python-3.14.8-embed-amd64\python.exe -s -m pytest tests -q`（务必带 `tests`，否则会收集 site-packages 里的测试）。

## Qt 开发约定

- 使用 PySide6，不要与 PyQt6 混用。
- 优先使用 Qt Designer 生成 `.ui` 文件，并通过 `pyside6-uic` 转换。
- 资源文件使用 `.qrc`，通过 `pyside6-rcc` 编译。
- 保持界面逻辑与业务逻辑分离，避免直接修改自动生成的 `ui_*.py` 文件。

## 验证

修改后至少确认 Python 和 PySide6 可导入：

```bat
lib\python-3.14.8-embed-amd64\python.exe --version
lib\python-3.14.8-embed-amd64\python.exe -c "import PySide6; print(PySide6.__version__)"
```
