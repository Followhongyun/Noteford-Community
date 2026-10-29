param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
)

$ErrorActionPreference = "Stop"
$desktop = Join-Path $ProjectRoot "desktop"
$electron = Join-Path $desktop "node_modules\electron\dist\electron.exe"

if (-not (Test-Path -LiteralPath $electron -PathType Leaf)) {
    throw "Desktop dependencies are not installed. Run npm install in $desktop first."
}

if (-not (Test-Path -LiteralPath (Join-Path $desktop "dist\index.html") -PathType Leaf)) {
    Push-Location $desktop
    try {
        npm.cmd run build
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    finally {
        Pop-Location
    }
}

Start-Process -FilePath $electron -ArgumentList "." -WorkingDirectory $desktop
