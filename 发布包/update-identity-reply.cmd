@echo off
title Update Identity Reply Plugin
echo Step 1 of 2: Uploading the identity reply plugin.
echo Enter the server password when prompted.
scp -r "%~dp0..\plugins\astrbot_plugin_identity_reply" root@43.134.235.139:/opt/passion-bot/data/plugins/
if errorlevel 1 goto failed

echo.
echo Step 2 of 2: Restarting AstrBot.
echo Enter the server password again when prompted.
ssh root@43.134.235.139 "python3 /opt/passion-bot/data/plugins/astrbot_plugin_identity_reply/configure_server.py && cd /opt/passion-bot && docker compose restart astrbot"
if errorlevel 1 goto failed

echo.
echo Update completed. Ask the bot: Who am I.
pause
exit /b 0

:failed
echo.
echo Update failed. Review the error shown above.
pause
exit /b 1
