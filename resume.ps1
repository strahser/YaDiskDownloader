# Resume after cooldown: re-enable watchdog, start both runners if dead. ASCII-only.
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log = Join-Path $Repo 'logs\watchdog.log'
function WLog($m) { Add-Content -Path $Log -Value ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m) }
WLog 'cooldown over -> enable watchdog'
Enable-ScheduledTask -TaskName 'YaDiskWatchdog' | Out-Null
foreach ($c in @('config-k1.yaml', 'config-m1.yaml')) {
    $run = @(Get-WmiObject Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like ('*' + $c + '*') })
    if ($run.Count -eq 0) {
        Start-Process -FilePath (Join-Path $Repo '.venv\Scripts\python.exe') -ArgumentList '-u', 'src\main.py', '--config', $c -WorkingDirectory $Repo -WindowStyle Hidden
        WLog ('started ' + $c + ' after cooldown')
    } else {
        WLog ($c + ' already running, skip start')
    }
}
