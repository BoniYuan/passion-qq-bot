param(
    [string]$Archive = (Join-Path $PSScriptRoot "..\napcat-image-amd64.tar")
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Archive)) {
    throw "NapCat image archive not found: $Archive"
}

docker load --input $Archive
docker compose --profile qq up -d napcat
docker compose ps napcat
