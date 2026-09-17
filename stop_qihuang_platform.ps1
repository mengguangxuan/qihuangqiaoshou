param(
    [string]$WslDistro = 'Ubuntu-24.04',
    [int]$Port = 8008
)

$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'robot_backend\stop_acupoint_demo.ps1') `
    -WslDistro $WslDistro `
    -Port $Port
