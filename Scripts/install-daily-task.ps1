param(
    [string]$Time = "23:50",
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$TaskName = "Noteford Daily",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    schtasks.exe /Delete /TN $TaskName /F
    exit $LASTEXITCODE
}

if ($Time -notmatch '^(?:[01]\\d|2[0-3]):[0-5]\\d$') {
    throw "Time must be HH:mm in 24-hour format."
}

$script = Join-Path $ProjectRoot "Scripts\\run-daily.ps1"
if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
    throw "Daily runner not found: $script"
}

# This installer is intentionally separate from normal setup. Do not register
# the task until BotDock supports and consumes taskType=wechat.daily.digest.
$action = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + $script + '"'
schtasks.exe /Create /TN $TaskName /TR $action /SC DAILY /ST $Time /F /RL LIMITED
exit $LASTEXITCODE
