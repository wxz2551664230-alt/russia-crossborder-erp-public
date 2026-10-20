$taskName = "Russia ERP Local Worker"
$ozonTaskName = "Russia ERP Ozon Worker"
$queueTaskName = "Russia ERP Queue Workers"
$clientDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $clientDir "stop-worker.ps1")
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
if (Get-ScheduledTask -TaskName $ozonTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $ozonTaskName -Confirm:$false
}
if (Get-ScheduledTask -TaskName $queueTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $queueTaskName -Confirm:$false
}
Write-Output "Remote worker and all isolated ERP queue workers autostart removed."
