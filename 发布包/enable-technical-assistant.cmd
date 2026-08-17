@echo off
title Enable Technical Group Assistant
echo Step 1 of 2: Uploading the assistant configuration.
echo Enter the server password when prompted.
scp "%~dp0configure-technical-assistant.py" root@43.134.235.139:/tmp/configure-technical-assistant.py
if errorlevel 1 goto failed

echo.
echo Step 2 of 2: Applying settings and restarting AstrBot.
echo Enter the server password again when prompted.
ssh root@43.134.235.139 "python3 /tmp/configure-technical-assistant.py && cd /opt/passion-bot && docker compose restart astrbot"
if errorlevel 1 goto failed

echo.
echo Update completed. The technical group assistant persona is active.
pause
exit /b 0

:failed
echo.
echo Update failed. Review the error shown above.
pause
exit /b 1
