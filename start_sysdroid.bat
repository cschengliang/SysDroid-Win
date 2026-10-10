@echo off
setlocal

cd /d "%~dp0"
set "PYTHON=%~dp0lib\python-3.14.8-embed-amd64\python.exe"
set "APP=%~dp0sysdroid.py"

if not exist "%PYTHON%" (
    echo [ERROR] Embedded Python was not found:
    echo         %PYTHON%
    pause
    exit /b 1
)

if not exist "%APP%" (
    echo [ERROR] SysDroid entry point was not found:
    echo         %APP%
    pause
    exit /b 1
)

"%PYTHON%" -s "%APP%" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] SysDroid exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
