param(
  [ValidateSet('connect','tools','call','serve')][string]$Action = 'connect',
  [Parameter(Mandatory=$true)][string]$ClientName,
  [string]$Tool,
  [string]$ArgumentsFile,
  [string]$ImageDirectory
)
$ErrorActionPreference = 'Stop'
$manifest = Join-Path $env:LOCALAPPDATA 'Noteford\connection.json'
# The running app repairs stale discovery records. Wait briefly for startup/repair;
# never substitute a PID, launch an unverified command, or alter authorization.
$connectionDeadline = [DateTime]::UtcNow.AddSeconds(7)
$appProcess = $null
while ([DateTime]::UtcNow -lt $connectionDeadline) {
  try {
    $record = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
    if ($record.product -ne 'Noteford' -or $record.version -ne 1) { throw 'invalid discovery' }
    $candidateProcess = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    if ($candidateProcess -and $candidateProcess.Path -eq $record.appExecutable) { $appProcess = $candidateProcess; break }
  } catch {
    # A partially replaced/missing record will be retried.
  }
  Start-Sleep -Milliseconds 250
}
if (!$appProcess) { throw 'Noteford 尚未就绪，请打开应用；连接信息恢复后可直接重试，无需重新授权。' }
$expected = Join-Path (Split-Path -Parent $appProcess.Path) 'resources\service\noteford-service.exe'
$launch = $expected
$prefix = @()
$exeMatch = ([IO.Path]::GetFullPath($record.command) -eq [IO.Path]::GetFullPath($expected)) -and (Test-Path -LiteralPath $expected)
if ($exeMatch) {
  $launch = $expected
} else {
  $leaf = [IO.Path]::GetFileName($record.command).ToLowerInvariant()
  $moduleArgs = @()
  if ($record.PSObject.Properties.Name -contains 'args' -and $record.args) { $moduleArgs = @($record.args) }
  $joined = (@($moduleArgs) | Select-Object -First 2) -join ' '
  $pythonLeaf = $leaf -in @('python.exe', 'python', 'python3.exe', 'python3')
  if ($pythonLeaf -and $joined -eq '-m noteford.service' -and (Test-Path -LiteralPath $record.command)) {
    $launch = $record.command
    $prefix = @($moduleArgs)
  } else {
    throw 'Noteford 服务位置无效，请重新安装应用。'
  }
}
$mode = if ($Action -eq 'serve') { '--mcp-auto' } else { '--mcp-skill' }
$serviceArgs = @($mode, '--client-name', $ClientName, '--discovery', $manifest)
$env:PYTHONIOENCODING = 'utf-8'
if ($ImageDirectory) { $serviceArgs += @('--image-directory', $ImageDirectory) }
if ($Action -ne 'serve') { $serviceArgs += @('--action', $Action) }
if ($Action -eq 'call') {
  if (!$Tool -or !$ArgumentsFile) { throw 'call 需要 Tool 和 UTF-8 JSON ArgumentsFile。' }
  $payload = Get-Content -LiteralPath $ArgumentsFile -Raw -Encoding UTF8
  $null = $payload | ConvertFrom-Json
  $serviceArgs += @('--tool', $Tool)
  $OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
  $payload | & $launch @prefix @serviceArgs
} else {
  [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
  & $launch @prefix @serviceArgs
}
exit $LASTEXITCODE
