param([Parameter(Mandatory = $true)][string]$CsvPath)

$ErrorActionPreference = "Stop"
$clientDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$resolved = (Resolve-Path -LiteralPath $CsvPath).Path
$rows = @(Import-Csv -LiteralPath $resolved)
if ($rows.Count -ne 1 -or -not $rows[0].AccessKeyId -or -not $rows[0].AccessKeySecret) {
    throw "AccessKey CSV format is invalid"
}
$payload = @{
    access_key_id = [string]$rows[0].AccessKeyId
    access_key_secret = [string]$rows[0].AccessKeySecret
} | ConvertTo-Json -Compress
$encrypted = ConvertFrom-SecureString (ConvertTo-SecureString $payload -AsPlainText -Force)
Set-Content -LiteralPath (Join-Path $clientDir "worker.secrets") -Value $encrypted -Encoding ascii
Write-Output "AccessKey encrypted for the current Windows user."
