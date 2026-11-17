@echo off
REM ResearchPilot AI - Windows launcher
REM Double-click this file, or run it in Anaconda Prompt.
REM It reuses the existing conda env "noisepy" and starts Streamlit.
REM
REM Port policy (fixed, do not change casually):
REM   WSL dev instance      8501   (WSL only, not started from here)
REM   WSL acceptance copy   8502   (WSL only)
REM   Windows official      8601   <-- this file, bound to 127.0.0.1
REM Why 8601: WSL2 forwards localhost:8501 to the WSL VM. If this launcher also
REM used 8501, the browser could reach the WSL instance instead, which shows the
REM WSL smoke-test projects and looks like "my projects disappeared".
REM This launcher never falls back to another port: a silent port change would
REM recreate the exact confusion it is meant to prevent.

setlocal
cd /d "%~dp0"
chcp 65001 >nul

set "APP_PORT=8601"
set "APP_ADDR=127.0.0.1"

echo.
echo ============================================
echo   ResearchPilot AI - Windows launcher
echo ============================================
echo Working directory: %CD%
echo Access address   : http://%APP_ADDR%:%APP_PORT%/
echo.

where conda >nul 2>nul
if errorlevel 1 (
    echo [ERROR] conda not found in PATH.
    echo Open "Anaconda Prompt" and run this file from there, or add
    echo your Miniconda/Anaconda Scripts folder to PATH.
    pause
    exit /b 1
)

call conda env list | findstr /R /C:"^noisepy " >nul 2>nul
if errorlevel 1 (
    echo [ERROR] conda env "noisepy" not found.
    echo.
    echo This project is meant to run in the existing "noisepy" environment.
    echo If you really want a separate environment, create it and run:
    echo     conda create -n rp -y python=3.11
    echo     conda activate rp
    echo     pip install -r requirements.txt
    echo     python -m streamlit run app.py --server.port %APP_PORT% --server.address %APP_ADDR%
    pause
    exit /b 1
) else (
    echo [1/4] Using existing conda env "noisepy".
)

call conda activate noisepy
if errorlevel 1 (
    echo [ERROR] Cannot activate env "noisepy".
    echo If you are in cmd.exe, run "conda init cmd.exe" once and reopen.
    pause
    exit /b 1
)

echo [2/4] Checking dependencies ...
python -c "import streamlit, pymupdf, requests, pydantic" >nul 2>nul
if errorlevel 1 (
    echo       Some dependency is missing. Installing from requirements.txt ...
    echo       NOTE: this installs into the shared "noisepy" environment.
    python -m pip install -r requirements.txt
    if errorlevel 1 goto :failed
) else (
    echo       Dependencies already installed.
)

echo [3/4] Checking port %APP_PORT% ...
REM 用 Get-NetTCPConnection 而不是 netstat+findstr：后者受管道、引号与
REM 本地化输出影响，实测在部分调用方式下会漏判（端口明明被占用却报空闲）。
powershell -NoProfile -Command "if (Get-NetTCPConnection -State Listen -LocalPort %APP_PORT% -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"
if errorlevel 1 (
    echo.
    echo [ERROR] Port %APP_PORT% is already in use - refusing to start.
    echo.
    echo Something is already listening on %APP_PORT%. It could be:
    echo   - another ResearchPilot window you left open
    echo   - the WSL instance, if it was reconfigured to %APP_PORT%
    echo.
    echo Find the process with:
    echo     powershell -NoProfile -Command "Get-NetTCPConnection -State Listen -LocalPort %APP_PORT% ^| Select-Object OwningProcess"
    echo Then close it, or stop it with:
    echo     taskkill /PID ^<the-pid-shown-above^> /F
    echo.
    echo This launcher will NOT pick another port automatically: doing so would
    echo hide which database your browser is talking to.
    pause
    exit /b 1
)
echo       Port %APP_PORT% is free.

echo [4/4] Database identity ...
python scripts\preflight_windows.py
if errorlevel 1 (
    echo [WARN] Preflight check failed; continuing anyway.
)

echo.
echo Starting Streamlit ...
echo.
echo   Open your browser at:  http://%APP_ADDR%:%APP_PORT%/
echo   Press Ctrl+C in this window to stop.
echo.
REM The project's .streamlit\config.toml sets headless=true for WSL use.
REM Override it here so Windows opens the browser. showEmailPrompt stays false.
python -m streamlit run app.py --server.port %APP_PORT% --server.address %APP_ADDR% --server.headless false
goto :eof

:failed
echo.
echo [ERROR] Setup failed. Read the messages above.
pause
exit /b 1
