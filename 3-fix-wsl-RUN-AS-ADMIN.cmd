@echo off
setlocal
title Install missing WSL package

fltmc >nul 2>&1
if errorlevel 1 (
  echo ERROR: Administrator permission is required.
  echo.
  echo Close this window, right-click this file, and choose Run as administrator.
  echo.
  pause
  exit /b 1
)

echo Close Docker Desktop before continuing.
echo.
echo [1/2] Installing the WSL application package...
wsl.exe --install --no-distribution
set "wsl_result=%errorlevel%"
if "%wsl_result%"=="0" goto installed
if "%wsl_result%"=="3010" goto installed

echo.
echo The standard WSL installer did not complete. Trying Windows Package Manager...
winget.exe install --id Microsoft.WSL --exact --accept-package-agreements --accept-source-agreements
set "winget_result=%errorlevel%"
if "%winget_result%"=="0" goto installed

goto failed

:installed
echo.
echo [2/2] Setting WSL 2 as the default...
wsl.exe --set-default-version 2
echo.
echo SUCCESS. Restart Windows, then open Docker Desktop again.
echo.
pause
exit /b 0

:failed
echo.
echo FAILED. Take a photo of this entire window and send it to Codex.
echo Error codes: WSL=%wsl_result% WINGET=%winget_result%
echo.
pause
exit /b 1
