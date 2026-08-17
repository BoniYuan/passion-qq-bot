@echo off
title Deploy and Open Bot Admin v0.7.3
set "ERROR_LOG=%~dp0OPEN-BOT-ADMIN-error.txt"

where ssh.exe >nul 2>nul
if errorlevel 1 (
  echo ERROR: Windows OpenSSH is not installed or not available.
  echo OpenSSH missing > "%ERROR_LOG%"
  goto failed
)
where scp.exe >nul 2>nul
if errorlevel 1 (
  echo ERROR: Windows SCP is not installed or not available.
  echo SCP missing > "%ERROR_LOG%"
  goto failed
)

echo [1/3] Uploading Passion bot plugin v0.7.3...
echo Enter the server password. Password characters will not be displayed.
scp "%~dp0..\plugins\astrbot_plugin_passion_admin\main.py" "%~dp0..\plugins\astrbot_plugin_passion_admin\metadata.yaml" "%~dp0..\plugins\astrbot_plugin_passion_admin\requirements.txt" "%~dp0..\plugins\astrbot_plugin_passion_admin\_conf_schema.json" "%~dp0configure-technical-assistant.py" root@43.134.235.139:/opt/passion-bot/plugins/astrbot_plugin_passion_admin/
if errorlevel 1 (
  echo Upload failed > "%ERROR_LOG%"
  goto failed
)

echo.
echo [2/3] Starting AstrBot and opening a secure SSH tunnel...
echo Enter the server password again. Keep this window open after login.
start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 10; Start-Process 'http://127.0.0.1:6185'"

echo.
echo [3/3] Admin URL: http://127.0.0.1:6185
echo Press Ctrl+C to close the admin tunnel. The server bot will keep running.
ssh -tt -L 6185:127.0.0.1:6185 root@43.134.235.139 "python3 /opt/passion-bot/plugins/astrbot_plugin_passion_admin/configure-technical-assistant.py && cd /opt/passion-bot && docker compose up -d astrbot && echo AstrBot started. Keep this window open. && tail -f /dev/null"
if errorlevel 1 (
  echo SSH tunnel or remote startup failed > "%ERROR_LOG%"
  goto failed
)
goto finished

:failed
echo.
echo The operation failed. Send me a screenshot of this window.
echo Error log: "%ERROR_LOG%"
pause
exit /b 1

:finished
echo.
echo The SSH connection has closed.
pause
exit /b 0
