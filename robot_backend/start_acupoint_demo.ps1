param(
    [string]$WslDistro = 'Ubuntu-24.04',
    [int]$Port = 8008,
    [switch]$EnableRobot = $true,
    [switch]$StartHardwareStack,
    [switch]$StartCameraStack = $true,
    [switch]$StartRobotStack,
    [string]$RobotNamespace = 'robot1',
    [string]$RobotBridgeUrl = 'http://127.0.0.1:8766',
    [double]$SpeedScale = 1.0
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$dataDir = Join-Path $projectRoot 'data'
$stdoutLog = Join-Path $dataDir '_acupoint_demo_live.log'
$stderrLog = Join-Path $dataDir '_acupoint_demo_live.err.log'
$pidFile = Join-Path $dataDir '_acupoint_demo_live.pid'
$projectRootPath = [System.IO.Path]::GetPathRoot($projectRoot)
$projectDrive = $projectRootPath.Substring(0, 1)
$projectDrive = $projectDrive.ToLowerInvariant()
$projectRelative = $projectRoot.Substring(3)
$projectRelative = $projectRelative.Replace('\', '/')
$wslProjectRoot = "/mnt/$projectDrive/$projectRelative"
$wslScript = "$wslProjectRoot/run_acupoint_demo_wsl.sh"
$statusUrl = "http://127.0.0.1:$Port/status.json"

New-Item -ItemType Directory -Path $dataDir -Force | Out-Null

if ($StartHardwareStack) {
    $StartCameraStack = $true
    $StartRobotStack = $true
}

function Start-AcupointCameraStack {
    $runningCamera = & wsl.exe -d $WslDistro -- pgrep -f '[o]rbbec_camera_node'
    if ($LASTEXITCODE -gt 1) { throw 'Cannot inspect the Gemini 2 camera process.' }
    if ($runningCamera) {
        $wslUsb = & wsl.exe -d $WslDistro -- lsusb
        if ($wslUsb -match '2bc5:0670') {
            Write-Host "Gemini 2 camera is already running (PID: $($runningCamera -join ', '))."
            return
        }
        Write-Host 'Removing stale Gemini 2 camera processes because the USB device is no longer attached.'
        & wsl.exe -d $WslDistro -- pkill -TERM -f '[o]rbbec_camera_node'
        Start-Sleep -Milliseconds 500
    }

    $usbipdPath = 'C:\Program Files\usbipd-win\usbipd.exe'
    if (-not (Test-Path -LiteralPath $usbipdPath)) {
        throw 'usbipd-win is not installed; Gemini 2 cannot be attached.'
    }

    $usbMatch = & $usbipdPath list | Select-String -Pattern '2bc5:0670' | Select-Object -First 1
    $usbLine = $usbMatch.Line
    if (-not $usbLine) {
        throw 'Orbbec Gemini 2 (VID:PID 2bc5:0670) was not found.'
    }
    $usbLineTrimmed = $usbLine.Trim()
    $busIdParts = $usbLineTrimmed -split '\s+'
    $busId = $busIdParts[0]
    if ($usbLine -match '(?i)Not shared') {
        throw "Gemini 2 is not shared. Run once in Administrator PowerShell: usbipd bind --busid $busId"
    }
    if ($usbLine -notmatch '(?i)\bAttached\b') {
        & $usbipdPath attach --wsl --busid $busId
        if ($LASTEXITCODE -ne 0) { throw 'Gemini 2 USB attach failed.' }
        Start-Sleep -Seconds 1
    }

    $runningCamera = & wsl.exe -d $WslDistro -- pgrep -f '[o]rbbec_camera_node'
    if ($LASTEXITCODE -gt 1) { throw 'Cannot inspect the Gemini 2 camera process.' }
    if (-not $runningCamera) {
        $cameraOutLog = Join-Path $dataDir '_acupoint_camera.out.log'
        $cameraErrLog = Join-Path $dataDir '_acupoint_camera.err.log'
        Start-Process -FilePath 'wsl.exe' -ArgumentList @(
            '-d', $WslDistro, '--', 'bash', "`"$wslProjectRoot/task1/start_component_wsl.sh`"", 'camera'
        ) -WindowStyle Hidden -RedirectStandardOutput $cameraOutLog -RedirectStandardError $cameraErrLog | Out-Null
        Write-Host "Gemini 2 camera launch requested. Logs: $cameraErrLog"
    } else {
        Write-Host "Gemini 2 camera is already running (PID: $($runningCamera -join ', '))."
    }
}

function Wait-AcupointFrame([int]$Seconds = 45) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try {
            $cameraStatus = Invoke-RestMethod -Uri $statusUrl -TimeoutSec 2
            if ($cameraStatus.frame_ready -and $null -ne $cameraStatus.frame_age_s -and $cameraStatus.frame_age_s -lt 2) {
                Write-Host "Gemini 2 live frame ready: $($cameraStatus.fps) fps."
                return $true
            }
        } catch {
            # The local service may still be starting.
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $false
}

if ($StartCameraStack) {
    Start-AcupointCameraStack
}

# Starting the platform always replaces the action bridge.  A stale bridge can
# keep port 8766 occupied while no longer being connected to the ROS2 graph.
# Starting the driver and bridge is passive and preserves the controller's
# current enable state; it never sends enable or disable by itself.
& (Join-Path $projectRoot 'start_task1_robot_only.ps1') -Distribution $WslDistro

try {
    $existing = Invoke-RestMethod -Uri $statusUrl -TimeoutSec 2
    if ($null -eq $existing.action -or $null -eq $existing.flows) {
        Write-Host "Incompatible old acupoint service detected on port $Port; replacing it."
        & wsl.exe -d $WslDistro -- pkill -TERM -f "[a]cupoint_demo.py.*--port $Port"
        Start-Sleep -Seconds 1
    } else {
        if ($EnableRobot -and -not $existing.action.enabled) {
            Write-Host "检测到旧的安全预览服务；正在替换为可控制机器人使能的服务。"
            & wsl.exe -d $WslDistro -- pkill -TERM -f "[a]cupoint_demo.py.*--port $Port"
            Start-Sleep -Seconds 1
            throw 'Restart the acupoint service with robot controls enabled.'
        }
        if ($StartCameraStack -and -not (Wait-AcupointFrame)) {
            throw "Gemini 2 started but no live frame reached $statusUrl within 45 seconds."
        }
        Write-Host "穴位演示已经运行：$statusUrl"
        exit 0
    }
} catch {
    # No healthy service yet; start one below.
}

$arguments = @(
    '-d', $WslDistro,
    '--', 'bash', $wslScript,
    '--host', '127.0.0.1',
    '--port', $Port
    '--robot-bridge-url', $RobotBridgeUrl
)
if ($EnableRobot) {
    $arguments += @('--enable-robot', '--robot-namespace', $RobotNamespace, '--speed-scale', $SpeedScale)
}
$process = Start-Process -FilePath 'wsl.exe' -ArgumentList $arguments -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru
Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii

$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 500
    if ($process.HasExited) { break }
    try {
        $null = Invoke-RestMethod -Uri $statusUrl -TimeoutSec 1
        $ready = $true
        break
    } catch {
        # Keep waiting for ROS2 discovery and the local web server.
    }
}

if (-not $ready) {
    Write-Host "穴位演示未能启动，请查看：$stderrLog"
    exit 1
}

if ($StartCameraStack -and -not (Wait-AcupointFrame)) {
    Write-Host "Gemini 2 已启动，但 45 秒内没有收到实时画面。请查看：$dataDir\_acupoint_camera.err.log"
    exit 1
}

Write-Host "穴位演示已启动：http://127.0.0.1:$Port"
if ($EnableRobot) {
    Write-Host "真机动作：已启用（命名空间 $RobotNamespace，速度缩放 $SpeedScale）"
} else {
    Write-Host "真机动作控制未开放"
}
Write-Host "日志：$stdoutLog"
