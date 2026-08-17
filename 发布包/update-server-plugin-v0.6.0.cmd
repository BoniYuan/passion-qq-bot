@echo off
title Update Passion Bot Plugin v0.6.0
echo Step 1 of 2: Uploading plugin files to the server.
echo Enter the server password when prompted.
scp "%~dp0..\plugins\astrbot_plugin_passion_admin\main.py" "%~dp0..\plugins\astrbot_plugin_passion_admin\metadata.yaml" "%~dp0..\plugins\astrbot_plugin_passion_admin\requirements.txt" "%~dp0..\plugins\astrbot_plugin_passion_admin\_conf_schema.json" root@43.134.235.139:/opt/passion-bot/plugins/astrbot_plugin_passion_admin/
if errorlevel 1 goto failed

echo.
echo Step 2 of 2: Restarting AstrBot on the server.
echo Enter the server password again when prompted.
ssh root@43.134.235.139 "cd /opt/passion-bot && docker compose restart astrbot"
if errorlevel 1 goto failed

echo.
echo Update completed. AstrBot has been restarted.
echo Wait 10 seconds, then send one email address to the bot.
pause
exit /b 0

:failed
echo.
echo Update failed. Review the error shown above.
pause
exit /b 1
