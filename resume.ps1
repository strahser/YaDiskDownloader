# Manual helper: make sure the watchdog task is enabled and SSG is running.
# K1/M1 are started manually via run-manual.ps1 since 2026-10-23. ASCII-only.
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log = Join-Path $Repo 'logs\watchdog.log'
function WLog($m) { Add-Content -Path $Log -Value ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m) }

WLog 'manual resume -> enable watchdog task'
Enable-ScheduledTask -TaskName 'YaDiskWatchdog' | Out-Null

$c = 'config-ssg.yaml'
$run = @(Get-WmiObject Win32_Process -ErrorAction SilentlyContinue |
         Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like ('*' + $c + '*') })
if ($run.Count -eq 0) {
    Start-Process -FilePath (Join-Path $Repo '.venv\Scripts\python.exe') `
                  -ArgumentList '-u', 'src\main.py', '--config', $c `
                  -WorkingDirectory $Repo -WindowStyle Hidden
    WLog ('started ' + $c + ' (manual resume)')
} else {
    WLog ($c + ' already running, skip start')
}
