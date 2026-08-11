@echo off
setlocal
cd /d "%~dp0"
echo === GPN AlertHub tests ===
where py >nul 2>nul
if %errorlevel%==0 (
    py -m unittest -v test_unit.py test_integration.py
    goto :end
)
where python >nul 2>nul
if %errorlevel%==0 (
    python -m unittest -v test_unit.py test_integration.py
    goto :end
)
echo Python not found.
:end
pause
