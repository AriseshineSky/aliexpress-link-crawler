@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "ROOT=%CD%"

echo Stopping AliExpress crawler processes...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = '%ROOT:\=\\%';" ^
  "Get-CimInstance Win32_Process | Where-Object {" ^
  "  $_.CommandLine -and (" ^
  "    $_.CommandLine -like '*alilj.py*' -or" ^
  "    ($_.CommandLine -like '*chrome*' -and $_.CommandLine -like \"*$root*browser*\")" ^
  "  )" ^
  "} | ForEach-Object {" ^
  "  Write-Host ('  stop PID ' + $_.ProcessId);" ^
  "  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue" ^
  "}"

if exist "%ROOT%\browser\SingletonLock" del /f /q "%ROOT%\browser\SingletonLock" >nul 2>&1
if exist "%ROOT%\browser\SingletonSocket" del /f /q "%ROOT%\browser\SingletonSocket" >nul 2>&1
if exist "%ROOT%\browser\SingletonCookie" del /f /q "%ROOT%\browser\SingletonCookie" >nul 2>&1
if exist "%ROOT%\browser\lockfile" del /f /q "%ROOT%\browser\lockfile" >nul 2>&1

echo Done.
timeout /t 2 >nul
endlocal
