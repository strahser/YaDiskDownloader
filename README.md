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
