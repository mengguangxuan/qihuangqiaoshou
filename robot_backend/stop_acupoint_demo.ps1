param(
    [string]$WslDistro = 'Ubuntu-24.04',
    [int]$Port = 8008
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $projectRoot 'data\_acupoint_demo_live.pid'
$projectRootPath = [System.IO.Path]::GetPathRoot($projectRoot)
$projectDrive = $projectRootPath.Substring(0, 1)
$projectDrive = $projectDrive.ToLowerInvariant()
$projectRelative = $projectRoot.Substring(3)
$projectRelative = $projectRelative.Replace('\', '/')
$wslProjectRoot = "/mnt/$projectDrive/$projectRelative"
$pattern = "[p]ython3 $wslProjectRoot/acupoint_demo.py.*--port $Port"

$wslOutput = @(& wsl.exe -d $WslDistro -- pgrep -f $pattern 2>$null)
$wslExitCode = $LASTEXITCODE
$linuxPids = @($wslOutput | Where-Object { $_ -match '^\d+$' })
if ($wslExitCode -gt 1) { throw 'Cannot inspect the acupoint service in WSL.' }
foreach ($linuxPid in $linuxPids) {
    if ($linuxPid -match '^\d+$') {
        & wsl.exe -d $WslDistro -- kill -TERM $linuxPid
    }
}

if (Test-Path -LiteralPath $pidFile) {
    $processId = [int](Get-Content -LiteralPath $pidFile -Raw)
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -ne $process -and $process.ProcessName -eq 'wsl') {
        Stop-Process -Id $processId
    }
    Remove-Item -LiteralPath $pidFile -Force
}

if ($linuxPids.Count -gt 0) {
    Write-Host "已停止穴位演示服务：$($linuxPids -join ', ')。相机与机器人驱动保持运行。"
} else {
    Write-Host '没有发现正在运行的穴位演示服务。'
}
