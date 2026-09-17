param([string]$Distribution = 'Ubuntu-24.04')
$ErrorActionPreference = 'Stop'
$projectRootPath = [System.IO.Path]::GetPathRoot($PSScriptRoot)
$projectDrive = $projectRootPath.Substring(0, 1)
$projectDrive = $projectDrive.ToLowerInvariant()
$projectRelative = $PSScriptRoot.Substring(3)
$projectRelative = $projectRelative.Replace('\', '/')
$wslRoot = "/mnt/$projectDrive/$projectRelative"
$logRoot = Join-Path $PSScriptRoot 'yolo_lab_data\startup_logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

# The action bridge keeps ROS2 clients and cached state in-process.  Reusing an
# old bridge after the platform is restarted can therefore leave port 8766
# reachable but unusable.  Always replace every previous bridge instance with
# one fresh process.  This does not publish commands or move the robot.
Write-Host 'Stopping previous action bridge instances...'
& wsl.exe -d $Distribution -- pkill -TERM -f '[t]ask1_robot_bridge.py'
if ($LASTEXITCODE -gt 1) { throw 'Cannot stop previous WSL action bridge processes.' }

$bridgeStopped = $false
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    $remainingBridge = & wsl.exe -d $Distribution -- pgrep -f '[t]ask1_robot_bridge.py'
    if ($LASTEXITCODE -gt 1) { throw 'Cannot inspect WSL action bridge processes.' }
    if (-not $remainingBridge) {
        $bridgeStopped = $true
        break
    }
    Start-Sleep -Milliseconds 250
}
if (-not $bridgeStopped) {
    Write-Host 'Previous action bridge did not stop gracefully; forcing it to exit.'
    & wsl.exe -d $Distribution -- pkill -KILL -f '[t]ask1_robot_bridge.py'
    if ($LASTEXITCODE -gt 1) { throw 'Cannot force-stop previous WSL action bridge processes.' }
    Start-Sleep -Milliseconds 500
}

$running = & wsl.exe -d $Distribution -- pgrep -f '[l]bot_driver'
if ($LASTEXITCODE -gt 1) { throw 'Cannot inspect WSL processes.' }
if (-not $running) {
    Start-Process wsl.exe -ArgumentList @('-d', $Distribution, '--', 'bash', "`"$wslRoot/task1/start_component_wsl.sh`"", 'lbot_driver') -WindowStyle Hidden -RedirectStandardOutput "$logRoot/robot_only_driver.out.log" -RedirectStandardError "$logRoot/robot_only_driver.err.log" | Out-Null
}

$bridgeProcess = Start-Process wsl.exe -ArgumentList @('-d', $Distribution, '--', 'bash', "`"$wslRoot/task1/start_component_wsl.sh`"", 'action_bridge') -WindowStyle Hidden -RedirectStandardOutput "$logRoot/robot_only_bridge.out.log" -RedirectStandardError "$logRoot/robot_only_bridge.err.log" -PassThru

$bridgeReady = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    Start-Sleep -Milliseconds 250
    if ($bridgeProcess.HasExited) { break }
    try {
        # /health may correctly return 503 until the physical robot services
        # are available. /state proves that the fresh HTTP bridge is listening.
        $null = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/state' -TimeoutSec 1
        $bridgeReady = $true
        break
    } catch {
        # ROS2 discovery and the HTTP listener may need a few seconds.
    }
}
if (-not $bridgeReady) {
    throw "New action bridge did not become reachable on port 8766. Check $logRoot/robot_only_bridge.err.log"
}

Write-Host "Fresh action bridge is ready on port 8766. No motion sent. Logs: $logRoot"
Write-Host 'Next run probe_task1_robot.py to check hardware readiness.'
