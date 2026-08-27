$ErrorActionPreference = "Stop"

$image = "m.daocloud.io/docker.io/mlikiowa/napcat-docker@sha256:1336a777f9a4f1f8cb89fef42f7548deacd3645919a067a50df5b66b5e77390e"
$output = Join-Path $PSScriptRoot "..\napcat-image-amd64.tar"

docker pull $image
docker image inspect $image | Out-Null
docker save --output $output $image

Write-Host "NapCat offline image exported to: $output"
Write-Host "This archive contains third-party binaries. Keep it private and do not commit it to Git."
