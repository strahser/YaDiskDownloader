# -*- coding: utf-8 -*-
import sys
import os
import re
import time
import asyncio
import itertools
import logging
import shutil
from pathlib import Path
from datetime import datetime
from urllib.parse import urlencode, unquote
from typing import List, Dict, Any, Tuple, Optional

import aiohttp
import yaml
from tqdm import tqdm

BASE = Path(__file__).parent.parent
CONFIG_PATH = Path(os.environ.get("YADISK_CONFIG", BASE / "config.yaml"))
LOG_DIR = BASE / "logs"
API_BASE = "https://cloud-api.yandex.net/v1/disk"
CONCURRENT_DOWNLOADS = 3
MAX_RETRIES = 2
QUEUE_MAXSIZE = 1000
# Пауза перед стартом новой загрузки, если недавно была сетевая ошибка
# (storage Яндекса начинает капать новые соединения при агрессивном потоке).
COOLDOWN_AFTER_FAIL = 30.0
_dl_state = {"last_fail": 0.0}


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
    def __init__(self, token: str, session: aiohttp.ClientSession, dl_session: aiohttp.ClientSession = None):
        self.token = token
        self.session = session
        # Отдельная сессия для скачивания: долгие загрузки больших файлов
        # не должны отъедать коннекты у быстрых API-запросов сканирования.
        self.dl_session = dl_session or session

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

    async def disk_info(self) -> dict:
        return await self._request("GET", "", None)

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

        for attempt in range(MAX_RETRIES):
            # Точку resume пересчитываем КАЖДУЮ попытку: прошлая могла
            # успеть дописать часть файла до обрыва.
            downloaded_size = 0
            mode = "wb"
            if dest.exists():
                cur = dest.stat().st_size
                if cur == total_size:
                    return True
                if 0 < cur < total_size:
                    downloaded_size = cur
                    mode = "ab"

            headers = {}
            if downloaded_size > 0:
                headers["Range"] = f"bytes={downloaded_size}-"
                logging.info(f"[RESUME] {dest.name} с {downloaded_size} байт (попытка {attempt + 1})")

            try:
                timeout = aiohttp.ClientTimeout(total=3600, sock_connect=20, sock_read=60)
                written = 0
                async with self.dl_session.get(href, headers=headers, timeout=timeout) as r:
                    if downloaded_size > 0 and r.status == 200:
                        logging.warning(f"Сервер игнорирует Range, перезапуск {dest.name}")
                        downloaded_size = 0
                        mode = "wb"
                    elif r.status not in (200, 206):
                        r.raise_for_status()

                    # При повторе после ignore-Range открываем заново в wb
                    with open(dest, mode) as f:
                        async for chunk in r.content.iter_chunked(1024 * 1024):
                            f.write(chunk)
                            written += len(chunk)
                    return True
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                _dl_state["last_fail"] = time.monotonic()
                logging.warning(
                    f"Ошибка скачивания (попытка {attempt + 1}/{MAX_RETRIES}) {dest.name}: {e} "
                    f"(в этой попытке получено {written / 1048576:.1f} МБ)"
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise
        return False


async def fetch_file(disk: YDiskClient, f: Dict[str, Any], download_dir: Path) -> str:
    """Скачать один файл. Возвращает 'ok' / 'skipped', при ошибке — исключение."""
    dest = yd_to_local(f["path"], download_dir)
    size = f.get("size") or 0

    if dest.exists() and dest.stat().st_size == size:
        return "skipped"

    href = await disk.get_download_link(f["path"])
    if not href:
        raise ValueError("Пустая ссылка на скачивание")

    t0 = time.time()
    await disk.download_file(href, dest, size)
    dt = time.time() - t0

    size_mb = size / 1048576
    speed = size_mb / dt if dt > 0 and size_mb > 0 else 0
    logging.info(f"[OK  ] {f.get('name')} {dt:.1f}s {speed:.1f} MB/s")
    return "ok"


async def scan_and_queue(
    disk: YDiskClient,
    queue: "asyncio.PriorityQueue",
    workers: int,
    roots: List[str],
    priority: List[str] = None,
) -> Tuple[int, int]:
    """BFS-обход: файлы сразу кладутся в очередь на скачивание.
    Приоритет: сначала priority-папки, внутри — мелкие файлы первыми,
    чтобы гигабайтные «холодные» файлы не блокировали поток.
    Возвращает (папок_просканировано, файлов_найдено)."""
    pending = list(roots)
    visited = set(roots)
    prio = priority or []

    def prio_of(p: str) -> int:
        for pr in prio:
            if p == pr or p.startswith(pr + "/"):
                return 0
        return 1
    seq = itertools.count()
    scanned = 0
    found = 0
    t0 = time.time()
    logging.info("Сканирование диска (файлы качаются по ходу)...")
    logging.info(f"Корни обхода: {roots}")
    while pending:
        batch = pending[:30]
        pending = pending[30:]

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
                        pending.append(p)
                else:
                    found += 1
                    await queue.put((prio_of(it["path"]), it.get("size") or 0, next(seq), it))
        if scanned % 90 == 0 or not pending:
            logging.info(f"Скан: папок={scanned} файлов_в_работе={found} очередь_папок={len(pending)} {time.time() - t0:.0f}с")
    logging.info(f"Сканирование готово: папок={scanned} файлов={found} за {time.time() - t0:.0f}с")
    for _ in range(workers):
        await queue.put((2 ** 62, 2 ** 62, next(seq), None))
    return scanned, found


async def worker(
    disk: YDiskClient,
    queue: "asyncio.PriorityQueue",
    download_dir: Path,
    stats: Dict[str, int],
    errors: List[Tuple[Dict, str]],
    pbar: tqdm,
):
    while True:
        _, _, _, f = await queue.get()
        if f is None:
            queue.task_done()
            break
        # Джентльменская пауза после недавних ошибок: не долбим storage,
        # иначе он начинает подвешивать новые соединения.
        now = time.monotonic()
        wait = COOLDOWN_AFTER_FAIL - (now - _dl_state["last_fail"])
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            r = await fetch_file(disk, f, download_dir)
            stats[r] += 1
        except Exception as e:
            _dl_state["last_fail"] = time.monotonic()
            errors.append((f, str(e)))
            logging.error(f"[FAIL] {f.get('name')}: {e}")
        finally:
            pbar.update(1)
            queue.task_done()


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
    roots = cfg.get("roots") or ["disk:/"]
    priority = cfg.get("priority") or []
    num_workers = int(cfg.get("workers", CONCURRENT_DOWNLOADS))
    priority = cfg.get("priority") or []

    connector = aiohttp.TCPConnector(limit=8, ttl_dns_cache=300)
    dl_connector = aiohttp.TCPConnector(limit=num_workers + 1, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session, \
               aiohttp.ClientSession(connector=dl_connector) as dl_session:
        disk = YDiskClient(token, session, dl_session)

        try:
            info = await disk.disk_info()
            need = info.get("used_space", 0)
            free = shutil.disk_usage(download_dir).free
            logging.info(f"Диск: user={info.get('user', {}).get('login')} занято={need / 1024**3:.1f} GB, свободно локально={free / 1024**3:.1f} GB")
            if free < need:
                logging.warning("Места может не хватить на всё содержимое диска!")
        except Exception as e:
            logging.warning(f"Не удалось получить инфо диска: {e}")

        queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=QUEUE_MAXSIZE)
        stats = {"ok": 0, "skipped": 0}
        errors: List[Tuple[Dict, str]] = []

        with tqdm(desc="Скачивание", unit="файл") as pbar:
            workers = [
                asyncio.create_task(worker(disk, queue, download_dir, stats, errors, pbar))
                for _ in range(num_workers)
            ]
            scanned, found = await scan_and_queue(disk, queue, num_workers, roots, priority)
            pbar.total = found
            pbar.refresh()
            await asyncio.gather(*workers)

        final_errors: List[Tuple[Dict, str]] = []
        if errors:
            logging.info(f"=== ФИНАЛЬНЫЙ ПОВТОР ({len(errors)} файлов) ===")
            with tqdm(total=len(errors), desc="Повтор", unit="файл") as pbar:
                for f, _ in errors:
                    try:
                        r = await fetch_file(disk, f, download_dir)
                        stats[r] += 1
                    except Exception as e:
                        final_errors.append((f, str(e)))
                        logging.error(f"[FAIL] {f.get('name')}: {e}")
                    pbar.update(1)

            if final_errors:
                logging.error(f"Не удалось скачать {len(final_errors)} файлов после повтора:")
                for f, e in final_errors:
                    logging.error(f"  {f.get('path')}: {e}")

        failed_count = len(final_errors) if errors else 0
        logging.info("=" * 60)
        logging.info(f"ИТОГО: папок={scanned} {stats['ok']} успешно, {stats['skipped']} пропущено, {failed_count} ошибок")


if __name__ == "__main__":
    asyncio.run(main())
