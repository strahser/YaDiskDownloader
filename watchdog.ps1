# Watchdog for split runners k1/m1: keep exactly one runner pair alive per config.
# Guards:
#   * de-duplicates: if more than 2 python processes match a config, kill the newest extras
#   * restart throttle: if a restart was issued < 10 min ago for a config and it is still
#     dead, skip this cycle (prevents a tight restart loop on a persistently failing config)
# ASCII-only.
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log = Join-Path $Repo 'logs\watchdog.log'
$StateFile = Join-Path $Repo 'logs\watchdog-state.json'
$py = Join-Path $Repo '.venv\Scripts\python.exe'
$MinRestartGapMin = 10
function WLog($m) { Add-Content -Path $Log -Value ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m) }

$state = @{}
if (Test-Path $StateFile) {
    try {
        $raw = Get-Content $StateFile -Raw -ErrorAction Stop
        if ($raw) {
            (ConvertFrom-Json $raw).PSObject.Properties | ForEach-Object { $state[$_.Name] = [string]$_.Value }
        }
    } catch { WLog ('state read failed: ' + $_.Exception.Message) }
}

foreach ($c in @('config-k1.yaml', 'config-m1.yaml')) {
    $procs = @(Get-WmiObject Win32_Process -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like ('*' + $c + '*') } |
               Sort-Object CreationDate)

    if ($procs.Count -gt 2) {
        foreach ($p in @($procs | Select-Object -Skip 2)) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            WLog ('killed duplicate ' + $c + ' pid=' + $p.ProcessId)
        }
        $procs = @($procs | Select-Object -First 2)
    }

    if ($procs.Count -gt 0) {
        WLog ('OK ' + $c + ' pid=' + (($procs | Select-Object -ExpandProperty ProcessId) -join ','))
        continue
    }

    $last = $null
    if ($state.ContainsKey($c)) { try { $last = [datetime]::Parse($state[$c]) } catch { $last = $null } }
    if ($last -and ((Get-Date) - $last).TotalMinutes -lt $MinRestartGapMin) {
        WLog ('skip restart ' + $c + ' (last attempt ' + $last.ToString('HH:mm:ss') + ')')
        continue
    }

    WLog ($c + ' NOT RUNNING -> restart')
    Start-Process -FilePath $py -ArgumentList '-u', 'src\main.py', '--config', $c -WorkingDirectory $Repo
    WLog ('restart issued ' + $c)
    $state[$c] = (Get-Date).ToString('s')
}

try { ($state | ConvertTo-Json) | Set-Content -Path $StateFile -Encoding UTF8 }
catch { WLog ('state write failed: ' + $_.Exception.Message) }
