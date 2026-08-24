$ErrorActionPreference = "Continue"

$sshExe = Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe"
$keyPath = Join-Path $PSScriptRoot "..\.ssh-passion\passion_bot_tunnel"
$logPath = Join-Path $PSScriptRoot "astrbot-tunnel.log"

while ($true) {
    $startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPath -Value "$startedAt starting SSH tunnel"
    $sshArgs = @(
        "-NT",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        "-L", "16199:127.0.0.1:6199",
        "-L", "16191:127.0.0.1:6191",
        "-i", $keyPath,
        "root@43.134.235.139"
    )
    & $sshExe @sshArgs 2>> $logPath
    $exitCode = $LASTEXITCODE
    $stoppedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPath -Value "$stoppedAt tunnel exited ($exitCode); retrying in 5 seconds"
    Start-Sleep -Seconds 5
}
