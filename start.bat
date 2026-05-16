@echo off
chcp 65001 >nul
title contactPutianHospital 

echo.
echo    ========================================
echo       contactPutianHospital
echo    ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found!
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do echo [OK] Python %%v

echo.
echo [INFO] Installing dependencies...
python -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies
    pause
    exit /b 1
)
echo [OK] Dependencies ready

if not exist "config.yaml" (
    echo.
    echo [WARN] config.yaml not found!
    echo        Run: copy config.example.yaml config.yaml
    echo        Then edit config.yaml with your phone number.
    pause
    exit /b 1
)
echo [OK] config.yaml found

echo.
echo Select mode:
echo   [1] ??? - ??????????? (main.py)
echo   [2] ??? - ????????URL (catchad)
echo   [3] ??? - ??09:00???? (scheduler)
echo.

:choose
set /p mode="Enter [1/2/3]: "

if "%mode%"=="1" (
    echo [INFO] Starting main...
    python main.py
    goto end
)
if "%mode%"=="2" (
    echo [INFO] Starting catch...
    cd catchad
    python catch.py
    cd ..
    goto end
)
if "%mode%"=="3" (
    echo [INFO] Starting scheduler...
    python scheduler.py
    goto end
)
echo [ERROR] Invalid, enter 1, 2 or 3
goto choose

:end
echo.
echo [INFO] Done
pause
