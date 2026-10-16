# Resume after cooldown: re-enable watchdog, start main.py if dead. ASCII-only.
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log = Join-Path $Repo 'logs\watchdog.log'
function WLog($m) { Add-Content -Path $Log -Value ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m) }
WLog 'cooldown over -> enable watchdog'
Enable-ScheduledTask -TaskName 'YaDiskWatchdog' | Out-Null
$run = @(Get-WmiObject Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*src\main.py*' })
if ($run.Count -eq 0) {
    Start-Process -FilePath (Join-Path $Repo '.venv\Scripts\python.exe') -ArgumentList '-u', 'src\main.py' -WorkingDirectory $Repo
    WLog 'main started after cooldown'
} else {
    WLog 'main already running, skip start'
}
