param([switch]$ValidateOnly)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$serviceOutput = Join-Path $projectRoot 'build\service\noteford-service'
$serviceExe = Join-Path $serviceOutput 'noteford-service.exe'
$buildPython = Join-Path $projectRoot '.venv-build\Scripts\python.exe'

$package = Get-Content -LiteralPath (Join-Path $projectRoot 'desktop/package.json') -Raw | ConvertFrom-Json
$lockVersionsJson = & node -e "const lock=JSON.parse(require('fs').readFileSync(process.argv[1],'utf8')); console.log(JSON.stringify([lock.version,lock.packages[''].version]));" (Join-Path $projectRoot 'desktop/package-lock.json')
if ($LASTEXITCODE -ne 0) { throw 'Unable to read npm lockfile.' }
$lockVersions = $lockVersionsJson | ConvertFrom-Json
$pythonProject = Get-Content -LiteralPath (Join-Path $projectRoot 'pyproject.toml') -Raw
$pythonVersion = [regex]::Match($pythonProject, '(?m)^version = "([^"]+)"').Groups[1].Value
if (-not $pythonVersion -or $package.version -ne $pythonVersion -or $lockVersions[0] -ne $package.version -or $lockVersions[1] -ne $package.version) {
    throw 'Release versions do not match across Python, desktop and lockfile.'
}
foreach ($asset in @('desktop/assets/noteford.ico', 'packaging/service-entry.py', 'requirements-build.txt', 'NOTICE.md', 'docs/DATA-AND-SECURITY.md')) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $asset) -PathType Leaf)) { throw "Missing build input: $asset" }
}
if ($ValidateOnly) {
    Write-Output "Release inputs validated: $($package.version). No package built."
    return
}

Push-Location $projectRoot
try {
    if (-not (Test-Path -LiteralPath $buildPython -PathType Leaf)) {
        python -m venv (Join-Path $projectRoot '.venv-build')
        if ($LASTEXITCODE -ne 0) { throw 'Build environment creation failed.' }
    }
    & $buildPython -I (Join-Path $projectRoot 'Scripts/bootstrap-build-pip.py')
    if ($LASTEXITCODE -ne 0) { throw 'Build pip security bootstrap failed.' }
    & $buildPython -m pip install --require-virtualenv --disable-pip-version-check -r (Join-Path $projectRoot 'requirements-build.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    & $buildPython -m PyInstaller --noconfirm --onedir --name noteford-service `
        --paths (Join-Path $projectRoot 'src') `
        --distpath (Join-Path $projectRoot 'build\service') `
        --workpath (Join-Path $projectRoot 'build\pyinstaller-work') `
        --specpath (Join-Path $projectRoot 'build') `
        (Join-Path $projectRoot 'packaging\service-entry.py')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $serviceExe -PathType Leaf)) {
        throw 'Python service packaging failed.'
    }

    Push-Location (Join-Path $projectRoot 'desktop')
    try {
        # Always restore the lockfile dependency set for a release build.
        node scripts/install-dependencies.cjs
        if ($LASTEXITCODE -ne 0) { throw 'Secure npm dependency installation failed.' }
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'desktop/node_modules/electron/dist/LICENSE'))) {
            node node_modules/electron/install.js
            if ($LASTEXITCODE -ne 0) { throw 'Electron runtime installation failed.' }
        }
        npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Renderer build failed.' }
        npm.cmd run package:win
        if ($LASTEXITCODE -ne 0) { throw 'Windows packaging failed.' }
    }
    finally {
        Pop-Location
    }
}
finally {
    Pop-Location
}
