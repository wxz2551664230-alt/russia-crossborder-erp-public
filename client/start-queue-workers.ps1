$ErrorActionPreference = "Stop"

$clientDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $clientDir
$pythonPath = Join-Path $projectDir ".venv\Scripts\python.exe"
$pidPath = Join-Path $clientDir "queue-workers.pid"
$logDir = Join-Path $projectDir "data\logs"

if (-not (Test-Path -LiteralPath $pythonPath)) { throw "Missing local Python environment" }
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Set-Content -LiteralPath $pidPath -Value $PID

# Each product area owns an independent consumer. Five AI consumers allow one
# slow generation call without blocking unrelated AI review/copy tasks.
$definitions = @(
    @{ Name = "ai-1"; Queue = "ai" },
    @{ Name = "ai-2"; Queue = "ai" },
    @{ Name = "ai-3"; Queue = "ai" },
    @{ Name = "ai-4"; Queue = "ai" },
    @{ Name = "ai-5"; Queue = "ai" },
    @{ Name = "pod"; Queue = "pod" },
    @{ Name = "orders"; Queue = "orders" },
    @{ Name = "ads"; Queue = "ads" },
    @{ Name = "general"; Queue = "general" },
    @{ Name = "sourcing"; Queue = "sourcing" },
    @{ Name = "wb"; Queue = "wb" },
    @{ Name = "wb_operations"; Queue = "wb_operations" },
    @{ Name = "pod_sync"; Queue = "pod_sync" },
    @{ Name = "self_listing"; Queue = "self_listing" }
)
$processes = @{}

function Start-QueueWorker($definition) {
    $name = $definition.Name
    $queue = $definition.Queue
    return Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @("-m", "erp.worker", "--queue", $queue) `
        -WorkingDirectory $projectDir `
        -RedirectStandardOutput (Join-Path $logDir "worker-$name.log") `
        -RedirectStandardError (Join-Path $logDir "worker-$name-error.log") `
        -PassThru `
        -WindowStyle Hidden
}

try {
    while ($true) {
        foreach ($definition in $definitions) {
            $name = $definition.Name
            $process = $processes[$name]
            if ($null -eq $process -or $process.HasExited) {
                $processes[$name] = Start-QueueWorker $definition
            }
        }
        Start-Sleep -Seconds 3
    }
} finally {
    foreach ($process in $processes.Values) {
        if ($null -ne $process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}
