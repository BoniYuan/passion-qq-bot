@echo off
setlocal
title Install Docker Desktop

fltmc >nul 2>&1
if errorlevel 1 (
  echo ERROR: Administrator permission is required.
  echo.
  echo Close this window, then right-click this file and choose:
  echo Run as administrator
  echo.
  pause
  exit /b 1
)

echo [1/3] Updating WSL...
wsl.exe --update

echo.
echo [2/3] Setting WSL 2 as default...
wsl.exe --set-default-version 2

echo.
echo [3/3] Installing Docker Desktop...
winget.exe install --id Docker.DockerDesktop --exact --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto failed

echo.
echo SUCCESS. Open Docker Desktop from the Start menu.
echo Wait until Docker reports that it is running, then return to Codex.
echo.
pause
exit /b 0

:failed
echo.
echo FAILED. Take a photo of this window and send it to Codex.
echo.
pause
exit /b 1
