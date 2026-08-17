@echo off
chcp 65001 >nul
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo 正在请求管理员权限，请在弹窗中选择“是”...
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo [1/2] 正在启用 Windows Linux 子系统...
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
if %errorlevel% neq 0 goto :failed

echo [2/2] 正在启用虚拟机平台...
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
if %errorlevel% neq 0 goto :failed

echo.
echo 第一步完成。请现在重启电脑。
echo 重启后，双击“Windows安装-第2步-安装Docker.cmd”。
pause
exit /b 0

:failed
echo.
echo 启用失败。请拍下这个窗口并发给 Codex。
pause
exit /b 1
