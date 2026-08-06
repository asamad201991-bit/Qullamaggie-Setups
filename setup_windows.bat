@echo off
echo Installing required Python packages (this may take a minute)...
echo.
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Something went wrong. If you saw an "externally-managed-environment"
    echo error, close this window, hold Shift and right-click this folder,
    echo choose "Open PowerShell window here", then run:
    echo     pip install -r requirements.txt --break-system-packages
    pause
    exit /b 1
)
echo.
echo ============================================================
echo All set! You can now double-click qullamaggie_scanner.py
echo to run your first scan.
echo ============================================================
pause
