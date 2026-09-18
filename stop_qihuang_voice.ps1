$ErrorActionPreference = 'Stop'
$pidPath = Join-Path $PSScriptRoot 'data\voice-process.json'
if (-not (Test-Path -LiteralPath $pidPath)) { Write-Host 'No voice process record.'; exit 0 }
$record = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $($record.pid)"
if ($process) {
    if ($process.ExecutablePath -ne $record.executable -or -not $process.CommandLine.Contains($record.server)) {
        throw 'Process identity changed; refusing to stop an unrelated process.'
    }
    Stop-Process -Id $record.pid
}
Remove-Item -LiteralPath $pidPath
Write-Host 'Voice service stopped.'
