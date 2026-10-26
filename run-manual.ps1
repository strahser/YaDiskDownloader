# Manual runner launcher. K1/M1 are not auto-started since 2026-10-23:
# the hourly restart loop only burned time on files Yandex refuses to serve.
# SSG is still started automatically by the watchdog task.
#
# Usage:
#   .\run-manual.ps1 -Config config-k1.yaml                # background, hidden window
#   .\run-manual.ps1 -Config config-k1.yaml -Foreground    # current window, live progress
#   .\run-manual.ps1 -Config config-m1.yaml -Wait          # background + wait for pass end
#   .\run-manual.ps1 -Config config-k1.yaml -RetryDead     # forget dead cache, retry all
# ASCII-only.
param(
    [Parameter(Mandatory = $true)][string]$Config,
    [switch]$Foreground,
    [switch]$Wait,
    [switch]$RetryDead
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

# -RetryDead: убрать кэш «Яндекс не отдаёт», чтобы проход снова попробовал эти файлы.
# Иначе они пропускаются 12 часов с момента прошлой неудачи и ручной проход их не тронет.
if ($RetryDead) {
    $deadName = 'dead_' + [IO.Path]::GetFileNameWithoutExtension($Config) + '.json'
    $deadFile = Join-Path $logDir $deadName
    if (Test-Path $deadFile) {
        $bak = $deadFile + '.bak-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
        Move-Item -LiteralPath $deadFile -Destination $bak -Force
        Write-Host ('Dead cache moved to ' + $bak)
    } else {
        Write-Host ('No dead cache: ' + $deadName)
    }
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
# CreationTime, не LastWriteTime: у открытого лог-файла Windows «замораживает»
# дату изменения, и самый свежий по mtime может оказаться чужим (например, SSG).
$log = Get-ChildItem $logDir -Filter 'download_*.log' -ErrorAction SilentlyContinue |
       Sort-Object CreationTime -Descending | Select-Object -First 1
if ($log) { Write-Host ('Log: ' + $log.FullName) }

if ($Wait) {
    $p.WaitForExit()
    Write-Host ('Finished ' + $Config + ', exit code ' + $p.ExitCode)
}
