$ErrorActionPreference = "Stop"

if (Test-Path -LiteralPath ".env") {
    Write-Host ".env already exists; keeping it."
} else {
    $bytes = New-Object byte[] 32
    $rng = New-Object Security.Cryptography.RNGCryptoServiceProvider
    $rng.GetBytes($bytes)
    $rng.Dispose()
    $webToken = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    $template = [IO.File]::ReadAllText((Join-Path $PWD ".env.example"), [Text.Encoding]::UTF8)
    $template = $template.Replace("change-this-to-a-long-random-string", $webToken)
    [IO.File]::WriteAllText((Join-Path $PWD ".env"), $template, (New-Object Text.UTF8Encoding($false)))
    Write-Host "Created .env with a random NapCat WebUI token."
}

try {
    $key = & python (Join-Path $PWD "tools/generate_key.py")
    [IO.File]::WriteAllText(
        (Join-Path $PWD "setup-secrets.txt"),
        "AstrBot sub2 plugin encryption_key:`r`n$key`r`n",
        (New-Object Text.UTF8Encoding($false))
    )
    Write-Host "Created setup-secrets.txt. Enter its key in the sub2 plugin settings."
} catch {
    Write-Warning "Could not generate the plugin key. Run: python tools/generate_key.py"
}
