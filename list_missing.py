# -*- coding: utf-8 -*-
"""Список файлов, которых нет локально: свежее дерево Диска (API) vs локальная папка.

Примеры:

    python list_missing.py --root "disk:/К1" --local "d:\\StrakhovYandexDisk\\К1" \
        --dead logs/dead_config-k1.json --out К1_не_скачано

    python list_missing.py --root "disk:/М1/РД_Основной комплект" \
        --root "disk:/М1/Стадия П ВЗУ ТСГ" ... --local ... --out М1_не_скачано

Пишет два файла: <out>.txt (для чтения) и <out>.csv (для Excel, UTF-8 BOM).
Отмечает файлы, известные как «мёртвые» (Яндекс их не отдаёт) — по кэшу
logs/dead_config-*.json.
"""
import argparse
import asyncio
import csv
import io
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import unquote

import aiohttp
import yaml

BASE = Path(__file__).parent
API = 'https://cloud-api.yandex.net/v1/disk/resources'
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


def sanitize(rel: str) -> str:
    """То же преобразование имени, что и в main.yd_to_local."""
    parts = unquote(rel).lstrip('/').split('/')
    return '/'.join(re.sub(r'[<>:"/\\|?*]', '_', p).rstrip('. ') for p in parts)


async def list_dir(session, sem, path: str) -> list:
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
    files, queue = [], [root]
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


def local_index(root: Path) -> dict:
    idx = {}
    if not root.exists():
        return idx
    for dp, _, fn in os.walk(root):
        for f in fn:
            p = Path(dp) / f
            rel = os.path.relpath(p, root).replace('\\', '/')
            try:
                idx[rel.lower()] = p.stat().st_size
            except OSError:
                idx[rel.lower()] = -1
    return idx


def load_dead(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', action='append', required=True,
                    help='путь на Диске, можно несколько раз')
    ap.add_argument('--base', default=None,
                    help='диск-путь, соответствующий локальной папке '
                         '(по умолчанию — общий корень всех --root)')
    ap.add_argument('--local', required=True, help='локальная папка для сравнения')
    ap.add_argument('--dead', default=None, help='logs/dead_config-*.json')
    ap.add_argument('--out', required=True, help='префикс имени выходных файлов')
    ap.add_argument('--title', default=None, help='заголовок отчёта')
    args = ap.parse_args()

    cfg = yaml.safe_load((BASE / 'config.yaml').read_text(encoding='utf-8'))
    token = cfg['token']
    local_root = Path(args.local)

    sem = asyncio.Semaphore(8)
    conn = aiohttp.TCPConnector(limit=16)
    async with aiohttp.ClientSession(
            connector=conn,
            headers={'Authorization': f'OAuth {token}'},
            timeout=aiohttp.ClientTimeout(total=120, sock_connect=15, sock_read=60)) as s:
        results = await asyncio.gather(*(walk(s, sem, r) for r in args.root))

    local = local_index(local_root)
    if not local:
        raise SystemExit(f'Локальная папка пуста или не найдена: {local_root}')
    dead = load_dead(BASE / args.dead) if args.dead else {}
    now = time.time()
    dead_now = {k.lower() for k, v in dead.items()
                if float(v.get('t', 0)) + DEAD_TTL_HOURS * 3600 > now}

    missing, mismatched = [], []
    total_remote = total_bytes = 0
    groups = {}
    base = (args.base or args.root[0]).rstrip('/')
    for files in results:
        for dpath, size in files:
            total_remote += 1
            total_bytes += size
            rel = dpath[len(base) + 1:] if dpath.startswith(base + '/') else dpath
            group = rel.split('/')[0] if '/' in rel else '(корень)'
            groups[group] = groups.get(group, 0) + 1
            key = sanitize(rel).lower()
            if key not in local:
                missing.append((group, rel, size, dpath.lower() in dead_now))
            elif local[key] != size:
                mismatched.append((group, rel, size, local[key], dpath.lower() in dead_now))

    miss_bytes = sum(m[2] for m in missing)
    known = sum(1 for m in missing if m[3])

    title = args.title or 'Файлы, которых нет локально'
    out = [title,
           f'Проверка: {time.strftime("%d.%m.%Y %H:%M")}  (дерево Диска — актуальное, через API)',
           f'Корни: {", ".join(args.root)}',
           f'Локальная папка: {local_root}',
           '',
           f'Всего на Диске: {total_remote} файлов, {gb(total_bytes)}',
           f'Нет локально: {len(missing)} файлов, {gb(miss_bytes)}',
           f'Из них помечены как «Яндекс не отдаёт»: {known}',
           f'Есть локально, но размер отличается: {len(mismatched)}',
           '',
           'Пометка [!] — файл уже пробовали качать, сервер отдаёт таймаут.',
           '']

    order = sorted({m[0] for m in missing}, key=lambda g: (-sum(1 for m in missing if m[0] == g), g))
    for group in order:
        rows = [m for m in missing if m[0] == group]
        rows.sort(key=lambda m: m[1].lower())
        out.append('=' * 70)
        out.append(f'{group} — {len(rows)} файлов, {gb(sum(r[2] for r in rows))}')
        out.append('=' * 70)
        for _, rel, size, is_dead in rows:
            out.append(f'{"[!]" if is_dead else "   "} {human(size):>9}  {rel}')
        out.append('')

    if mismatched:
        out.append('=' * 70)
        out.append(f'Есть локально, но размер не совпадает — {len(mismatched)}')
        out.append('=' * 70)
        for _, rel, size, lsize, is_dead in sorted(mismatched, key=lambda m: m[1].lower()):
            ls = human(lsize) if lsize >= 0 else '?'
            out.append(f'{"[!]" if is_dead else "   "} {human(size):>9} (локально {ls})  {rel}')
        out.append('')

    (BASE / f'{args.out}.txt').write_text('\n'.join(out), encoding='utf-8-sig')

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';', lineterminator='\r\n')
    w.writerow(['Папка', 'Путь на Диске', 'Размер', 'Размер (байт)', 'Яндекс не отдаёт'])
    prefix = base
    for group, rel, size, is_dead in sorted(missing, key=lambda m: (m[0], m[1].lower())):
        w.writerow([group, f'{prefix}/{rel}', human(size), size, 'да' if is_dead else ''])
    (BASE / f'{args.out}.csv').write_text(buf.getvalue(), encoding='utf-8-sig')

    print(f'remote={total_remote} bytes={total_bytes}')
    print(f'missing={len(missing)} bytes={miss_bytes} known_dead={known}')
    print(f'mismatched={len(mismatched)}')


if __name__ == '__main__':
    asyncio.run(main())
