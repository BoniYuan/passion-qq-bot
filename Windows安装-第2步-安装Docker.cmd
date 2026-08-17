@echo off
chcp 65001 >nul
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo 正在请求管理员权限，请在弹窗中选择“是”...
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo 正在检查 WSL...
wsl.exe --update
wsl.exe --set-default-version 2

echo.
echo 正在通过 Windows 软件源安装 Docker Desktop...
winget.exe install --id Docker.DockerDesktop --exact --accept-package-agreements --accept-source-agreements
if %errorlevel% neq 0 goto :failed

echo.
echo Docker Desktop 已安装。请从开始菜单打开 Docker Desktop，接受协议并等待左下角显示运行正常。
echo 然后回到 Codex 告诉我：“Docker 已打开”。
pause
exit /b 0

:failed
echo.
echo Docker 自动安装失败。请拍下这个窗口并发给 Codex。
pause
exit /b 1
