# -*- coding: utf-8 -*-
"""Проверка «не найденных нигде» файлов по реестру.

Для каждого имени ищет:
  1) точное совпадение (без учёта регистра) в живом дереве Диска и локально;
  2) близкие по имени файлы (похожесть >= 0.72) — вдруг лежит под другим именем.

Живое дерево берётся через API (как в list_missing.py).
"""
import asyncio
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

import aiohttp
import yaml

BASE = Path(__file__).parent
API = 'https://cloud-api.yandex.net/v1/disk/resources'
LOCAL = {'М1': Path(r'd:\StrakhovYandexDisk\М1'),
         'К1': Path(r'd:\StrakhovYandexDisk\К1')}
ROOTS = {'М1': 'disk:/М1', 'К1': 'disk:/К1'}

# 12 позиций из отчёта о раскладке: не нашлись ни на Диске, ни в архивах
TARGETS = [
    ('М1', 'ВВ-17.К-1.1,1.2,1.3,1.4-ВК1.pdf'),
    ('М1', 'ВВ-17.К1-2.5-АР изм.pdf'),
    ('М1', 'ВВ-17-4.5-АР1-01-Rev1'),
    ('М1', 'ВВ-17-3.1-ЭОМ_изм.2 04.03.2024 .pdf'),
    ('М1', '005-2022-НВК v6.pdf'),
    ('М1', 'ВВ-17-2.1...2.4-АПС1 250703.pdf'),
    ('М1', 'сканы'),
    ('К1', '308.17.02 Канал навозоудаления.pdf'),
    ('К1', '308.17.02-ТК.pdf'),
    ('К1', '308.17.02-24-КЖ0_изм.2_17.11.21.pdf'),
    ('К1', '306.17-2-ЭОМ_изм.1_22.11.2022.pdf'),
    ('К1', '307.17-АР.pdf'),
]


def norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r'\.(pdf|dwg|docx?|xlsx?|msg|zip|rvt|jpg|png|txt|log)$', '', s)
    s = re.sub(r'[^0-9a-zа-яё]+', '', s)
    return s


async def list_dir(session, sem, path):
    out, offset = [], 0
    while True:
        params = {'path': path, 'limit': '1000', 'offset': str(offset),
                  'fields': '_embedded.items.path,_embedded.items.name,'
                            '_embedded.items.size,_embedded.items.type,_embedded.total'}
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


async def walk(session, sem, root):
    files, queue = [], [root]
    while queue:
        batch, queue = queue, []
        res = await asyncio.gather(*(list_dir(session, sem, p) for p in batch))
        for items in res:
            for it in items:
                if it.get('type') == 'dir':
                    queue.append(it['path'])
                else:
                    files.append((it['path'], int(it.get('size') or 0)))
    return files


def local_files(root: Path):
    out = []
    if not root.exists():
        return out
    for dp, _, fn in os.walk(root):
        for f in fn:
            out.append(str(Path(dp) / f))
    return out


async def main():
    cfg = yaml.safe_load((BASE / 'config.yaml').read_text(encoding='utf-8'))
    sem = asyncio.Semaphore(8)
    async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=16),
            headers={'Authorization': f"OAuth {cfg['token']}"},
            timeout=aiohttp.ClientTimeout(total=120, sock_connect=15, sock_read=60)) as s:
        disk = {k: await walk(s, sem, ROOTS[k]) for k in ROOTS}

    loc = {k: local_files(v) for k, v in LOCAL.items()}

    lines = [f'Проверка «не найденных нигде» файлов: {time.strftime("%d.%m.%Y %H:%M")}', '']
    for comp, name in TARGETS:
        n = norm(name)
        lines.append(f'== [{comp}] {name}')
        exact_disk = [p for p, _ in disk[comp] if Path(p).name.lower() == name.lower()]
        exact_loc = [p for p in loc[comp] if Path(p).name.lower() == name.lower()]
        lines.append(f'   точное имя на Диске: {len(exact_disk)}'
                     + (f' -> {exact_disk[0]}' if exact_disk else ''))
        lines.append(f'   точное имя локально: {len(exact_loc)}'
                     + (f' -> {exact_loc[0]}' if exact_loc else ''))
        cands = []
        for p, sz in disk[comp]:
            b = Path(p).name
            if b.lower().endswith(('.pdf', '.dwg', '.doc', '.docx', '.xls', '.xlsx',
                                   '.msg', '.zip', '.rvt', '.jpg', '.png', '.sig')):
                r = SequenceMatcher(None, n, norm(b)).ratio()
                if r >= 0.72:
                    cands.append((r, p, sz, 'Диск'))
        for p in loc[comp]:
            b = Path(p).name
            if b.lower().endswith(('.pdf', '.dwg', '.doc', '.docx', '.xls', '.xlsx',
                                   '.msg', '.zip', '.rvt', '.jpg', '.png', '.sig')):
                r = SequenceMatcher(None, n, norm(b)).ratio()
                if r >= 0.72:
                    cands.append((r, p, Path(p).stat().st_size if Path(p).exists() else 0, 'локально'))
        cands.sort(key=lambda t: -t[0])
        seen = set()
        shown = 0
        for r, p, sz, where in cands:
            key = (Path(p).name.lower(), where)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f'   ~{r:.2f} [{where}] {sz} B  {p}')
            shown += 1
            if shown >= 4:
                break
        if not shown:
            lines.append('   похожих файлов нет')
        lines.append('')

    (BASE / 'logs' / 'unfound_check.txt').write_text('\n'.join(lines), encoding='utf-8-sig')
    print('done', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
