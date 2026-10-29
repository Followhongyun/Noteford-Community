param([Parameter(Mandatory=$true)][string]$DataRoot, [switch]$Background)
$ErrorActionPreference='Stop'
$projectRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$resolvedData=(Resolve-Path -LiteralPath $DataRoot).Path
if(-not(Test-Path -LiteralPath (Join-Path $resolvedData 'config.local.json'))){throw 'Existing data config is required.'}
$node=(Get-Command node.exe -ErrorAction Stop).Source
$runner=Join-Path $projectRoot 'desktop\scripts\local-live.cjs'
$env:NOTEFORD_DATA_ROOT=$resolvedData
$env:NOTEFORD_BACKGROUND=if($Background){'1'}else{'0'}
$env:NOTEFORD_PYTHON=Join-Path $projectRoot '.venv-build\Scripts\python.exe'
if(-not(Test-Path -LiteralPath $env:NOTEFORD_PYTHON)){throw 'Local Python runtime is missing.'}
Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
& $node $runner
if($LASTEXITCODE -ne 0){
 Add-Type -AssemblyName PresentationFramework
 [System.Windows.MessageBox]::Show('本机热更新启动失败。请查看数据目录 data\runtime\local-live\runner.log；也可使用“Noteford 安装版”回退。','Noteford Community') | Out-Null
 exit $LASTEXITCODE
}
