param(
    [string]$WslDistro = 'Ubuntu-24.04',
    [int]$Port = 8008,
    [double]$SpeedScale = 1.0
)

$ErrorActionPreference = 'Stop'
$backendScript = Join-Path $PSScriptRoot 'robot_backend\start_acupoint_demo.ps1'
if (-not (Test-Path -LiteralPath $backendScript)) {
    throw "Robot backend launcher not found: $backendScript"
}

& $backendScript `
    -WslDistro $WslDistro `
    -Port $Port `
    -EnableRobot `
    -StartCameraStack `
    -SpeedScale $SpeedScale

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Qihuang Qiaoshou is ready: http://127.0.0.1:$Port/"
Write-Host 'Robot enable state is preserved; only the per-arm controls change it.'
