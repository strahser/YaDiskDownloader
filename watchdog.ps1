# Watchdog: hourly check for main.py, restart if dead. ASCII-only.
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log = Join-Path $Repo 'logs\watchdog.log'
$py = Join-Path $Repo '.venv\Scripts\python.exe'
function WLog($m) { Add-Content -Path $Log -Value ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m) }
$run = @(Get-WmiObject Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*src\main.py*' })
if ($run.Count -gt 0) {
    WLog ('OK pid=' + (($run | Select-Object -ExpandProperty ProcessId) -join ','))
    exit 0
}
WLog 'main.py NOT RUNNING -> restart'
Start-Process -FilePath $py -ArgumentList '-u', 'src\main.py' -WorkingDirectory $Repo
WLog 'restart issued'
