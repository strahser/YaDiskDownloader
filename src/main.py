# -*- coding: utf-8 -*-
import sys
import os
import re
import time
import asyncio
import itertools
import json
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


def _config_path() -> Path:
    import argparse
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--config", default=None)
    args, _ = ap.parse_known_args()
    if args.config:
        return Path(args.config)
    return Path(os.environ.get("YADISK_CONFIG", BASE / "config.yaml"))


CONFIG_PATH = _config_path()
LOG_DIR = BASE / "logs"
API_BASE = "https://cloud-api.yandex.net/v1/disk"
CONCURRENT_DOWNLOADS = 3
MAX_RETRIES = 2
QUEUE_MAXSIZE = 1000
# Пауза перед стартом новой загрузки, если недавно была сетевая ошибка
# (storage Яндекса начинает капать новые соединения при агрессивном потоке).
COOLDOWN_AFTER_FAIL = 10.0
# «Мёртвые» файлы: Яндекс их не отдаёт (таймаут сокета / ContentLengthError).
# Один такой файл стоит ~40 с (две попытки + пауза), а проходов много — поэтому
# запоминаем их и пропускаем на DEAD_TTL_HOURS, после чего пробуем снова.
DEAD_TTL_HOURS = 12.0
# Эталонный (заведомо скачанный) файл: по нему проверяем, жива ли сеть, когда
# проход не дал ни одного успеха. Если канарейка не проходит — это обрыв сети,
# и метки «мёртвых» файлов надо снять, иначе просидим на них полдня.
CANARY_STATE_PATH = LOG_DIR / f"canary_{CONFIG_PATH.stem}.json"
# Сколько отказов нужно, чтобы считать файл мёртвым: две попытки уже делает
# download_file, поэтому одного [FAIL] достаточно.
DEAD_AFTER_FAILS = 1
# Файл помечается мёртвым, только если рядом было успешное скачивание: иначе
# при общем обрыве сети мы бы «похоронили» весь диск на несколько часов.
DEAD_CONFIRM_WINDOW = 600.0
DEAD_STATE_PATH = LOG_DIR / f"dead_{CONFIG_PATH.stem}.json"
# Цикловой режим: максимальная пауза между проходами (2 часа, как договаривались).
MAX_IDLE_SLEEP = 7200.0
# Сторож зависаний: если столько минут подряд в проходе не было ни одной попытки,
# считаем процесс зависшим и выходим — внешний сторож поднимет его заново.
HANG_TIMEOUT_MINUTES = 20
_dl_state = {"last_fail": 0.0}
_state = {"active": False, "last": 0.0, "last_ok": 0.0}
_dead: Dict[str, Dict[str, float]] = {}
_dead_dirty = 0


def touch_activity():
    """Отметка «процесс жив»: любая попытка скачивания или страница скана."""
    _state["last"] = time.monotonic()


async def hang_guard():
    """Force-exit, если проход замер и внештатно висит без прогресса."""
    while True:
        await asyncio.sleep(60)
        if _state["active"] and (_state["last"] and
                                 time.monotonic() - _state["last"] > HANG_TIMEOUT_MINUTES * 60):
            logging.critical(f"Нет активности более {HANG_TIMEOUT_MINUTES} мин — "
                             f"выходим, чтобы сторож перезапустил процесс")
            logging.shutdown()
            os._exit(2)


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


# ---------- кэш «мёртвых» файлов ----------

def load_dead() -> None:
    global _dead
    try:
        if DEAD_STATE_PATH.exists():
            raw = json.loads(DEAD_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _dead = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        logging.info(f"Кэш мёртвых файлов: {len(_dead)} записей ({DEAD_STATE_PATH.name})")
    except Exception as e:
        logging.warning(f"Не удалось прочитать кэш мёртвых файлов: {e}")


def save_dead(force: bool = False) -> None:
    global _dead_dirty
    if not force and not _dead_dirty:
        return
    if force and not _dead_dirty and DEAD_STATE_PATH.exists():
        return
    try:
        # Чистим давно просроченные записи, чтобы файл не рос бесконечно.
        cutoff = time.time() - DEAD_TTL_HOURS * 3600 * 4
        fresh = {k: v for k, v in _dead.items() if float(v.get("t", 0)) >= cutoff}
        tmp = DEAD_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, DEAD_STATE_PATH)
        _dead_dirty = 0
    except Exception as e:
        logging.warning(f"Не удалось сохранить кэш мёртвых файлов: {e}")


def is_dead(path: str) -> bool:
    v = _dead.get(path)
    if not v:
        return False
    if int(v.get("n", 0)) < DEAD_AFTER_FAILS:
        return False
    return (time.time() - float(v.get("t", 0))) < DEAD_TTL_HOURS * 3600


