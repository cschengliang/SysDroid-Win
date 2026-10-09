# 依赖说明

本仓库只保存源码、测试和设计文档。运行环境和第三方工具体积大，而且都能从官方渠道原样取得，所以没有放进 git。克隆仓库后，按本文补齐环境即可运行、测试和构建。

> 只想使用软件的用户不用看本文，直接到 GitHub Releases 下载打包好的 `AndroidToolbox-win-x64.zip`，解压后运行 `AndroidToolbox.exe`。便携版自带 Python、Qt、ADB、Scrcpy 和 Windows Terminal。

## 1. 不在 git 中的内容（见 `.gitignore`）

| 路径 | 内容 | 不入库的原因 |
| --- | --- | --- |
| `lib/python-3.14.8-embed-amd64/` | Windows 嵌入式 Python 及其 `Lib/site-packages`（PySide6、adbutils 等） | 体积达数百 MB，可由官方 zip + pip 重建 |
| `tool/scrcpy-win64-v5.0/` | Scrcpy 5.0（包含程序使用的 `adb.exe`） | 官方二进制发布包，固定版本和 SHA-256 |
| `tool/terminal-1.25.2733.0/` | 便携版 Windows Terminal | 同上 |
| `build/`、`dist/` | 构建中间文件和发行产物 | 由 `build_android_toolbox.py` 生成 |

## 2. 版本要求

| 组件 | 版本 | 依据 |
| --- | --- | --- |
| CPython（嵌入版，x64） | **3.14.8**（必须完全一致） | `build_android_toolbox.py` 中的 `EXPECTED_PYTHON = (3, 14, 8)`、`start_android_toolbox.bat` |
| Scrcpy | **5.0**（win64） | `portable_assets.py`、`runtime_paths.py` |
| Windows Terminal | **1.25.2733.0**（x64 zip） | `portable_assets.py`、`android_backend.py` |
| PySide6 | 6.11.2 | 当前开发环境实际安装的版本；只用到 QtCore / QtGui / QtWidgets |
| adbutils | 2.12.0 | 当前开发环境实际安装的版本 |

Python 第三方包见 [`requirements.txt`](requirements.txt)，版本已按当前开发环境固定：

- 运行时：`PySide6`、`adbutils`。`Pillow`、`requests` 等由 adbutils 自动带入。
- 构建：`pyinstaller`、`pyinstaller-hooks-contrib`、`pefile`、`packaging`、`requests`
- 测试：`pytest`、`pytest-qt`（测试用到 `qapp` / `qtbot` fixture）

每次构建都会在 `dist/AndroidToolbox-win-x64/` 中生成 `requirements-runtime.txt` 和 `requirements-build.txt`，里面记录实际安装的精确版本。需要锁定版本时，以这两个文件为准。

## 3. 官方下载来源

| 组件 | 下载地址 | SHA-256 |
| --- | --- | --- |
| Python 3.14.8 嵌入版 | https://www.python.org/ftp/python/3.14.8/python-3.14.8-embed-amd64.zip | 可在 python.org 发布页核对 |
| Scrcpy 5.0 | https://github.com/Genymobile/scrcpy/releases/download/v5.0/scrcpy-win64-v5.0.zip | `44c10d9e82f20ea67227d14d37bf9fbe3603117c5736df3f514544a02ba20a73` |
| Windows Terminal 1.25.2733.0 | https://github.com/microsoft/terminal/releases/download/v1.25.2733.0/Microsoft.WindowsTerminal_1.25.2733.0_x64.zip | `bf3ef2012f6c44d8340a4c58125acc9498d19b580f9890dc043cdf831852e796` |
| get-pip.py | https://bootstrap.pypa.io/get-pip.py | — |

上表两个 SHA-256 与 `portable_assets.py` 中固定的值一致，构建时会再次校验。PowerShell 可以这样核对：`Get-FileHash <文件> -Algorithm SHA256`。

## 4. 搭建后的目录结构

```text
SysDroid-Win/
├─ android_toolbox.py …            # 源码（git）
├─ requirements.txt
├─ lib/
│  └─ python-3.14.8-embed-amd64/
│     ├─ python.exe
│     ├─ python314.zip / python314.dll / python3.dll / LICENSE.txt
│     ├─ python314._pth            # 需加入 Lib\site-packages 并启用 import site
│     ├─ pip.ini                   # pip 镜像配置
│     └─ Lib/site-packages/        # pip 安装的包
└─ tool/
   ├─ scrcpy-win64-v5.0/           # scrcpy.exe、scrcpy-server、adb.exe 及全部 DLL
   └─ terminal-1.25.2733.0/        # WindowsTerminal.exe 等
```

