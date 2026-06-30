@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "ROOT=%CD%"
set "TMPDIR=%ROOT%\.tmp"
set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo Virtual environment not found. Run install.bat first.
    pause
    exit /b 1
)

if not exist "%ROOT%\.env" (
    echo .env not found. Copy .env.example to .env and configure Elasticsearch.
    pause
    exit /b 1
)

if not exist "%TMPDIR%" mkdir "%TMPDIR%"
if not exist "%ROOT%\data" mkdir "%ROOT%\data"

echo ============================================================
echo AliExpress Link Crawler (alilj.py)
echo ============================================================
echo Project : %ROOT%
echo Log file: %ROOT%\crawl.log
echo.

call "%ROOT%\stop_crawl.bat"

echo Starting crawler...
echo Browser window will open for captcha if needed.
echo.

"%VENV_PY%" -u "%ROOT%\alilj.py"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Crawler exited with code %EXIT_CODE%.
echo See crawl.log for details.
pause
exit /b %EXIT_CODE%

endlocal
