@echo off
rem GoAnywhere Game Mode - keeps the iPhone trusting the spoofed location
rem (Bluetooth iAP2 accessory). Leave this window open while playing.
rem It follows the position shown in the GoAnywhere app automatically.
cd /d "%~dp0"
:loop
".venv\Scripts\python.exe" game_mode.py send
echo Game Mode stopped (phone off or BT issue). Retrying in 20s...
timeout /t 20 >nul
goto loop
