$ErrorActionPreference = "Stop"

$clientDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$workers = @(
    @{ TaskName = "Russia ERP Local Worker"; Script = "start-worker.ps1" },
    @{ TaskName = "Russia ERP Ozon Worker"; Script = "start-ozon-worker.ps1" },
    @{ TaskName = "Russia ERP Queue Workers"; Script = "start-queue-workers.ps1" }
)

foreach ($item in $workers) {
    $task = Get-ScheduledTask -TaskName $item.TaskName -ErrorAction SilentlyContinue
    if ($task) {
        if ($task.State -ne "Running") { Start-ScheduledTask -TaskName $item.TaskName }
        continue
    }
    Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @("-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $clientDir $item.Script)) `
        -WindowStyle Hidden
}

Write-Output "Remote worker and all isolated ERP queue workers started."
