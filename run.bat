@echo off
setlocal
cd /d "%~dp0"
echo === GPN AlertHub FINAL V7 FINAL V2 ===
where py >nul 2>nul
if %errorlevel%==0 (
    py server.py
    goto :end
)
where python >nul 2>nul
if %errorlevel%==0 (
    python server.py
    goto :end
)
echo Python not found.
:end
pause
