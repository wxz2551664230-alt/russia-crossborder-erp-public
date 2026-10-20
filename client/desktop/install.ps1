param(
    [string]$PythonPath = "",
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "RussiaERPClient"),
    [switch]$NoShortcut,
    [switch]$NoDependencies
)
$ErrorActionPreference = "Stop"
function Get-PackageHash([string]$FilePath) {
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($FilePath)
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $algorithm.Dispose() }
}
if (-not $PythonPath) { $PythonPath = (Get-Command python.exe -ErrorAction Stop).Source }
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
& $PythonPath -c "import sys, tkinter; assert sys.version_info >= (3,12), 'Python 3.12+ required'"
if ($LASTEXITCODE -ne 0) { throw "Install Python 3.12+ with Tk support first." }
$source = $PSScriptRoot
$manifest = Get-Content -Raw -LiteralPath (Join-Path $source "manifest.json") | ConvertFrom-Json
if ($manifest.version -notmatch '^\d+\.\d+\.\d+$') { throw "Invalid package version" }
$target = Join-Path $InstallRoot ("app\" + $manifest.version)
New-Item -ItemType Directory -Force -Path $target | Out-Null
foreach ($property in $manifest.files.PSObject.Properties) {
    $name = $property.Name
    if ($name -in @('install.ps1','install.cmd','repair-shortcut.ps1','repair-shortcut.cmd')) { continue }
    if ($name -notin @('erp_desktop.py','desktop_ui.py','local_worker.py','local_picker.py','composition_worker.py','artwork_worker.py','ai_worker.py','wb_worker.py','order_worker.py','purchase_submit_worker.py','purchase_journal.py','wb_reconcile_worker.py','wb_history_worker.py','wb_shop_sync_worker.py','wb_stock_worker.py','wb_price_worker.py','ad_worker.py','ad_sync_worker.py','ad_report_worker.py','ad_preview_worker.py','ad_execution_worker.py','ad_reconcile_worker.py','supplier_gallery_worker.py','supplier_catalog_worker.py','us_catalog_worker.py','amazon_worker.py','catalog_import_worker.py','pod_import_worker.py','self_listing_pipeline_worker.py','store_test_worker.py','catalog_ozon_batch_worker.py','catalog_ozon_worker.py','catalog_ozon_submit_worker.py','catalog_ozon_followup_worker.py','catalog_ozon_options_worker.py','ozon_image_rules.py', 'codex_bridge.py', 'wb_codex_bridge.py', 'amazon_codex_bridge.py', 'CODEX-HANDOFF.md', 'ozon_paid_transport.py','worker-requirements.txt','README.md','tools/realesrgan-ncnn-vulkan/realesrgan-ncnn-vulkan.exe','tools/realesrgan-ncnn-vulkan/vcomp140.dll','tools/realesrgan-ncnn-vulkan/models/realesrgan-x4plus.bin','tools/realesrgan-ncnn-vulkan/models/realesrgan-x4plus.param','tools/realesrgan-ncnn-vulkan/ERP-MIGRATION.md') -and $name -notmatch '^runtime/erp/[A-Za-z0-9_/.-]+\.py$') { throw "Unexpected package file" }
    if ($name.Contains('..')) { throw "Invalid package path" }
    $inputFile = Join-Path $source $name
    $expected = $manifest.files.$name
    if (-not $expected -or (Get-PackageHash $inputFile) -ne $expected) { throw "Package hash mismatch: $name" }
    $outputFile = Join-Path $target $name
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $outputFile) | Out-Null
    if (Test-Path -LiteralPath $outputFile) {
        if ((Get-PackageHash $outputFile) -ne $expected) { throw "Existing version differs. Keep it intact and use a new version." }
    } else { Copy-Item -LiteralPath $inputFile -Destination $outputFile }
}
if (-not $NoDependencies) {
    $venvPath = Join-Path $target '.venv'
    if (-not (Test-Path -LiteralPath (Join-Path $venvPath 'Scripts\python.exe'))) {
        & $PythonPath -m venv $venvPath
        if ($LASTEXITCODE -ne 0) { throw 'Cannot create client Python environment' }
    }
    $PythonPath = Join-Path $venvPath 'Scripts\python.exe'
    & $PythonPath -m pip install --disable-pip-version-check --no-cache-dir -r (Join-Path $target 'worker-requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Composition dependency installation failed; check network and retry installation' }
}
$program = Join-Path $target "erp_desktop.py"
$pythonw = Join-Path (Split-Path -Parent $PythonPath) "pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonw)) { throw "Missing pythonw.exe" }
if (-not $NoShortcut) {
    $shell = New-Object -ComObject WScript.Shell
    $desktopPath = [Environment]::GetFolderPath('Desktop')
    if (-not $desktopPath) { throw 'Cannot locate the current user desktop' }
    New-Item -ItemType Directory -Force -Path $desktopPath | Out-Null
    $shortcut = $shell.CreateShortcut((Join-Path $desktopPath '俄罗斯跨境 ERP.lnk'))
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = ('"' + $program + '" gui')
    $shortcut.WorkingDirectory = $target
    $shortcut.Save()
    $startMenuPath = Join-Path ([Environment]::GetFolderPath('Programs')) '俄罗斯跨境 ERP.lnk'
    $startShortcut = $shell.CreateShortcut($startMenuPath)
    $startShortcut.TargetPath = $pythonw
    $startShortcut.Arguments = ('"' + $program + '" gui')
    $startShortcut.WorkingDirectory = $target
    $startShortcut.Save()
    Write-Output "Launcher shortcuts created: $desktopPath and Start Menu"
    # Preserve old ERP shortcuts for rollback; only retire our verified entry points.
    foreach ($oldName in @('Russia ERP Client.lnk', 'Russia ERP Local Worker.lnk')) {
        $oldPath = Join-Path $desktopPath $oldName
        if (Test-Path -LiteralPath $oldPath) {
            $oldLink = $shell.CreateShortcut($oldPath)
            if ($oldLink.Arguments -match 'erp_desktop\.py"? (gui|local-worker)$') {
                $backupDir = Join-Path $InstallRoot 'shortcut-backups'
                New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
                $backupPath = Join-Path $backupDir (([guid]::NewGuid().ToString()) + '-' + $oldName)
                Copy-Item -LiteralPath $oldPath -Destination $backupPath
                Remove-Item -LiteralPath $oldPath
            }
        }
    }
}
$pythonQuoted = ConvertTo-Json -Compress $PythonPath
$programQuoted = ConvertTo-Json -Compress $program
$snippet = "[mcp_servers.russia_erp_desktop]`ncommand = $pythonQuoted`nargs = [$programQuoted, `"mcp`"]`n"
[System.IO.File]::WriteAllText((Join-Path $InstallRoot "codex-mcp-snippet.toml"), $snippet, (New-Object System.Text.UTF8Encoding($false)))
Write-Output "Installed client $($manifest.version): $target"
Write-Output "Codex config snippet prepared. Existing Codex configuration was not changed."

if (-not $NoDependencies) {
    $profilePath = if ($env:RU_ERP_CLIENT_HOME) { $env:RU_ERP_CLIENT_HOME } else { Join-Path $env:LOCALAPPDATA 'RussiaERPClient' }
    New-Item -ItemType Directory -Force -Path $profilePath | Out-Null
    $runtimeInfo = @{ python=$PythonPath; script=(Join-Path $target 'codex_bridge.py'); guide=(Join-Path $target 'CODEX-HANDOFF.md'); version=$manifest.version } | ConvertTo-Json
    [System.IO.File]::WriteAllText((Join-Path $profilePath 'codex-runtime.json'), $runtimeInfo, (New-Object System.Text.UTF8Encoding($false)))
}
