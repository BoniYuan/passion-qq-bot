@echo off
title Complete Passion Bot Features v0.7.0
echo Step 1 of 2: Uploading bot features and assistant configuration.
echo Enter the server password when prompted.
scp "%~dp0..\plugins\astrbot_plugin_passion_admin\main.py" "%~dp0..\plugins\astrbot_plugin_passion_admin\metadata.yaml" "%~dp0..\plugins\astrbot_plugin_passion_admin\requirements.txt" "%~dp0..\plugins\astrbot_plugin_passion_admin\_conf_schema.json" "%~dp0configure-technical-assistant.py" root@43.134.235.139:/opt/passion-bot/plugins/astrbot_plugin_passion_admin/
if errorlevel 1 goto failed

echo.
echo Step 2 of 2: Applying the persona and restarting AstrBot.
echo Enter the server password again when prompted.
ssh root@43.134.235.139 "python3 /opt/passion-bot/plugins/astrbot_plugin_passion_admin/configure-technical-assistant.py && cd /opt/passion-bot && docker compose restart astrbot"
if errorlevel 1 goto failed

echo.
echo Update completed. Wait 10 seconds, then test in QQ.
pause
exit /b 0

:failed
echo.
echo Update failed. Review the error shown above.
pause
exit /b 1
