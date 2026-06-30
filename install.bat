@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "ROOT=%CD%"

echo ============================================================
echo AliExpress Link Crawler - Windows Install
echo ============================================================
echo Project: %ROOT%
echo.

where py >nul 2>&1
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    set "PY=python"
)

echo [1/6] Checking Python...
%PY% --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3 not found. Install Python 3.10+ from https://www.python.org/downloads/
    echo        Enable "Add python.exe to PATH" during installation.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('%PY% -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"') do set "PY_VER=%%v"
echo        Python %PY_VER%

echo [2/6] Creating virtual environment...
if not exist "%ROOT%\.venv\Scripts\python.exe" (
    %PY% -m venv "%ROOT%\.venv"
    if errorlevel 1 (
        echo ERROR: Failed to create venv.
        pause
        exit /b 1
    )
) else (
    echo        .venv already exists, skip create.
)

set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"
set "VENV_PIP=%ROOT%\.venv\Scripts\pip.exe"

echo [3/6] Upgrading pip...
"%VENV_PIP%" install --upgrade pip >nul

echo [4/6] Installing Python packages...
"%VENV_PIP%" install -r "%ROOT%\requirements.txt"
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause
    exit /b 1
)

echo [5/6] Installing Playwright Chromium...
"%ROOT%\.venv\Scripts\playwright.exe" install chromium
if errorlevel 1 (
    echo ERROR: playwright install chromium failed.
    pause
    exit /b 1
)

echo [6/6] Preparing directories and config...
if not exist "%ROOT%\data" mkdir "%ROOT%\data"
if not exist "%ROOT%\.tmp" mkdir "%ROOT%\.tmp"
if not exist "%ROOT%\browser" mkdir "%ROOT%\browser"
if not exist "%ROOT%\.env" (
    if exist "%ROOT%\.env.example" (
        copy /y "%ROOT%\.env.example" "%ROOT%\.env" >nul
        echo        Created .env from .env.example - please edit Elasticsearch settings.
    ) else (
        echo        WARNING: .env not found. Create .env before running the crawler.
    )
) else (
    echo        .env already exists.
)

echo.
echo ============================================================
echo Install complete.
echo.
echo Next steps:
echo   1. Edit .env and set ELASTICSEARCH_URL / ELASTICSEARCH_INDEX_URLS
echo   2. Double-click run_crawl.bat to start crawling
echo   3. Use stop_crawl.bat to stop the crawler
echo ============================================================
pause
endlocal
