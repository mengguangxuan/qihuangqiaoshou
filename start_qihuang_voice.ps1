param([int]$Port = 8010)
$ErrorActionPreference = 'Stop'
$python = (Get-Command python -ErrorAction Stop).Source
$serverFile = Join-Path $PSScriptRoot 'voice_backend\server.py'
$envFile = Join-Path $PSScriptRoot '.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '.env.example') -Destination $envFile
}
$url = "http://127.0.0.1:$Port"
$existing = $null
try { $existing = Invoke-RestMethod -Uri "$url/api/voice/status" -TimeoutSec 2 } catch {}
if ($existing.service -eq 'qihuang-voice') {
    Write-Host "Voice service is already running: $url/"
    exit 0
}
$versionOk = & $python -c 'import sys; print(int(sys.version_info >= (3, 11)))'
if ($versionOk -ne '1') { throw 'Python 3.11 or newer is required.' }
$dataDir = Join-Path $PSScriptRoot 'data'
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
$serverArgs = @('-u', ('"' + $serverFile + '"'), '--port', "$Port")
$process = Start-Process -FilePath $python -ArgumentList $serverArgs -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $dataDir 'voice.stdout.log') `
    -RedirectStandardError (Join-Path $dataDir 'voice.stderr.log')
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    if ($process.HasExited) { throw "Voice service failed to start. Check data/voice.stderr.log (port $Port may be occupied)." }
    try {
        $status = Invoke-RestMethod -Uri "$url/api/voice/status" -TimeoutSec 1
        if ($status.service -eq 'qihuang-voice') {
            @{ pid=$process.Id; port=$Port; executable=$python; server=$serverFile } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $dataDir 'voice-process.json')
            Write-Host "Qihuang voice is ready: $url/"
            Write-Host "Fill OPENAI_API_KEY in $envFile, then click Check configuration on the page."
            exit 0
        }
    } catch {}
    Start-Sleep -Milliseconds 200
}
throw 'Voice service did not become ready. Check data/voice.stderr.log.'
