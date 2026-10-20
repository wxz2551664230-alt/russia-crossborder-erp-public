$clientDir = Split-Path -Parent $MyInvocation.MyCommand.Path
foreach ($taskName in @("Russia ERP Local Worker", "Russia ERP Ozon Worker", "Russia ERP Queue Workers")) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    }
}
$pidPaths = @(
    (Join-Path $clientDir "worker.pid"),
    (Join-Path $clientDir "ozon-worker.pid"),
    (Join-Path $clientDir "queue-workers.pid")
)
$stopped = $false
foreach ($pidPath in $pidPaths) {
    if (-not (Test-Path -LiteralPath $pidPath)) { continue }
    $supervisorPid = [int](Get-Content -LiteralPath $pidPath)
    & taskkill.exe /PID $supervisorPid /T /F 2>$null | Out-Null
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    $stopped = $true
}
if ($stopped) { Write-Output "Workers stopped." } else { Write-Output "Workers are not running." }
