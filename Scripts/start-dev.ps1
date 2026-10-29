$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$desktopRoot = Join-Path $projectRoot 'desktop'
$configPath = Join-Path $projectRoot 'config.local.json'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot 'config.example.json') -Destination $configPath
}
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
if ($config.project_root -and
    [IO.Path]::GetFullPath([string]$config.project_root) -ne [IO.Path]::GetFullPath($projectRoot)) {
    throw 'Development config project_root points outside the source directory. Refusing to start.'
}
if ($config.library_registry_path) {
    $registryPath = [IO.Path]::GetFullPath([string]$config.library_registry_path)
    $projectPrefix = [IO.Path]::GetFullPath($projectRoot).TrimEnd('\') + '\'
    if (-not $registryPath.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Development config library_registry_path points outside the source directory. Refusing to start.'
    }
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Python development environment is missing: $pythonPath. See docs/DEVELOPMENT.md."
}
if (-not (Test-Path -LiteralPath (Join-Path $desktopRoot 'node_modules\.bin\electron.cmd') -PathType Leaf)) {
    throw "Desktop dependencies are missing. Run node scripts/install-dependencies.cjs in $desktopRoot."
}

$listener = Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalAddress -eq '127.0.0.1' } |
    Select-Object -First 1
if ($listener) {
    $viteProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
    $electronPath = Join-Path $desktopRoot 'node_modules\electron\dist\electron.exe'
    $devWindow = Get-CimInstance Win32_Process -Filter "Name = 'electron.exe'" |
        Where-Object {
            $_.ExecutablePath -eq $electronPath -and
            $_.CommandLine -match '(^|\s)--dev(\s|$)' -and
            $_.CommandLine -notmatch '\s--type='
        } |
        ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue } |
        Where-Object { $_.MainWindowHandle -ne 0 } |
        Select-Object -First 1

    if ($viteProcess.CommandLine -like "*$desktopRoot*" -and
        $viteProcess.CommandLine -match 'vite[\\/]bin[\\/]vite\.js' -and
        $devWindow) {
        Add-Type -Namespace Win32 -Name DevWindow -MemberDefinition '[DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow); [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);'
        [void][Win32.DevWindow]::ShowWindowAsync($devWindow.MainWindowHandle, 9)
        [void][Win32.DevWindow]::SetForegroundWindow($devWindow.MainWindowHandle)
        Write-Host 'Noteford Dev is already running; activated its window.'
        return
    }

    throw "Port 5173 is in use and no running Noteford Dev window was found. Process ID: $($listener.OwningProcess)."
}

$env:NOTEFORD_PYTHON = $pythonPath
$ollamaProcess = $null
$ollamaExe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
$ollamaManifest = Join-Path $projectRoot 'data\models\ollama\manifests\registry.ollama.ai\library\qwen3\4b'
if ((Test-Path -LiteralPath $ollamaManifest -PathType Leaf) -and
    (Test-Path -LiteralPath $ollamaExe -PathType Leaf) -and
    -not (Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue)) {
    $env:OLLAMA_MODELS = Join-Path $projectRoot 'data\models\ollama'
    $env:OLLAMA_CONTEXT_LENGTH = '8192'
    $nvidiaGpu = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match 'NVIDIA' } | Select-Object -First 1
    if ($nvidiaGpu -and (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\lib\ollama\cuda_v13\ggml-cuda.dll'))) {
        $env:OLLAMA_VULKAN = 'false'
        $env:OLLAMA_LLM_LIBRARY = 'cuda_v13'
    }
    $ollamaProcess = Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden -PassThru
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $null = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 1
            $ready = $true
            break
        }
        catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $ready) {
        Stop-Process -Id $ollamaProcess.Id -ErrorAction SilentlyContinue
        throw 'Local Ollama did not start. Check the installed Ollama runtime.'
    }
}
Push-Location $desktopRoot
try {
    & npm.cmd run dev
    if ($LASTEXITCODE -ne 0) { throw "Development app exited with code $LASTEXITCODE." }
}
finally {
    Pop-Location
    if ($ollamaProcess -and -not $ollamaProcess.HasExited) {
        Stop-Process -Id $ollamaProcess.Id -ErrorAction SilentlyContinue
    }
}
