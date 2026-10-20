$ErrorActionPreference = "Stop"

$clientDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $clientDir
$pythonPath = Join-Path $projectDir ".venv\Scripts\python.exe"
$pidPath = Join-Path $clientDir "ozon-worker.pid"
$logDir = Join-Path $projectDir "data\logs"
$logPath = Join-Path $logDir "ozon-worker.log"
$errorLogPath = Join-Path $logDir "ozon-worker-error.log"

if (-not (Test-Path -LiteralPath $pythonPath)) { throw "Missing local Python environment" }
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Set-Content -LiteralPath $pidPath -Value $PID

try {
    while ($true) {
        $worker = Start-Process `
            -FilePath $pythonPath `
            -ArgumentList @("-m", "erp.worker", "--queue", "ozon") `
            -WorkingDirectory $projectDir `
            -RedirectStandardOutput $logPath `
            -RedirectStandardError $errorLogPath `
            -PassThru `
            -WindowStyle Hidden
        $worker.WaitForExit()
        Start-Sleep -Seconds 3
    }
} finally {
    if ($null -ne $worker -and -not $worker.HasExited) { Stop-Process -Id $worker.Id -Force }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}
