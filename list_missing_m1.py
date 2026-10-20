# -*- coding: utf-8 -*-
"""Список файлов М1, которых нет локально.

Берёт актуальное дерево шести приоритетных папок с Диска (API), сравнивает
с локальной папкой d:\\StrakhovYandexDisk\\М1 и пишет два файла:

    M1_не_скачано.txt  — для чтения, сгруппировано по папкам
    M1_не_скачано.csv  — для Excel (UTF-8 BOM, разделитель ';')

Дополнительно отмечает файлы, которые уже известны как «мёртвые»
(Яндекс их не отдаёт) — по кэшу logs\\dead_config-m1.json.
"""
import asyncio
import csv
import io
import json
import os
from pathlib import Path

import re
from urllib.parse import unquote

import aiohttp
import yaml

BASE = Path(__file__).parent
CFG = yaml.safe_load((BASE / 'config.yaml').read_text(encoding='utf-8'))
TOKEN = CFG['token']
API = 'https://cloud-api.yandex.net/v1/disk/resources'
LOCAL_M1 = Path(r'd:\StrakhovYandexDisk\М1')
DEAD_PATH = BASE / 'logs' / 'dead_config-m1.json'

TARGETS = ['РД_Основной комплект', 'РД_Этап 2_ИСЕТ', 'Стадия П ВЗУ ТСГ',
           'Стадия П Делаваль все этапы', 'Стадия П изменения ИСЕТ этапы 2-3',
           'Стадия П изменения Родин']

DEAD_TTL_HOURS = 12.0


def gb(b: int) -> str:
    return f'{b / 1024 ** 3:.2f} ГБ'


def human(b: int) -> str:
    if b >= 1024 ** 3:
        return f'{b / 1024 ** 3:.2f} ГБ'
    if b >= 1024 ** 2:
        return f'{b / 1024 ** 2:.1f} МБ'
    if b >= 1024:
        return f'{b / 1024:.0f} КБ'
    return f'{b} Б'


async def list_dir(session: aiohttp.ClientSession, sem: asyncio.Semaphore,
                   path: str) -> list:
    out, offset = [], 0
    while True:
        params = {
            'path': path,
            'limit': '1000',
            'offset': str(offset),
            'fields': '_embedded.items.path,_embedded.items.name,'
                      '_embedded.items.size,_embedded.items.type,_embedded.total',
        }
        async with sem:
            for attempt in range(3):
                try:
                    async with session.get(API, params=params) as r:
                        if r.status == 429:
                            await asyncio.sleep(2 * (attempt + 1))
                            continue
                        r.raise_for_status()
                        data = await r.json()
                        break
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1.5 * (attempt + 1))
        emb = data.get('_embedded') or {}
        items = emb.get('items') or []
        out.extend(items)
        offset += len(items)
        if offset >= int(emb.get('total') or 0) or not items:
            break
    return out


async def walk(session, sem, root: str) -> list:
    """Рекурсивный обход, возвращает [(disk_path, size)]."""
    files = []
    queue = [root]
    while queue:
        batch, queue = queue, []
        results = await asyncio.gather(*(list_dir(session, sem, p) for p in batch))
        for items in results:
            for it in items:
                if it.get('type') == 'dir':
                    queue.append(it['path'])
                else:
                    files.append((it['path'], int(it.get('size') or 0)))
    return files


def sanitize(rel: str) -> str:
    """То же преобразование имени, что и в main.yd_to_local."""
    parts = unquote(rel).lstrip('/').split('/')
    return '/'.join(re.sub(r'[<>:"/\\|?*]', '_', p).rstrip('. ') for p in parts)


def local_index() -> dict:
    idx = {}
    if not LOCAL_M1.exists():
        return idx
    for dp, _, fn in os.walk(LOCAL_M1):
        for f in fn:
            p = Path(dp) / f
            rel = os.path.relpath(p, LOCAL_M1).replace('\\', '/')
            try:
                idx[rel.lower()] = p.stat().st_size
            except OSError:
                idx[rel.lower()] = -1
    return idx


