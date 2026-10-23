# Watchdog for the SSG runner (K1/M1 are started manually, see run-manual.ps1).
# Guards:
#   * one runner pair per config; extra duplicates are killed
#   * restart throttle: if a restart was issued < 10 min ago for a config and it is still
#     dead, skip this cycle (prevents a tight restart loop on a persistently failing config)
#   * restart on config/code change so edits are applied without manual action
#   * one-shot stop request: logs\stop-request.txt with config names
# ASCII-only.
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log = Join-Path $Repo 'logs\watchdog.log'
$StateFile = Join-Path $Repo 'logs\watchdog-state.json'
$py = Join-Path $Repo '.venv\Scripts\python.exe'
$MinRestartGapMin = 10
# Автозапуск оставлен только для SSG (личный аккаунт ssg-81@yandex.ru -> d:\SsgYandexDisk).
# К1 и М1 по решению от 22.10.2026 качаются вручную (см. run-manual.ps1):
# автоперезапуск по кругу только жёг время на недоступных файлах.
$Configs = @('config-ssg.yaml')
# Разовый стоп по заявке: logs\stop-request.txt со списком конфигов (по одному в строке).
# Нужен потому, что раннеры живут в session 0 (S4U) и из интерактивной сессии их не убить.
$StopFile = Join-Path $Repo 'logs\stop-request.txt'
function WLog($m) { Add-Content -Path $Log -Value ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m) }

# Get-WmiObject отдаёт CreationDate строкой в формате DMTF ("20261022140619.000000+180"),
# поэтому приводим к DateTime вручную — иначе сравнение с временем файла не работает.
function ToDate($v) {
    if ($v -is [datetime]) { return $v }
    if ($v) {
        try { return [datetime]::ParseExact($v.Substring(0, 14), 'yyyyMMddHHmmss', $null) } catch { }
    }
    return [datetime]::MinValue
}

$state = @{}
if (Test-Path $StateFile) {
    try {
        $raw = Get-Content $StateFile -Raw -ErrorAction Stop
        if ($raw) {
            (ConvertFrom-Json $raw).PSObject.Properties | ForEach-Object { $state[$_.Name] = [string]$_.Value }
        }
    } catch { WLog ('state read failed: ' + $_.Exception.Message) }
}

if (Test-Path $StopFile) {
    foreach ($c in @(Get-Content $StopFile -Encoding UTF8 | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
        $rp = @(Get-WmiObject Win32_Process -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like ('*' + $c + '*') })
        foreach ($p in $rp) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            WLog ('stopped by request ' + $c + ' pid=' + $p.ProcessId)
        }
        if ($rp.Count -eq 0) { WLog ('stop request ' + $c + ': nothing running') }
    }
    Remove-Item $StopFile -Force -ErrorAction SilentlyContinue
}

foreach ($c in $Configs) {
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

    # Конфиг или код изменились позже старта раннера -> перезапуск с новыми
    # настройками. Иначе правка config-*.yaml осталась бы незамеченной.
    if ($procs.Count -gt 0) {
        $oldest = ToDate ($procs | Select-Object -First 1).CreationDate
        $cfgPath = Join-Path $Repo $c
        $cfgTime = if (Test-Path $cfgPath) { (Get-Item $cfgPath).LastWriteTime } else { [datetime]::MinValue }
        $codePath = Join-Path $Repo 'src\main.py'
        $codeTime = if (Test-Path $codePath) { (Get-Item $codePath).LastWriteTime } else { [datetime]::MinValue }
        $changed = if ($cfgTime -gt $codeTime) { $cfgTime } else { $codeTime }
        if ($changed -gt $oldest) {
            WLog ($c + ' config/code changed (' + $changed.ToString('yyyy-MM-dd HH:mm:ss') + ') -> restart')
            foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 2
            $procs = @()
            # Перезапуск по правке конфига троттлом не блокируем.
            $state[$c] = '2000-01-01T00:00:00'
        }
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
    # -WindowStyle Hidden: без него каждый раннер открывает отдельное видимое окно
    # консоли, которое легко закрыть случайно — это убивает процесс без трассировки.
    Start-Process -FilePath $py -ArgumentList '-u', 'src\main.py', '--config', $c `
                  -WorkingDirectory $Repo -WindowStyle Hidden
    WLog ('restart issued ' + $c)
    $state[$c] = (Get-Date).ToString('s')
}

try { ($state | ConvertTo-Json) | Set-Content -Path $StateFile -Encoding UTF8 }
catch { WLog ('state write failed: ' + $_.Exception.Message) }