两个工具 zip 包里各自带一层同名目录（`scrcpy-win64-v5.0/`、`terminal-1.25.2733.0/`），直接解压到 `tool/` 下即可。Python 的 zip 没有外层目录，要解压到 `lib/python-3.14.8-embed-amd64/` 里。

## 5. 搭建步骤（在项目根目录执行）

1. **解压 Python**：把 `python-3.14.8-embed-amd64.zip` 解压到 `lib\python-3.14.8-embed-amd64\`。
2. **启用 site-packages**：把 `lib\python-3.14.8-embed-amd64\python314._pth` 改成下面这样（加一行 `Lib\site-packages`，并取消 `import site` 的注释）。不改的话，pip 和 `Lib\site-packages` 都不会生效。

   ```text
   python314.zip
   .
   Lib\site-packages

   # Uncomment to run site.main() automatically
   import site
   ```
3. **配置 pip 镜像（可选）**：在同一目录新建 `pip.ini`，例如：

   ```ini
   [global]
   index-url = https://pypi.tuna.tsinghua.edu.cn/simple
   ```

4. **安装 pip 和依赖**：始终用 `python.exe -m pip`，不要依赖 `Scripts` 目录是否在 PATH 中。

   ```bat
   lib\python-3.14.8-embed-amd64\python.exe get-pip.py
   lib\python-3.14.8-embed-amd64\python.exe -m pip install -r requirements.txt
   ```

5. **放置工具**：把 Scrcpy 和 Windows Terminal 的 zip 解压到 `tool\` 下，并保持上面的目录名。ADB 默认只使用 `tool\scrcpy-win64-v5.0\adb.exe`；要改用其他 ADB，可以设置 `ADB` / `ADBUTILS_ADB_PATH` 环境变量（见 README）。

## 6. 验证

```bat
lib\python-3.14.8-embed-amd64\python.exe --version
lib\python-3.14.8-embed-amd64\python.exe -c "import PySide6; print(PySide6.__version__)"
lib\python-3.14.8-embed-amd64\python.exe -m pip --version
lib\python-3.14.8-embed-amd64\python.exe -s -m pytest tests -q
```

然后双击 `start_android_toolbox.bat`，或者运行 `lib\python-3.14.8-embed-amd64\python.exe -s android_toolbox.py`，确认桌面程序能正常启动。

## 7. 构建与发布

```bat
lib\python-3.14.8-embed-amd64\python.exe -s build_android_toolbox.py
```

构建只能在 Windows x64 上，使用上面这个嵌入式解释器（3.14.8，带 `-s`）运行。生成的 `dist\AndroidToolbox-win-x64.zip` 上传到 GitHub Releases 供最终用户下载。

## 8. 用 GitHub Actions 自动构建

仓库里的 `.github/workflows/build.yml` 会在 GitHub 的 Windows 机器上，从零搭好环境、运行测试，然后打出便携版。

**触发方式**

- **手动构建（不发布）**：打开仓库的 **Actions** 页面，左侧选 **Build portable**，点 **Run workflow**，选好分支后运行。
- **发版**：在本地打标签并推送，就会自动构建并创建 Release：

  ```bat
  git tag v0.1.0
  git push origin v0.1.0
  ```

**流程**：从官方地址下载 Python 3.14.8 嵌入版、Scrcpy 5.0 和 Windows Terminal 1.25.2733.0，并逐个校验 SHA-256（不一致会直接失败），再执行 `pip install -r requirements.txt`（CI 不使用镜像）、`pytest`，最后运行 `build_android_toolbox.py`。下载内容会缓存，下次构建更快。

**产物在哪里**

- 每次构建都会在该次运行页面底部的 **Artifacts** 里留下 `AndroidToolbox-win-x64-<版本>`，包含 `AndroidToolbox-win-x64-<版本>.zip` 及其 `.sha256` 校验文件。`<版本>` 在推送标签时是标签名（如 `v0.1.0`），手动构建时是提交的短 SHA。Artifact 默认保留 90 天。
- 推送 `v*` 标签时，同样的文件还会附加到 [Releases](https://github.com/cschengliang/SysDroid-Win/releases) 中对应版本的页面，更新说明会根据提交记录自动生成。
- 构建失败时，可以在 Artifacts 里下载 `build-logs-*`，查看 `failure.json` 和 PyInstaller 日志。
