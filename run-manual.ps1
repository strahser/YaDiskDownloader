# Manual runner launcher. K1/M1 are not auto-started since 2026-10-23:
# the hourly restart loop only burned time on files Yandex refuses to serve.
# SSG is still started automatically by the watchdog task.
#
# Usage:
#   .\run-manual.ps1 -Config config-k1.yaml                # background, hidden window
#   .\run-manual.ps1 -Config config-k1.yaml -Foreground    # current window, live progress
#   .\run-manual.ps1 -Config config-m1.yaml -Wait          # background + wait for pass end
# ASCII-only.
param(
    [Parameter(Mandatory = $true)][string]$Config,
    [switch]$Foreground,
    [switch]$Wait
)

$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $Repo '.venv\Scripts\python.exe'
$cfgPath = Join-Path $Repo $Config
$logDir = Join-Path $Repo 'logs'

if (-not (Test-Path $cfgPath)) { Write-Host ('Config not found: ' + $Config); exit 1 }
if ($Config -like '*ssg*') {
    Write-Host 'SSG is auto-started by the watchdog task (YaDiskWatchdog) - manual run is not needed.'
    exit 0
}

$run = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
         Where-Object { $_.CommandLine -like ('*' + $Config + '*') })
if ($run.Count -gt 0) {
    Write-Host ('Already running: ' + $Config + ' pid=' + (($run | ForEach-Object { $_.ProcessId }) -join ','))
    exit 0
}

if ($Foreground) {
    Write-Host ('Running ' + $Config + ' in this window, one pass (Ctrl+C stops it)...')
    & $py -u 'src\main.py' --config $Config --once
    exit $LASTEXITCODE
}

$p = Start-Process -FilePath $py -ArgumentList '-u', 'src\main.py', '--config', $Config, '--once' `
                   -WorkingDirectory $Repo -WindowStyle Hidden -PassThru
Write-Host ('Started ' + $Config + ' pid=' + $p.Id + ' (one pass)')
Start-Sleep -Seconds 3
$log = Get-ChildItem $logDir -Filter 'download_*.log' -ErrorAction SilentlyContinue |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($log) { Write-Host ('Log: ' + $log.FullName) }

if ($Wait) {
    $p.WaitForExit()
    Write-Host ('Finished ' + $Config + ', exit code ' + $p.ExitCode)
}
