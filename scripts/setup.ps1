# One-shot setup and run for Windows PowerShell.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path .env)) {
    Write-Host "creating .env from .env.example - fill in BOT_TOKEN before rerunning" -ForegroundColor Yellow
    Copy-Item .env.example .env
    exit 1
}

if (-not (Test-Path .venv)) {
    python -m venv .venv
}
& .\.venv\Scripts\Activate.ps1

python -m pip install --quiet --upgrade pip
python -m pip install --quiet -e ".[dev]"

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "warning: FFmpeg not found - merging, converting and splitting large files will be unavailable" -ForegroundColor Yellow
}

Write-Host "== doctor ==" -ForegroundColor Cyan
grabber doctor
Write-Host ""
Write-Host "run 'grabber run' to start the bot" -ForegroundColor Green
