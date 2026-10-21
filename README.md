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

Скопируй образец конфига и подставь свои значения:
- один общий конфиг — `config.example.yaml` → `config.yaml`;
- раннер К1 — `config-k1.example.yaml` → `config-k1.yaml`;
- раннер М1 — `config-m1.example.yaml` → `config-m1.yaml`;
- раннер SSG — `config-ssg.example.yaml` → `config-ssg.yaml` (личный аккаунт
  `ssg-81@yandex.ru` → `d:\SsgYandexDisk`).

Ключи:
- `token` — OAuth-токен Яндекс.Диска;
- `refresh_token`, `client_id`, `client_secret` — необязательно, если задан `refresh_token`, токен освежается сам;
- `download_dir` — куда скачивать (по умолчанию `d:\YandexProjects`);
- `workers` — сколько файлов качать одновременно;
- `roots` — корни обхода (`disk:/К1`, `disk:/М1`, …);
- `priority` — папки, которые качаются первыми (внутри них — мелкие файлы);
- `repeat_delay_minutes` — пауза между проходами в цикловом режиме, `0` = один проход и выход.

> Файлы `config.yaml`, `config-k1.yaml`, `config-m1.yaml`, `config-ssg.yaml`
> содержат настоящие токены и намеренно не отслеживаются git — в репозитории
> лежат только `*.example.yaml`. Не добавляй их в индекс случайно (`git add -f`).

## Запуск

```powershell
# один общий процесс
.\.venv\Scripts\python.exe src\main.py

# разделённые раннеры (второй конфиг — отдельный процесс)
.\.venv\Scripts\python.exe src\main.py --config config-k1.yaml
.\.venv\Scripts\python.exe src\main.py --config config-m1.yaml
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
- держит по одному процессу на конфиг (`config-k1.yaml`, `config-m1.yaml`,
  `config-ssg.yaml`), лишние дубликаты убивает;
- не перезапускает конфиг чаще, чем раз в 10 минут (защита от цикла);
- состояние попыток — `logs\watchdog-state.json`, журнал — `logs\watchdog.log`.
