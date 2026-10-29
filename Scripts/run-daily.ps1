param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectRoot
$env:PYTHONPATH = Join-Path $ProjectRoot "src"

$python = (Get-Command python -ErrorAction Stop).Source
& $python -m noteford.cli daily-all --config (Join-Path $ProjectRoot "config.local.json")
exit $LASTEXITCODE
