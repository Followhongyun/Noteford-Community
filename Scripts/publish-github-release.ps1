param(
    [Parameter(Mandatory = $true)]
    [string]$Notes
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$package = Get-Content -LiteralPath (Join-Path $projectRoot 'desktop\package.json') -Raw | ConvertFrom-Json
$version = [string]$package.version
$tag = "v$version"
$releaseRoot = Join-Path $projectRoot 'build\release'
$installer = Join-Path $releaseRoot "Noteford Setup $version.exe"
$blockmap = "$installer.blockmap"
$manifest = Join-Path $releaseRoot 'latest.yml'
$publicInstaller = Join-Path $releaseRoot "Noteford-Setup-$version.exe"
$publicBlockmap = "$publicInstaller.blockmap"

foreach ($file in @($installer, $blockmap, $manifest)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing release artifact: $file" }
}
$metadata = Get-Content -LiteralPath $manifest -Raw
$hashHex = (Get-FileHash -LiteralPath $installer -Algorithm SHA512).Hash
$hashBytes = for ($index = 0; $index -lt $hashHex.Length; $index += 2) {
    [Convert]::ToByte($hashHex.Substring($index, 2), 16)
}
$sha512 = [Convert]::ToBase64String([byte[]]$hashBytes)
if ($metadata -notmatch "(?m)^version: $([regex]::Escape($version))$" -or
    $metadata -notmatch "(?m)^\s+- url: Noteford-Setup-$([regex]::Escape($version))\.exe$" -or
    $metadata -notmatch "(?m)^\s+size: $((Get-Item -LiteralPath $installer).Length)$" -or
    $metadata -notmatch "(?m)^\s+sha512: $([regex]::Escape($sha512))$") {
    throw 'Update metadata does not match the installer version, filename, size, or SHA-512.'
}
$sourceVisibility = (gh repo view Followhongyun/Noteford --json visibility | ConvertFrom-Json).visibility
$updateVisibility = (gh repo view Followhongyun/WeChatLocal-Updates --json visibility | ConvertFrom-Json).visibility
if ($sourceVisibility -ne 'PRIVATE' -or $updateVisibility -ne 'PUBLIC') {
    throw 'Repository visibility does not match the private source/public binary boundary.'
}
$releases = gh release list --repo Followhongyun/WeChatLocal-Updates --json tagName --limit 100 | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Could not list existing GitHub releases.' }
if ($releases | Where-Object { $_.tagName -eq $tag }) { throw "Release $tag already exists." }

Copy-Item -LiteralPath $installer -Destination $publicInstaller -Force
Copy-Item -LiteralPath $blockmap -Destination $publicBlockmap -Force
try {
    gh release create $tag $publicInstaller $publicBlockmap $manifest `
        --repo Followhongyun/WeChatLocal-Updates `
        --title "Noteford $version" --notes $Notes --latest
    if ($LASTEXITCODE -ne 0) { throw 'GitHub release upload failed.' }
}
finally {
    Remove-Item -LiteralPath $publicInstaller,$publicBlockmap -Force -ErrorAction SilentlyContinue
}
