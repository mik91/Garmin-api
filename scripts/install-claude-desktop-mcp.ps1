# Adds this project's MCP server to Claude Desktop's config.
# Claude Desktop rewrites its config while running, so this waits until it is fully quit
# (tray icon > Quit), then merges the "garmin" entry without touching other settings.
# Run from Windows Terminal or PowerShell:  .\scripts\install-claude-desktop-mcp.ps1

param([string]$ConfigPath)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

# The Microsoft Store build keeps its config here; the regular installer uses %APPDATA%\Claude.
$candidates = @(
    (Get-ChildItem "$env:LOCALAPPDATA\Claude-Data\*\claude_desktop_config.json" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName),
    "$env:APPDATA\Claude\claude_desktop_config.json"
) | Where-Object { $_ }
$target = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $target) { $target = $candidates[0] }
if ($ConfigPath) { $target = $ConfigPath }

$python = (Get-Command python -ErrorAction Stop).Source
if (Test-Path "$projectRoot\.venv\Scripts\python.exe") { $python = "$projectRoot\.venv\Scripts\python.exe" }

function Get-ClaudeDesktop {
    Get-Process -Name claude -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*\WindowsApps\Claude_*" -or $_.Path -like "*\AnthropicClaude\*" }
}

if (-not $ConfigPath -and (Get-ClaudeDesktop)) {
    Write-Host "Quit Claude Desktop now (right-click the tray icon > Quit). Waiting..."
    while (Get-ClaudeDesktop) { Start-Sleep -Seconds 1 }
    Start-Sleep -Seconds 2
}

$config = if (Test-Path $target) { Get-Content $target -Raw | ConvertFrom-Json } else { New-Object PSObject }
if (Test-Path $target) { Copy-Item $target "$target.bak" -Force }
if (-not $config.PSObject.Properties["mcpServers"]) {
    $config | Add-Member -NotePropertyName mcpServers -NotePropertyValue (New-Object PSObject)
}
$server = [PSCustomObject]@{
    command = $python
    args    = @("-m", "garmin_ai.mcp_server")
    env     = [PSCustomObject]@{ PYTHONPATH = $projectRoot }
}
$config.mcpServers | Add-Member -NotePropertyName garmin -NotePropertyValue $server -Force

# Write UTF-8 without a BOM so the app can parse it.
New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
[IO.File]::WriteAllText($target, ($config | ConvertTo-Json -Depth 100), (New-Object Text.UTF8Encoding $false))

Write-Host "Added the garmin MCP server to $target"
Write-Host "  python: $python"
Write-Host "Start Claude Desktop again; the garmin tools appear in a new chat."
