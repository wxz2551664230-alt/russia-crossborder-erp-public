$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { py -m venv (Join-Path $ProjectRoot ".venv") }
& $Python -m pip install --upgrade pip
& $Python -m pip install -e "$ProjectRoot[dev]"
Push-Location (Join-Path $ProjectRoot "frontend")
try { npm install; npm run build } finally { Pop-Location }
Push-Location $ProjectRoot
try { & $Python -m erp.migrate } finally { Pop-Location }
Write-Host "安装完成。"