def mark_dead(path: str) -> None:
    """Запомнить файл как мёртвый.

    «Подтверждённой» (c=1) считаем метку, если рядом было успешное скачивание,
    то есть сервер отдаёт другие файлы. Неподтверждённые метки снимаются, если
    канарейка показала общий обрыв сети.
    """
    global _dead_dirty
    last_ok = _state.get("last_ok") or 0.0
    confirmed = 1 if (last_ok and (time.monotonic() - last_ok) <= DEAD_CONFIRM_WINDOW) else 0
    v = _dead.setdefault(path, {"n": 0, "t": 0.0, "c": 0})
    v["n"] = int(v.get("n", 0)) + 1
    v["t"] = time.time()
    v["c"] = max(int(v.get("c", 0)), confirmed)
    _dead_dirty += 1
    save_dead()


def purge_unconfirmed() -> int:
    """Снять метки, поставленные без подтверждения (при общем обрыве сети)."""
    global _dead_dirty
    bad = [k for k, v in _dead.items() if int(v.get("c", 0)) == 0]
    for k in bad:
        _dead.pop(k, None)
    if bad:
        _dead_dirty += len(bad)
        save_dead(force=True)
    return len(bad)


def load_canary() -> None:
    try:
        if CANARY_STATE_PATH.exists():
            j = json.loads(CANARY_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(j, dict) and j.get("path"):
                _state["canary"] = {"path": str(j["path"]), "size": int(j.get("size") or 0)}
    except Exception as e:
        logging.warning(f"Не удалось прочитать эталонный файл: {e}")


def save_canary() -> None:
    try:
        if _state.get("canary"):
            CANARY_STATE_PATH.write_text(
                json.dumps(_state["canary"], ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logging.warning(f"Не удалось сохранить эталонный файл: {e}")


def clear_dead(path: str) -> None:
    global _dead_dirty
    if _dead.pop(path, None) is not None:
        _dead_dirty += 1
        save_dead()


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


def lp(path: Path) -> str:
    """Строка пути с префиксом \\\\?\\ — для длинных путей (>=260 символов).

    Windows по умолчанию ограничивает путь 260 символами (LongPathsEnabled=0),
    и API не видит такие файлы: dest.exists() -> False, open() -> WinError 3.
    С префиксом \\\\?\\ работают и существующие, и только что созданные файлы.
    """
    s = str(path)
    if len(s) >= 260 and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s
    return s


def lp_size(path: Path) -> int:
    """Размер файла (для длинных путей — через lp). -1, если файла нет."""
    try:
        return os.stat(lp(path)).st_size
    except OSError:
        return -1


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
        # Для длинных путей (>260 симв.) работаем через префикс \\?\
        os.makedirs(lp(dest.parent), exist_ok=True)

        for attempt in range(MAX_RETRIES):
            # Точку resume пересчитываем КАЖДУЮ попытку: прошлая могла
            # успеть дописать часть файла до обрыва.
            downloaded_size = 0
            mode = "wb"
            cur = lp_size(dest)
            if cur >= 0:
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
                # sock_read=20: мёртвый файл не отдаёт вообще ничего, и ждать
                # минуту на каждую из двух попыток — это 2 минуты впустую.
                # Живые файлы идут потоком, таймаут между чанками не мешает.
                timeout = aiohttp.ClientTimeout(total=3600, sock_connect=20, sock_read=20)
                written = 0
                async with self.dl_session.get(href, headers=headers, timeout=timeout) as r:
                    if downloaded_size > 0 and r.status == 200:
                        logging.warning(f"Сервер игнорирует Range, перезапуск {dest.name}")
                        downloaded_size = 0
                        mode = "wb"
                    elif r.status not in (200, 206):
                        r.raise_for_status()

                    # При повторе после ignore-Range открываем заново в wb
                    with open(lp(dest), mode) as f:
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

    # lp_size: длинные пути (>260 симв.) обычный Path.stat() не видит —
    # без этого файл, уже лежащий на диске, считается отсутствующим.
    if lp_size(dest) == size:
        return "skipped"

    path = f["path"]
    if is_dead(path):
        return "dead"

    href = await disk.get_download_link(path)
    if not href:
        raise ValueError("Пустая ссылка на скачивание")

    t0 = time.time()
    await disk.download_file(href, dest, size)
    dt = time.time() - t0

    _state["last_ok"] = time.monotonic()
    clear_dead(path)
    if size and size <= 5 * 1024 * 1024:
        # Небольшой успешно скачанный файл — эталон для проверки канала.
        _state["canary"] = {"path": path, "size": int(size)}
    size_mb = size / 1048576
    speed = size_mb / dt if dt > 0 and size_mb > 0 else 0
    logging.info(f"[OK  ] {f.get('name')} {dt:.1f}s {speed:.1f} MB/s")
    return "ok"


async def canary_ok(disk: YDiskClient) -> bool:
    """Проверка канала: качается ли эталонный файл прямо сейчас."""
    c = _state.get("canary")
    if not c:
        return False
    try:
        href = await disk.get_download_link(c["path"])
        if not href:
            return False
        timeout = aiohttp.ClientTimeout(total=25, sock_connect=10, sock_read=15)
        async with disk.dl_session.get(href, timeout=timeout) as r:
            if r.status not in (200, 206):
                return False
            chunk = await r.content.read(8192)
            return bool(chunk)
    except Exception as e:
        logging.info(f"Канарейка не прошла: {e}")
        return False


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
        touch_activity()
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
        touch_activity()
        if f is None:
            queue.task_done()
            break
        # Мёртвые файлы пропускаем мгновенно, до паузы: иначе 200+ таких
        # файлов съедают по 10 с каждый на пустом месте.
        if is_dead(f.get("path")):
            stats["dead"] += 1
            pbar.update(1)
            queue.task_done()
            touch_activity()
            continue
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
            mark_dead(f.get("path"))
            logging.error(f"[FAIL] {f.get('name')}: {e}")
        finally:
            pbar.update(1)
            queue.task_done()
            touch_activity()


async def run_pass() -> Tuple[int, int, int]:
    """Один проход: скан диска → скачивание → финальный повтор ошибок."""
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
        stats = {"ok": 0, "skipped": 0, "dead": 0}
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
        retry_list = [(f, e) for f, e in errors if not is_dead(f.get("path"))]
        skipped_dead = len(errors) - len(retry_list)
        if skipped_dead:
            logging.info(f"Финальный повтор: пропущено {skipped_dead} мёртвых файлов")
        if retry_list:
            logging.info(f"=== ФИНАЛЬНЫЙ ПОВТОР ({len(retry_list)} файлов) ===")
            with tqdm(total=len(retry_list), desc="Повтор", unit="файл") as pbar:
                for f, _ in retry_list:
                    try:
                        r = await fetch_file(disk, f, download_dir)
                        stats[r] += 1
                    except Exception as e:
                        final_errors.append((f, str(e)))
                        mark_dead(f.get("path"))
                        logging.error(f"[FAIL] {f.get('name')}: {e}")
                    pbar.update(1)

            if final_errors:
                logging.error(f"Не удалось скачать {len(final_errors)} файлов после повтора:")
                for f, e in final_errors:
                    logging.error(f"  {f.get('path')}: {e}")

        failed_count = len(final_errors) if retry_list else 0
        save_dead(force=True)
        save_canary()
        # Если проход не дал ни одного файла — проверяем канал эталонным файлом.
        # Отличаем «эти файлы не отдаются» от «сеть легла».
        if stats["ok"] == 0 and _dead:
            if await canary_ok(disk):
                logging.info("Канарейка прошла: канал жив, мёртвые файлы остаются в кэше")
            else:
                removed = purge_unconfirmed()
                logging.warning(f"Канарейка не прошла: похоже на общий обрыв сети — "
                                f"снято {removed} неподтверждённых меток")
        logging.info("=" * 60)
        logging.info(f"ИТОГО: папок={scanned} {stats['ok']} успешно, {stats['skipped']} пропущено, "
                     f"{stats['dead']} мёртвых (пропущены из кэша), {failed_count} ошибок")
        return scanned, stats["ok"], failed_count


async def main():
    setup_environment()
    log_file = setup_logging()
    logging.info(f"Лог-файл: {log_file}")
    load_dead()
    load_canary()
    if _state.get("canary"):
        logging.info(f"Эталонный файл для проверки канала: {_state['canary']['path']}")

    cfg = load_config()
    delay_min = float(cfg.get("repeat_delay_minutes", 0) or 0)

    if delay_min <= 0:
        # Однопроходный режим (как раньше): прошли круг и вышли.
        await run_pass()
        return

    delay = delay_min * 60
    logging.info(f"Цикловой режим: пауза между проходами {delay_min:.0f} мин "
                 f"(с нарастанием до {MAX_IDLE_SLEEP / 60:.0f} мин при пустых проходах)")
    asyncio.create_task(hang_guard())

    pass_no = 0
    empty_streak = 0
    while True:
        pass_no += 1
        t0 = time.time()
        _state["active"] = True
        touch_activity()
        try:
            scanned, ok, failed = await run_pass()
        except Exception as e:
            logging.exception(f"[ПРОХОД {pass_no}] аварийно прерван: {e}")
            scanned = ok = failed = 0
        finally:
            _state["active"] = False
        dur = time.time() - t0

        if ok > 0:
            empty_streak = 0
        else:
            empty_streak += 1
        # Пустые проходы: ждём дольше, но не бесконечно — небоевые окна переждать.
        wait = delay * (2 ** (empty_streak - 1)) if empty_streak else delay
        wait = min(wait, MAX_IDLE_SLEEP)

        logging.info(f"[ПРОХОД {pass_no}] ok={ok} fail={failed} за {dur / 60:.1f} мин; "
                     f"следующий через {wait / 60:.0f} мин (пустых подряд={empty_streak})")
        await asyncio.sleep(wait)


if __name__ == "__main__":
    asyncio.run(main())
