# -*- coding: utf-8 -*-
"""Fresh progress for the 6 priority М1 folders, without hitting the Yandex API.

Reuses the Oct-16 snapshot tree-М1.txt (what exists on Disk) and the local walk,
then reports: how many of the previously-missing files are now present locally.
"""
import os, re
from pathlib import Path

BASE = Path(__file__).parent
TARGETS = ['РД_Основной комплект', 'РД_Этап 2_ИСЕТ', 'Стадия П ВЗУ ТСГ',
           'Стадия П Делаваль все этапы', 'Стадия П изменения ИСЕТ этапы 2-3',
           'Стадия П изменения Родин']
LOCAL_ROOT = Path(r'd:\StrakhovYandexDisk')
LOCAL_M1 = LOCAL_ROOT / 'М1'


def gb(b):
    return f'{b/1024**3:.2f} GB'


def norm(s):
    return s.strip().lower()


# ---- remote inventory from the snapshot ----
remote = {t: [] for t in TARGETS}      # target -> [(relpath, size)]
cur_t = None
for line in (BASE / 'tree-М1.txt').read_text(encoding='utf-8').splitlines():
    if line.startswith('disk:/'):
        cur = line.rstrip('/')
        cur_t = next((t for t in TARGETS
                      if cur == f'disk:/М1/{t}' or cur.startswith(f'disk:/М1/{t}/')), None)
    elif cur_t and line.startswith('    '):
        m = re.match(r'\s+(.*)\s+\[(\d+) B\]$', line)
        if m:
            remote[cur_t].append((cur, m.group(1), int(m.group(2))))

# ---- local inventory ----
local = set()
local_by_size = {}
for t in TARGETS:
    p = LOCAL_M1 / t
    if not p.exists():
        continue
    for dp, dn, fn in os.walk(p):
        for f in fn:
            fp = Path(dp) / f
            rel = os.path.relpath(fp, LOCAL_M1).replace('\\', '/')
            try:
                sz = fp.stat().st_size
            except OSError:
                continue
            local.add(norm(rel))
            local_by_size.setdefault((norm(rel), sz), True)

rows = []
tot = [0, 0, 0, 0]
for t in TARGETS:
    files = remote[t]
    rn = len(files)
    rb = sum(s for _, _, s in files)
    present = miss = 0
    miss_b = 0
    for folder, name, size in files:
        rel = folder[len('disk:/М1/'):] + '/' + name
        if norm(rel) in local:
            present += 1
        else:
            miss += 1
            miss_b += size
    pct = 100.0 * present / rn if rn else 100.0
    rows.append(f'{t} | {rn} | {gb(rb)} | {present} | {miss} | {gb(miss_b)} | {pct:.1f}%')
    tot[0] += rn; tot[1] += rb; tot[2] += miss; tot[3] += miss_b

out = ['Папка | Файлов на Диске | Объём Диск | Есть локально | Нет локально | Нет объём | % готово(шт)']
out += rows
pct_tot = 100.0 * (tot[0] - tot[2]) / tot[0] if tot[0] else 100.0
out.append(f'ИТОГО 6 папок | {tot[0]} | {gb(tot[1])} | {tot[0]-tot[2]} | {tot[2]} | {gb(tot[3])} | {pct_tot:.1f}%')
out.append('')
out.append('Снимок "Файлов на Диске" — от 16.10.2026 (tree-М1.txt). Локальная проверка — сейчас.')
(BASE / 'analysis_М1_fresh.txt').write_text('\n'.join(out), encoding='utf-8')
print('\n'.join(out))
