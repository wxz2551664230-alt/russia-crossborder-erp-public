$ErrorActionPreference = "Stop"

$clientDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $clientDir
$configPath = Join-Path $clientDir "worker.env"
$secretsPath = Join-Path $clientDir "worker.secrets"
$pythonPath = Join-Path $projectDir ".venv\Scripts\python.exe"
$pidPath = Join-Path $clientDir "worker.pid"

if (-not (Test-Path -LiteralPath $configPath)) { throw "Missing client\worker.env" }
if (-not (Test-Path -LiteralPath $pythonPath)) { throw "Missing local Python environment" }

$config = @{}
Get-Content -LiteralPath $configPath | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)=(.*)$') { $config[$matches[1].Trim()] = $matches[2].Trim() }
}
if (Test-Path -LiteralPath $secretsPath) {
    $secure = ConvertTo-SecureString ((Get-Content -Raw -LiteralPath $secretsPath).Trim())
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $secretValues = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) | ConvertFrom-Json
        $config["ERP_WORKER_OSS_ACCESS_KEY_ID"] = [string]$secretValues.access_key_id
        $config["ERP_WORKER_OSS_ACCESS_KEY_SECRET"] = [string]$secretValues.access_key_secret
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}
foreach ($name in @("ERP_CONTROL_PLANE_URL", "ERP_WORKER_ID", "ERP_WORKER_TOKEN", "ERP_TUNNEL_HOST", "ERP_TUNNEL_USER", "ERP_TUNNEL_KEY")) {
    if (-not $config[$name] -or $config[$name] -like '<*') { throw "worker.env is missing $name" }
}

$localPort = if ($config["ERP_TUNNEL_LOCAL_PORT"]) { $config["ERP_TUNNEL_LOCAL_PORT"] } else { "18789" }
$remotePort = if ($config["ERP_TUNNEL_REMOTE_PORT"]) { $config["ERP_TUNNEL_REMOTE_PORT"] } else { "8789" }
$sshArgs = @(
    "-i", $config["ERP_TUNNEL_KEY"], "-N", "-T",
    "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
    "-L", "${localPort}:127.0.0.1:${remotePort}",
    ($config["ERP_TUNNEL_USER"] + "@" + $config["ERP_TUNNEL_HOST"])
)

$env:ERP_RUNTIME_MODE = "worker"
$env:ERP_CONTROL_PLANE_URL = $config["ERP_CONTROL_PLANE_URL"]
$env:ERP_WORKER_ID = $config["ERP_WORKER_ID"]
$env:ERP_WORKER_TOKEN = $config["ERP_WORKER_TOKEN"]
$env:ERP_WORKER_ALLOW_SYNTHETIC_DNS = if ($config["ERP_WORKER_ALLOW_SYNTHETIC_DNS"]) { $config["ERP_WORKER_ALLOW_SYNTHETIC_DNS"] } else { "false" }
$env:ERP_DATA_DIR = Join-Path $clientDir "runtime"
foreach ($name in $config.Keys) {
    if ($name -like "ERP_WORKER_OSS_*") { Set-Item -Path "Env:$name" -Value $config[$name] }

}

$worker = Start-Process -FilePath $pythonPath -ArgumentList @("-m", "erp.remote_worker") -WorkingDirectory $projectDir -PassThru -NoNewWindow
Set-Content -LiteralPath $pidPath -Value $PID
$tunnel = $null
try {
    while (-not $worker.HasExited) {
        if ($null -eq $tunnel -or $tunnel.HasExited) {
            $tunnel = Start-Process -FilePath "ssh.exe" -ArgumentList $sshArgs -PassThru -WindowStyle Hidden
        }
        Start-Sleep -Seconds 5
    }
} finally {
    if ($null -ne $tunnel -and -not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -Force }
    if (-not $worker.HasExited) { Stop-Process -Id $worker.Id -Force }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}
