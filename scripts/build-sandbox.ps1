param(
    [string]$PythonImage = "python:3.12-slim",
    [string]$DebianMirror = "deb.debian.org",
    [string]$PipIndexUrl = "https://pypi.org/simple"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$docker = (Get-Command docker -ErrorAction SilentlyContinue).Source
if (-not $docker) {
    $docker = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
}
if (-not (Test-Path -LiteralPath $docker)) {
    throw "Docker CLI not found. Install and start Docker Desktop first."
}
Push-Location $repoRoot
try {
    & $docker build `
        --progress plain `
        --build-arg "PYTHON_IMAGE=$PythonImage" `
        --build-arg "DEBIAN_MIRROR=$DebianMirror" `
        --build-arg "PIP_INDEX_URL=$PipIndexUrl" `
        -f sandbox/Dockerfile `
        -t meoclaw-sandbox:latest `
        .
    if ($LASTEXITCODE -ne 0) {
        throw "Docker image build failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
