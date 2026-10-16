# Watchdog for split runners k1/m1: ensure both alive, restart dead. ASCII-only.
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log = Join-Path $Repo 'logs\watchdog.log'
$py = Join-Path $Repo '.venv\Scripts\python.exe'
function WLog($m) { Add-Content -Path $Log -Value ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m) }
foreach ($c in @('config-k1.yaml', 'config-m1.yaml')) {
    $run = @(Get-WmiObject Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like ('*' + $c + '*') })
    if ($run.Count -gt 0) {
        WLog ('OK ' + $c + ' pid=' + (($run | Select-Object -ExpandProperty ProcessId) -join ','))
    } else {
        WLog ($c + ' NOT RUNNING -> restart')
        Start-Process -FilePath $py -ArgumentList '-u', 'src\main.py', '--config', $c -WorkingDirectory $Repo
        WLog ('restart issued ' + $c)
    }
}
