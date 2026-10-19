# YaDisk Downloader

Скачивает все файлы с Яндекс.Диска через API.

## Установка

```powershell
cd d:\Projects\YaDiskDownloader
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Настройка

Отредактируй `config.yaml`:
- `token` — OAuth-токен Яндекс.Диска
- `download_dir` — куда скачивать (по умолчанию `d:\YandexProjects`)

## Запуск

```powershell
.\.venv\Scripts\python.exe src\main.py
```

Логи пишутся в `logs\download_YYYYMMDD_HHMMSS.log`.

## Режимы запуска

```powershell
# один проход (скан → скачивание → финальный повтор) и выход
.\.venv\Scripts\python.exe src\main.py --config config-k1.yaml

# то же, но по кругу: см. repeat_delay_minutes в конфиге
```

`repeat_delay_minutes: 10` включает цикловой режим: после прохода процесс спит
10 минут и идёт новый круг. Если проход не дал ни одного успеха, пауза
удваивается с каждым таким проходом до 120 минут (2 часа) и сбрасывается,
как только появились успехи. Без этого ключа поведение прежнее — один проход
и выход.

Зависание: если во время прохода 20 минут подряд не было ни одной попытки
скачивания или страницы скана, процесс принудительно завершается с кодом 2,
чтобы внешний сторож (`watchdog.ps1`) поднял его заново.

## Сторож

`watchdog.ps1` (задача планировщика `YaDiskWatchdog`, раз в час):
- держит по одному процессу на конфиг, лишние дубликаты убивает;
- не перезапускает конфиг чаще, чем раз в 10 минут (защита от цикла);
- состояние попыток — `logs\watchdog-state.json`, журнал — `logs\watchdog.log`.
