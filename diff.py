# -*- coding: utf-8 -*-
"""Diff remote (scoped roots) vs local. Writes diff_report.txt."""
import asyncio, sys, os
from pathlib import Path
from collections import defaultdict
sys.path.insert(0, str(Path(__file__).parent / 'src'))
from main import YDiskClient, yd_to_local
import aiohttp, yaml

async def t():
    cfg = yaml.safe_load(open('config.yaml', encoding='utf-8'))
    dl = Path(cfg['download_dir'])
    roots = cfg.get('roots') or ['disk:/']
    remote = {}  # local_rel_lower -> (remote_path, size)
    async with aiohttp.ClientSession() as s:
        d = YDiskClient(cfg['token'], s)
        queue = list(roots)
        seen = set(queue)
        n_dirs = 0
        unlisted = []
        async def safe_list(p):
            try:
                return await d.list_dir(p)
            except Exception as e:
                unlisted.append((p, str(e)[:120]))
                return []
        while queue:
            batch = queue[:20]
            queue = queue[20:]
            res = await asyncio.gather(*[safe_list(p) for p in batch])
            for items in res:
                for it in items:
                    p = it.get('path')
                    if it.get('type') == 'dir':
                        if p not in seen:
                            seen.add(p)
                            queue.append(p)
                            n_dirs += 1
                    else:
                        lp = yd_to_local(p, dl)
                        rel = str(lp.relative_to(dl)).lower()
                        remote[rel] = (p, it.get('size') or 0)
    print(f'remote files={len(remote)} dirs={n_dirs}', flush=True)

    local = {}
    for dp, dn, fn in os.walk(dl):
        for f in fn:
            p = os.path.join(dp, f)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            rel = os.path.relpath(p, dl).lower()
            # только внутри scoped-корней верхнего уровня
            local[rel] = sz
    print(f'local files={len(local)}', flush=True)

    missing = []   # (remote_path, size)
    mismatch = []  # (remote_path, rsize, lsize)
    for rel, (rp, rs) in remote.items():
        if rel not in local:
            missing.append((rp, rs))
        elif local[rel] != rs:
            mismatch.append((rp, rs, local[rel]))
    extra = [r for r in local if r not in remote]

    miss_b = sum(s for _, s in missing)
    lines = []
    lines.append(f'REMOTE files={len(remote)}  LOCAL files={len(local)}')
    lines.append(f'MISSING files={len(missing)} bytes={miss_b} ({miss_b/1024**3:.2f} GB)')
    lines.append(f'SIZE-MISMATCH files={len(mismatch)}')
    lines.append(f'LOCAL-ONLY (not on disk) files={len(extra)}')
    lines.append(f'UNLISTED dirs (API 500, содержимое неизвестно)={len(unlisted)}')
    lines.append('')
    if unlisted:
        lines.append('=== UNLISTED DIRS (проверить вручную) ===')
        for p, e in sorted(unlisted):
            lines.append(f'{p}  [{e}]')
        lines.append('')
    lines.append('=== MISSING TREE (remote path | size) ===')
    bydir = defaultdict(list)
    for rp, rs in missing:
        dd = rp.rsplit('/', 1)[0] if '/' in rp else '/'
        bydir[dd].append((rp.rsplit('/', 1)[-1], rs))
    for dd in sorted(bydir):
        lines.append(f'{dd}/')
        for name, rs in sorted(bydir[dd]):
            lines.append(f'    {name}  [{rs} B]')
    if mismatch:
        lines.append('')
        lines.append('=== SIZE MISMATCH ===')
        for rp, rs, ls in sorted(mismatch)[:200]:
            lines.append(f'{rp} remote={rs} local={ls}')
    if extra:
        lines.append('')
        lines.append('=== LOCAL-ONLY (first 100) ===')
        for r in sorted(extra)[:100]:
            lines.append(r)
    out = Path('diff_report.txt')
    out.write_text('\n'.join(lines), encoding='utf-8')
    print('\n'.join(lines[:8]), flush=True)
    print(f'full report -> {out.resolve()} ({len(lines)} lines)', flush=True)

asyncio.run(t())
