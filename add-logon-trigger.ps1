# Adds an "at logon" trigger to YaDiskWatchdog so downloads resume right after boot.
# Needs elevation: right-click -> Run as administrator (or run from an elevated PowerShell).
$ErrorActionPreference = 'Stop'
$name = 'YaDiskWatchdog'
$t = Get-ScheduledTask -TaskName $name
$has = @($t.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' }).Count -gt 0
if ($has) {
    Write-Output 'logon trigger already present, nothing to do'
    exit 0
}
$triggers = @($t.Triggers) + @(New-ScheduledTaskTrigger -AtLogOn)
Set-ScheduledTask -TaskName $name -Trigger $triggers | Out-Null
Write-Output 'logon trigger added to YaDiskWatchdog'
(Get-ScheduledTask -TaskName $name).Triggers | Format-List Enabled,BoundaryType,StartBoundary