def load_dead() -> dict:
    try:
        return json.loads(DEAD_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {}


async def main():
    import time
    sem = asyncio.Semaphore(8)
    conn = aiohttp.TCPConnector(limit=16)
    async with aiohttp.ClientSession(
            connector=conn,
            headers={'Authorization': f'OAuth {TOKEN}'},
            timeout=aiohttp.ClientTimeout(total=120, sock_connect=15, sock_read=60)) as s:
        # диск доступен? заодно получаем общий объём
        results = await asyncio.gather(
            *(walk(s, sem, f'disk:/М1/{t}') for t in TARGETS))

    local = local_index()
    dead = load_dead()
    now = time.time()
    dead_now = {k.lower() for k, v in dead.items()
                if float(v.get('t', 0)) + DEAD_TTL_HOURS * 3600 > now}

    missing = []      # (target, relpath, size, known_dead)
    mismatched = []   # (target, relpath, size, local_size)
    total_remote = total_bytes = 0
    for target, files in zip(TARGETS, results):
        for dpath, size in files:
            total_remote += 1
            total_bytes += size
            rel = dpath[len('disk:/М1/'):]
            key = sanitize(rel).lower()
            if key not in local:
                missing.append((target, rel, size, dpath.lower() in dead_now))
            elif local[key] != size:
                mismatched.append((target, rel, size, local[key], dpath.lower() in dead_now))

    miss_bytes = sum(m[2] for m in missing)

    # ---------- TXT ----------
    out = []
    out.append('Файлы М1, которых нет локально')
    out.append(f'Проверка: {time.strftime("%d.%m.%Y %H:%M")}  '
               f'(дерево Диска — актуальное, через API)')
    out.append(f'Локальная папка: {LOCAL_M1}')
    out.append('')
    out.append(f'Всего на Диске в шести приоритетных папках: {total_remote} файлов, '
               f'{gb(total_bytes)}')
    out.append(f'Нет локально: {len(missing)} файлов, {gb(miss_bytes)}')
    known = sum(1 for m in missing if m[3])
    out.append(f'Из них помечены как «Яндекс не отдаёт»: {known}')
    out.append(f'Есть локально, но размер отличается: {len(mismatched)}')
    out.append('')
    out.append('Пометка [!] — файл уже пробовали качать, сервер отдаёт таймаут')
    out.append('(лежит в кэше мёртвых logs\\dead_config-m1.json).')
    out.append('')
    for target in TARGETS:
        rows = [m for m in missing if m[0] == target]
        if not rows:
            continue
        rows.sort(key=lambda m: m[1].lower())
        out.append('=' * 70)
        out.append(f'{target} — {len(rows)} файлов, {gb(sum(r[2] for r in rows))}')
        out.append('=' * 70)
        for _, rel, size, is_dead in rows:
            mark = '[!]' if is_dead else '   '
            out.append(f'{mark} {human(size):>9}  {rel}')
        out.append('')

    if mismatched:
        out.append('=' * 70)
        out.append(f'Есть локально, но размер не совпадает — {len(mismatched)}')
        out.append('=' * 70)
        for target, rel, size, lsize, is_dead in sorted(mismatched, key=lambda m: m[1].lower()):
            mark = '[!]' if is_dead else '   '
            out.append(f'{mark} {human(size):>9} (локально {human(lsize) if lsize >= 0 else "?"})  {rel}')
        out.append('')

    (BASE / 'M1_не_скачано.txt').write_text('\n'.join(out), encoding='utf-8-sig')

    # ---------- CSV ----------
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';', lineterminator='\r\n')
    w.writerow(['Папка', 'Путь на Диске', 'Размер', 'Размер (байт)', 'Яндекс не отдаёт'])
    for target, rel, size, is_dead in sorted(missing, key=lambda m: (m[0], m[1].lower())):
        w.writerow([target, 'disk:/М1/' + rel, human(size), size, 'да' if is_dead else ''])
    (BASE / 'M1_не_скачано.csv').write_text(buf.getvalue(), encoding='utf-8-sig')

    print(f'remote={total_remote} bytes={total_bytes}')
    print(f'missing={len(missing)} bytes={miss_bytes} known_dead={known}')
    print(f'mismatched={len(mismatched)}')


if __name__ == '__main__':
    asyncio.run(main())
