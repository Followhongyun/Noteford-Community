param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [int]$Port = 17890
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectRoot
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
$python = (Get-Command python -ErrorAction Stop).Source
& $python -m noteford.cli serve-api --config (Join-Path $ProjectRoot "config.local.json") --port $Port
exit $LASTEXITCODE
