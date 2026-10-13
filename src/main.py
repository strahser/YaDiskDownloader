# -*- coding: utf-8 -*-
import sys
import re
import time
import asyncio
import logging
import shutil
from pathlib import Path
from datetime import datetime
from urllib.parse import urlencode, unquote
from typing import List, Dict, Any, Tuple

import aiohttp
import yaml
from tqdm import tqdm

BASE = Path(__file__).parent.parent
CONFIG_PATH = BASE / "config.yaml"
LOG_DIR = BASE / "logs"
API_BASE = "https://cloud-api.yandex.net/v1/disk"
CONCURRENT_DOWNLOADS = 3
MAX_RETRIES = 5


def setup_environment():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass


def setup_logging() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"download_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )
    return log_file


def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def yd_to_local(yd_path: str, download_dir: Path) -> Path:
    if yd_path.startswith("disk:/"):
        rel = yd_path[6:]
    else:
        rel = yd_path
    rel = rel.lstrip("/")
    rel = unquote(rel)

    parts = rel.split("/")
    sanitized_parts = []
    for part in parts:
        part = re.sub(r'[<>:"/\\|?*]', "_", part)
        part = part.rstrip(". ")
        sanitized_parts.append(part)
    return download_dir / Path(*sanitized_parts)


class YDiskClient:
    def __init__(self, token: str, session: aiohttp.ClientSession):
        self.token = token
        self.session = session

    async def _request(self, method: str, endpoint: str, params: dict = None) -> dict:
        qs = urlencode(params) if params else ""
        url = f"{API_BASE}{endpoint}?{qs}" if qs else f"{API_BASE}{endpoint}"
        headers = {"Authorization": f"OAuth {self.token}"}

        for attempt in range(MAX_RETRIES):
            try:
                timeout = aiohttp.ClientTimeout(total=30)
                async with self.session.request(method, url, headers=headers, timeout=timeout) as r:
                    if r.status == 429:
                        wait = int(r.headers.get("Retry-After", 2 ** attempt))
                        logging.warning(f"Rate limit (429). Ожидание {wait}с...")
                        await asyncio.sleep(wait)
                        continue
                    r.raise_for_status()
                    return await r.json()
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                logging.warning(f"Сетевая ошибка (попытка {attempt + 1}/{MAX_RETRIES}): {e}")
                if attempt == MAX_RETRIES - 1:
                    raise
                await asyncio.sleep(2 ** attempt)
        return {}

    async def list_dir(self, path: str) -> List[Dict[str, Any]]:
        items = []
        offset = 0
        limit = 1000
        while True:
            j = await self._request("GET", "/resources", {"path": path, "limit": limit, "offset": offset})
            embedded = j.get("_embedded", {})
            current_items = embedded.get("items", [])
            items.extend(current_items)
            total = embedded.get("total", 0)
            offset += limit
            if offset >= total or not current_items:
                break
        return items

    async def get_download_link(self, path: str) -> str:
        j = await self._request("GET", "/resources/download", {"path": path})
        return j.get("href", "")

    async def download_file(self, href: str, dest: Path, total_size: int) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)

        if dest.exists() and dest.stat().st_size == total_size:
            return True

        downloaded_size = 0
        mode = "wb"
        if dest.exists():
            downloaded_size = dest.stat().st_size
            if downloaded_size < total_size:
                mode = "ab"
            else:
                return True

        headers = {}
        if downloaded_size > 0:
            headers["Range"] = f"bytes={downloaded_size}-"
            logging.info(f"[RESUME] {dest.name} с {downloaded_size} байт")

        for attempt in range(MAX_RETRIES):
            try:
                timeout = aiohttp.ClientTimeout(total=3600, sock_connect=20, sock_read=60)
                async with self.session.get(href, headers=headers, timeout=timeout) as r:
                    if downloaded_size > 0 and r.status == 200:
                        logging.warning(f"Сервер игнорирует Range, перезапуск {dest.name}")
                        downloaded_size = 0
                        mode = "wb"
                    elif r.status not in (200, 206):
                        r.raise_for_status()

                    with open(dest, mode) as f:
                        async for chunk in r.content.iter_chunked(1024 * 1024):
                            f.write(chunk)
                    return True
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logging.warning(f"Ошибка скачивания (попытка {attempt + 1}/{MAX_RETRIES}) {dest.name}: {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise
        return False


async def scan_disk(disk: YDiskClient) -> List[Dict[str, Any]]:
    files = []
    queue = ["disk:/"]
    visited = {"disk:/"}
    logging.info("Сканирование диска...")
    scanned = 0
    t0 = time.time()
    while queue:
        batch = queue[:30]
        queue = queue[30:]

        async def fetch_one(p: str):
            try:
                return await disk.list_dir(p)
            except Exception as e:
                logging.error(f"Ошибка чтения папки {p}: {e}")
                return []

        results = await asyncio.gather(*[fetch_one(p) for p in batch])
        scanned += len(batch)
        for items in results:
            for it in items:
                p = it.get("path")
                if it.get("type") == "dir":
                    if p not in visited:
                        visited.add(p)
                        queue.append(p)
                else:
                    files.append(it)
        if scanned % 90 == 0 or not queue:
            logging.info(f"Скан: папок={scanned} файлов={len(files)} очередь={len(queue)} {time.time() - t0:.0f}с")
    logging.info(f"Сканирование готово: папок={scanned} файлов={len(files)} за {time.time() - t0:.0f}с")
    return files


async def process_file(
    disk: YDiskClient,
    f: Dict[str, Any],
    download_dir: Path,
    sem: asyncio.Semaphore,
    stats: Dict[str, int],
    errors: List[Tuple[Dict, str]],
):
    dest = yd_to_local(f["path"], download_dir)
    size = f.get("size") or 0

    if dest.exists() and dest.stat().st_size == size:
        stats["skipped"] += 1
        return

    async with sem:
        try:
            href = await disk.get_download_link(f["path"])
            if not href:
                raise ValueError("Пустая ссылка на скачивание")

            t0 = time.time()
            await disk.download_file(href, dest, size)
            dt = time.time() - t0

            size_mb = size / 1048576
            speed = size_mb / dt if dt > 0 and size_mb > 0 else 0
            stats["ok"] += 1
            logging.info(f"[OK  ] {f.get('name')} {dt:.1f}s {speed:.1f} MB/s")
        except Exception as e:
            errors.append((f, str(e)))
            logging.error(f"[FAIL] {f.get('name')}: {e}")


async def main():
    setup_environment()
    log_file = setup_logging()
    logging.info(f"Лог-файл: {log_file}")

    cfg = load_config()
    token = cfg.get("token")
    download_dir = Path(cfg.get("download_dir", "./downloads"))

    if not token or token == "REPLACE_WITH_YOUR_TOKEN":
        logging.error("Токен не задан в config.yaml")
        sys.exit(1)

    download_dir.mkdir(parents=True, exist_ok=True)

    connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        disk = YDiskClient(token, session)

        files = await scan_disk(disk)
        total_files = len(files)
        logging.info(f"Найдено файлов для обработки: {total_files}")

        total_size_needed = sum(
            f.get("size", 0) for f in files
            if not (yd_to_local(f["path"], download_dir).exists() and
                    yd_to_local(f["path"], download_dir).stat().st_size == f.get("size", 0))
        )
        free_space = shutil.disk_usage(download_dir).free
        if free_space < total_size_needed:
            logging.warning(
                f"ВНИМАНИЕ: Недостаточно места! Требуется: {total_size_needed / 1024**3:.2f} GB, "
                f"Доступно: {free_space / 1024**3:.2f} GB"
            )

        stats = {"ok": 0, "skipped": 0}
        errors = []
        sem = asyncio.Semaphore(CONCURRENT_DOWNLOADS)
        tasks = [process_file(disk, f, download_dir, sem, stats, errors) for f in files]

        with tqdm(total=total_files, desc="Скачивание", unit="файл") as pbar:
            async def track_task(task):
                await task
                pbar.update(1)
            await asyncio.gather(*[track_task(t) for t in tasks])

        final_errors: List[Tuple[Dict, str]] = []
        if errors:
            logging.info(f"=== ФИНАЛЬНЫЙ ПОВТОР ({len(errors)} файлов) ===")
            retry_tasks = [process_file(disk, f, download_dir, sem, stats, final_errors) for f, _ in errors]
            with tqdm(total=len(errors), desc="Повтор", unit="файл") as pbar:
                async def track_retry(task):
                    await task
                    pbar.update(1)
                await asyncio.gather(*[track_retry(t) for t in retry_tasks])

            if final_errors:
                logging.error(f"Не удалось скачать {len(final_errors)} файлов после повтора:")
                for f, e in final_errors:
                    logging.error(f"  {f.get('path')}: {e}")

        failed_count = len(final_errors) if errors else 0
        logging.info("=" * 60)
        logging.info(f"ИТОГО: {stats['ok']} успешно, {stats['skipped']} пропущено, {failed_count} ошибок")


if __name__ == "__main__":
    asyncio.run(main())
