@echo off
setlocal
title GoAnywhere
cd /d "%~dp0"

rem --- First run: build the venv if it's missing ---
if not exist ".venv\Scripts\pythonw.exe" (
    echo [GoAnywhere] .venv not found - setting it up, this takes a minute...
    if exist "%USERPROFILE%\.local\bin\uv.exe" (
        "%USERPROFILE%\.local\bin\uv.exe" venv --python 3.12 .venv || goto :fail
        "%USERPROFILE%\.local\bin\uv.exe" pip install --python .venv\Scripts\python.exe -r requirements.txt || goto :fail
    ) else (
        echo [GoAnywhere] uv not found, falling back to python -m venv + pip
        py -3.12 -m venv .venv 2>nul || python -m venv .venv || goto :fail
        ".venv\Scripts\python.exe" -m pip install --upgrade pip
        ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
    )
)

rem --- Kill any old server.py still hanging about ---
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -match 'server\.py|desktop_app\.py'} | ForEach-Object {Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}" >nul 2>&1

rem --- Start the desktop app (window + server inside, no console) ---
echo [GoAnywhere] Starting GoAnywhere...
start "GoAnywhere" /min ".venv\Scripts\pythonw.exe" desktop_app.py
timeout /t 3 /nobreak >nul
endlocal
exit /b 0

:fail
echo.
echo [GoAnywhere] Setup failed - check the messages above.
pause
endlocal
exit /b 1
