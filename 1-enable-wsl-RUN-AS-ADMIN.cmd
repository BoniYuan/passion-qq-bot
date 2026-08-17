@echo off
setlocal
title Enable WSL for Docker Desktop

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

echo [1/2] Enabling Windows Subsystem for Linux...
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
set "dism_result=%errorlevel%"
if not "%dism_result%"=="0" if not "%dism_result%"=="3010" goto failed

echo.
echo [2/2] Enabling Virtual Machine Platform...
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
set "dism_result=%errorlevel%"
if not "%dism_result%"=="0" if not "%dism_result%"=="3010" goto failed

echo.
echo SUCCESS. Restart Windows now.
echo After restart, run 2-install-docker-RUN-AS-ADMIN.cmd as administrator.
echo.
pause
exit /b 0

:failed
echo.
echo FAILED. Take a photo of this window and send it to Codex.
echo.
pause
exit /b 1
