<#
.SYNOPSIS
  Expose Noteford remote MCP (127.0.0.1:8765) through Cloudflare Tunnel.
  This script never installs software. Steps that need the owner's Cloudflare
  login (Setup) must be run by the owner interactively.

.EXAMPLE
  # One-time: named tunnel on your own domain (stable URL, recommended)
  .\Scripts\remote-mcp-tunnel.ps1 -Action Setup -Hostname noteford.example.com
  # Every time Noteford remote MCP should be reachable:
  .\Scripts\remote-mcp-tunnel.ps1 -Action Run
  # Optional: register as a Windows service so it starts with the PC (admin shell)
  .\Scripts\remote-mcp-tunnel.ps1 -Action InstallService
  # Testing only: temporary https://*.trycloudflare.com URL (changes every run, no account)
  .\Scripts\remote-mcp-tunnel.ps1 -Action Quick
#>
param(
  [ValidateSet('Check', 'Setup', 'Run', 'Quick', 'InstallService')]
  [string]$Action = 'Check',
  [string]$Hostname,
  [string]$TunnelName = 'noteford-mcp',
  [int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
$cf = Get-Command cloudflared -ErrorAction SilentlyContinue
if (-not $cf) {
  Write-Host 'cloudflared 未安装。请自行安装：winget install --id Cloudflare.cloudflared' -ForegroundColor Yellow
  exit 1
}
$cfDir = Join-Path $env:USERPROFILE '.cloudflared'
$configFile = Join-Path $cfDir "$TunnelName.yml"

switch ($Action) {
  'Check' {
    & $cf.Source --version
    Write-Host "配置文件：$configFile 存在=$(Test-Path $configFile)"
    try { $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$Port/.well-known/oauth-authorization-server"; Write-Host "本机远程 MCP：运行中 (127.0.0.1:$Port)" }
    catch { Write-Host '本机远程 MCP：未响应（请在 Noteford 设置 → AI 连接 开启远程连接）' -ForegroundColor Yellow }
  }
  'Setup' {
    if (-not $Hostname) { throw '需要 -Hostname，例如 noteford.example.com（域名需托管在 Cloudflare）' }
    if (-not (Test-Path (Join-Path $cfDir 'cert.pem'))) { & $cf.Source tunnel login }   # opens browser; owner logs in
    $existing = & $cf.Source tunnel list --output json | ConvertFrom-Json | Where-Object name -eq $TunnelName
    if (-not $existing) { & $cf.Source tunnel create $TunnelName | Out-Host; $existing = & $cf.Source tunnel list --output json | ConvertFrom-Json | Where-Object name -eq $TunnelName }
    $id = $existing.id
    & $cf.Source tunnel route dns $TunnelName $Hostname | Out-Host
    @"
tunnel: $id
credentials-file: $(Join-Path $cfDir "$id.json")
ingress:
  - hostname: $Hostname
    service: http://127.0.0.1:$Port
    originRequest:
      httpHostHeader: $Hostname
  - service: http_status:404
"@ | Set-Content -Encoding UTF8 $configFile
    Write-Host "完成。Noteford 设置里的公网地址填：https://$Hostname" -ForegroundColor Green
    Write-Host '注意：只转发 8765（公网 MCP/OAuth），绝不要转发 8766（本机管理接口）。'
  }
  'Run' {
    if (-not (Test-Path $configFile)) { throw "缺少 $configFile，请先运行 -Action Setup" }
    & $cf.Source tunnel --config $configFile run $TunnelName
  }
  'InstallService' {
    if (-not (Test-Path $configFile)) { throw "缺少 $configFile，请先运行 -Action Setup" }
    Copy-Item $configFile (Join-Path $cfDir 'config.yml') -Force
    & $cf.Source service install
  }
  'Quick' {
    Write-Host '临时地址每次运行都会变，需同步改 Noteford 公网地址并让 AI 重新授权；仅供测试。' -ForegroundColor Yellow
    & $cf.Source tunnel --no-autoupdate --url "http://127.0.0.1:$Port" --http-host-header "127.0.0.1:$Port"
  }
}
