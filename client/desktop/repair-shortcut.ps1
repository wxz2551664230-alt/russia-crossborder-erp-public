param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'RussiaERPClient'),
    [string]$DesktopPath = [Environment]::GetFolderPath('Desktop'),
    [string]$ProgramsPath = [Environment]::GetFolderPath('Programs')
)
$ErrorActionPreference = 'Stop'
$appRoot = Join-Path $InstallRoot 'app'
if (-not (Test-Path -LiteralPath $appRoot)) { throw "ERP client is not installed for this Windows user: $appRoot" }
$versions = Get-ChildItem -LiteralPath $appRoot -Directory | Where-Object { $_.Name -match '^\d+\.\d+\.\d+$' } | Sort-Object { [version]$_.Name } -Descending
$selected = $null
foreach ($version in $versions) {
    $program = Join-Path $version.FullName 'erp_desktop.py'
    $pythonw = Join-Path $version.FullName '.venv\Scripts\pythonw.exe'
    if ((Test-Path -LiteralPath $program) -and (Test-Path -LiteralPath $pythonw)) {
        $selected = @{ Directory = $version.FullName; Program = $program; Pythonw = $pythonw }
        break
    }
}
if (-not $selected) { throw 'No complete ERP client installation was found. Run install.cmd first.' }
$shell = New-Object -ComObject WScript.Shell
$paths = @($DesktopPath, $ProgramsPath)
foreach ($folder in $paths) {
    if (-not $folder) { throw 'Cannot locate the current user desktop or Start Menu' }
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    $link = $shell.CreateShortcut((Join-Path $folder '俄罗斯跨境 ERP.lnk'))
    $link.TargetPath = $selected.Pythonw
    $link.Arguments = ('"' + $selected.Program + '" gui')
    $link.WorkingDirectory = $selected.Directory
    $link.Save()
    Write-Output "Launcher ready: $(Join-Path $folder '俄罗斯跨境 ERP.lnk')"
}
