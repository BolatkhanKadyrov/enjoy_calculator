param([switch]$Upgrade, [switch]$Stop)
$ErrorActionPreference = 'Stop'
$projectDirectory = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonPath = Join-Path $projectDirectory '.venv\Scripts\python.exe'
$localDirectory = Join-Path $projectDirectory '.local'
$listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $listenerProcessId = $listener[0].OwningProcess
    $currentProcessId = $listenerProcessId
    $belongsToProject = $false
    for ($depth = 0; $depth -lt 5 -and $currentProcessId; $depth++) {
        $candidate = Get-CimInstance Win32_Process -Filter "ProcessId = $currentProcessId"
        if (-not $candidate) { break }
        if ($candidate.CommandLine -like "*$pythonPath*" -and $candidate.CommandLine -match 'runserver.*127\.0\.0\.1:8000') {
            $belongsToProject = $true
            break
        }
        $currentProcessId = $candidate.ParentProcessId
    }
    if (-not $belongsToProject) { throw 'Port 8000 belongs to an unverified process. It was not stopped.' }
    Stop-Process -Id $listenerProcessId
    Write-Output 'Stopped the project application server.'
}
if ($Stop) { exit 0 }
Push-Location -LiteralPath $projectDirectory
try {
    & $pythonPath scripts/dev.py start-db
    if ($LASTEXITCODE -ne 0) { throw 'Local PostgreSQL did not start.' }
    if ($Upgrade) {
        & $pythonPath scripts/upgrade_local.py
        if ($LASTEXITCODE -ne 0) { throw 'Upgrade did not complete. Application was not restarted.' }
    }
    $server = Start-Process -FilePath $pythonPath -ArgumentList @('scripts/dev.py','manage','runserver','127.0.0.1:8000','--noreload') -WorkingDirectory $projectDirectory -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $localDirectory 'web.log') -RedirectStandardError (Join-Path $localDirectory 'web-error.log')
    $server.Id | Set-Content -LiteralPath (Join-Path $localDirectory 'app.pid')
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health/' -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $ready) { throw 'Application did not become ready. See .local/web-error.log.' }
    Write-Output 'Application ready: http://127.0.0.1:8000/'
} finally {
    Pop-Location
}
