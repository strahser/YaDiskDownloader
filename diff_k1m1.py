# -*- coding: utf-8 -*-
"""Scoped diff: remote disk:/К1 + disk:/М1 vs local. Missing tree + full trees."""
import asyncio, sys, os
from pathlib import Path
from collections import defaultdict
sys.path.insert(0, str(Path(__file__).parent / 'src'))
from main import YDiskClient, yd_to_local
import aiohttp, yaml

ROOTS = ['disk:/К1', 'disk:/М1']

async def t():
    cfg = yaml.safe_load(open('config.yaml', encoding='utf-8'))
    dl = Path(cfg['download_dir'])
    remote = {}
    remote_dirs = set()
    unlisted = []
    async with aiohttp.ClientSession() as s:
        d = YDiskClient(cfg['token'], s)
        async def safe_list(p):
            try:
                return await d.list_dir(p)
            except Exception as e:
                unlisted.append(p)
                return []
        queue = list(ROOTS)
        seen = set(queue)
        n = 0
        while queue:
            batch = queue[:20]
            queue = queue[20:]
            res = await asyncio.gather(*[safe_list(p) for p in batch])
            n += len(batch)
            if n % 200 == 0:
                print(f'scanned dirs={n} files={len(remote)} queue={len(queue)}', flush=True)
            for items in res:
                for it in items:
                    p = it.get('path')
                    if it.get('type') == 'dir':
                        remote_dirs.add(p)
                        if p not in seen:
                            seen.add(p)
                            queue.append(p)
                    else:
                        lp = yd_to_local(p, dl)
                        rel = str(lp.relative_to(dl)).lower()
                        remote[rel] = (p, it.get('size') or 0)
    print(f'DONE remote files={len(remote)} dirs={len(remote_dirs)} unlisted={len(unlisted)}', flush=True)

    local = {}
    for dp, dn, fn in os.walk(dl):
        for f in fn:
            p = os.path.join(dp, f)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            rel = os.path.relpath(p, dl).lower()
            if rel.startswith(('к1\\', 'к1/', 'м1\\', 'м1/')):
                local[rel] = sz

    missing, mismatch = [], []
    for rel, (rp, rs) in remote.items():
        if rel not in local:
            missing.append((rp, rs))
        elif local[rel] != rs:
            mismatch.append((rp, rs, local[rel]))
    miss_b = sum(s for _, s in missing)

    # 1) missing tree
    L = [f'REMOTE files={len(remote)}  LOCAL(scoped) files={len(local)}',
         f'MISSING files={len(missing)} bytes={miss_b} ({miss_b/1024**3:.2f} GB)',
         f'SIZE-MISMATCH files={len(mismatch)}',
         f'UNLISTED dirs={len(unlisted)}', '',
         '=== MISSING TREE ===']
    bydir = defaultdict(list)
    for rp, rs in missing:
        dd = rp.rsplit('/', 1)[0]
        bydir[dd].append((rp.rsplit('/', 1)[-1], rs))
    for dd in sorted(bydir):
        L.append(f'{dd}/')
        for name, rs in sorted(bydir[dd]):
            L.append(f'    {name}  [{rs} B]')
    if mismatch:
        L.append('')
        L.append('=== SIZE MISMATCH (remote vs local) ===')
        for rp, rs, ls in sorted(mismatch):
            L.append(f'{rp} remote={rs} local={ls}')
    if unlisted:
        L.append('')
        L.append('=== UNLISTED DIRS (API 500 — проверить вручную) ===')
        for p in sorted(unlisted):
            L.append(p)
    Path('diff_К1М1.txt').write_text('\n'.join(L), encoding='utf-8')

    # 2) full remote trees
    for root in ROOTS:
        name = root.split('/')[-1]
        items = sorted([(rp, rs) for rel, (rp, rs) in remote.items()
                        if rp == root or rp.startswith(root + '/')])
        TL = [f'# {root} — files={len(items)}', '']
        cur = None
        for rp, rs in items:
            dd = rp.rsplit('/', 1)[0]
            if dd != cur:
                cur = dd
                TL.append(f'{dd}/')
            TL.append(f'    {rp.rsplit("/", 1)[-1]}  [{rs} B]')
        Path(f'tree-{name}.txt').write_text('\n'.join(TL), encoding='utf-8')
    print(f'REPORT missing={len(missing)} ({miss_b/1024**3:.2f} GB) mismatch={len(mismatch)} unlisted={len(unlisted)}', flush=True)

asyncio.run(t())
