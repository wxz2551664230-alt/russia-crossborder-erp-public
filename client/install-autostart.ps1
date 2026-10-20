$ErrorActionPreference = "Stop"
$clientDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startScript = Join-Path $clientDir "start-worker.ps1"
$taskName = "Russia ERP Local Worker"
$ozonStartScript = Join-Path $clientDir "start-ozon-worker.ps1"
$ozonTaskName = "Russia ERP Ozon Worker"
$queueStartScript = Join-Path $clientDir "start-queue-workers.ps1"
$queueTaskName = "Russia ERP Queue Workers"

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$startScript`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 20 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description "Secure local worker for Russia Cross-border ERP" -Force | Out-Null
$ozonAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ozonStartScript`""
Register-ScheduledTask -TaskName $ozonTaskName -Action $ozonAction -Trigger $trigger -Settings $settings -Description "Dedicated local Ozon queue worker for Russia Cross-border ERP" -Force | Out-Null
$queueAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$queueStartScript`""
Register-ScheduledTask -TaskName $queueTaskName -Action $queueAction -Trigger $trigger -Settings $settings -Description "Isolated AI, POD, order, and general queue workers for Russia Cross-border ERP" -Force | Out-Null
if ((Get-ScheduledTask -TaskName $taskName).State -ne "Running") { Start-ScheduledTask -TaskName $taskName }
if ((Get-ScheduledTask -TaskName $ozonTaskName).State -ne "Running") { Start-ScheduledTask -TaskName $ozonTaskName }
if ((Get-ScheduledTask -TaskName $queueTaskName).State -ne "Running") { Start-ScheduledTask -TaskName $queueTaskName }
Write-Output "Remote worker and all isolated ERP queue workers autostart installed and started."
