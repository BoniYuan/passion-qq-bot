@echo off
chcp 65001 >nul
title 部署并打开机器人后台 v0.7.4

echo [1/3] 正在上传机器人插件（仅响应 @ 消息）...
echo 请输入一次服务器密码。
scp "%~dp0..\plugins\astrbot_plugin_passion_admin\main.py" "%~dp0..\plugins\astrbot_plugin_passion_admin\metadata.yaml" "%~dp0..\plugins\astrbot_plugin_passion_admin\requirements.txt" "%~dp0..\plugins\astrbot_plugin_passion_admin\_conf_schema.json" root@43.134.235.139:/opt/passion-bot/plugins/astrbot_plugin_passion_admin/
if errorlevel 1 goto failed
scp "%~dp0..\plugins\astrbot_plugin_passion_faq\main.py" "%~dp0..\plugins\astrbot_plugin_passion_faq\metadata.yaml" "%~dp0..\plugins\astrbot_plugin_passion_faq\faq.json" root@43.134.235.139:/opt/passion-bot/plugins/astrbot_plugin_passion_faq/
if errorlevel 1 goto failed

echo.
echo [2/3] 即将重启 AstrBot 并建立后台安全通道...
echo 请再次输入服务器密码。连接成功后不要关闭此窗口。
start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 10; Start-Process 'http://127.0.0.1:6185'"

echo.
echo [3/3] 后台地址：http://127.0.0.1:6185
echo 按 Ctrl+C 可以关闭后台连接。
ssh -tt -L 6185:127.0.0.1:6185 root@43.134.235.139 "python3 /opt/passion-bot/plugins/astrbot_plugin_passion_admin/configure-technical-assistant.py && cd /opt/passion-bot && docker compose up -d astrbot && echo AstrBot started. Keep this window open. && tail -f /dev/null"
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo 操作失败。请查看上方错误信息并截图发给我。
pause
exit /b 1
