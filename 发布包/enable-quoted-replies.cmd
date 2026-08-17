@echo off
title Enable Mentioned and Quoted Replies
echo Step 1 of 2: Uploading the configuration helper.
echo Enter the server password when prompted.
scp "%~dp0configure-reply-target.py" root@43.134.235.139:/tmp/configure-reply-target.py
if errorlevel 1 goto failed

echo.
echo Step 2 of 2: Applying settings and restarting AstrBot.
echo Enter the server password again when prompted.
ssh root@43.134.235.139 "python3 /tmp/configure-reply-target.py && cd /opt/passion-bot && docker compose restart astrbot"
if errorlevel 1 goto failed

echo.
echo Update completed. Replies will quote and mention the sender.
pause
exit /b 0

:failed
echo.
echo Update failed. Review the error shown above.
pause
exit /b 1
