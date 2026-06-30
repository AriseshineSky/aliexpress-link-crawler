@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0crawl.log" (
    echo crawl.log not found yet.
    pause
    exit /b 1
)
powershell -NoProfile -Command "Get-Content -Path '%~dp0crawl.log' -Wait -Tail 30"
