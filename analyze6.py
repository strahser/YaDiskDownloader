# -*- coding: utf-8 -*-
"""Per-folder analysis for 6 М1 subfolders from tree + diff + local."""
import os, re
from pathlib import Path
from collections import defaultdict

BASE = Path(__file__).parent
TARGETS = ['РД_Основной комплект', 'РД_Этап 2_ИСЕТ', 'Стадия П ВЗУ ТСГ',
           'Стадия П Делаваль все этапы', 'Стадия П изменения ИСЕТ этапы 2-3',
           'Стадия П изменения Родин']
RPREFIX = {t: f'disk:/М1/{t}' for t in TARGETS}
LOCAL = Path(r'd:\StrakhovYandexDisk\М1')

def gb(b): return f'{b/1024**3:.2f} GB'

# 1) remote from tree-М1.txt
rem_files = defaultdict(list)  # target -> [(name, size)]
rem_unmatched = 0
cur = None
cur_t = None
for line in (BASE / 'tree-М1.txt').read_text(encoding='utf-8').splitlines():
    if line.startswith('disk:/'):
        cur = line.rstrip('/')
        cur_t = next((t for t in TARGETS if cur == RPREFIX[t] or cur.startswith(RPREFIX[t] + '/')), None)
    elif line.startswith('    ') and cur_t:
        m = re.match(r'\s+(.*)\s+\[(\d+) B\]', line)
        if m:
            rem_files[cur_t].append((m.group(1), int(m.group(2))))

# 2) local walk
loc = {}
for t in TARGETS:
    p = LOCAL / t
    n = b = 0
    if p.exists():
        for dp, dn, fn in os.walk(p):
            for f in fn:
                try:
                    b += os.path.getsize(os.path.join(dp, f)); n += 1
                except OSError:
                    pass
    loc[t] = (n, b)

# 3) missing from diff_К1М1.txt (missing section only)
miss = defaultdict(list)
in_miss = False
cur = None
cur_t = None
for line in (BASE / 'diff_К1М1.txt').read_text(encoding='utf-8').splitlines():
    if line.startswith('=== MISSING TREE'):
        in_miss = True; continue
    if line.startswith('=== SIZE MISMATCH'):
        break
    if not in_miss:
        continue
    if not line.startswith('    '):
        cur = line.rstrip('/')
        cur_t = next((t for t in TARGETS if cur == RPREFIX[t] or cur.startswith(RPREFIX[t] + '/')), None)
    elif cur_t:
        m = re.match(r'\s+(.*)\s+\[(\d+) B\]', line)
        if m:
            miss[cur_t].append((m.group(1), int(m.group(2))))

L = ['Папка | Файлов на Диске | Объём Диск | Локально файлов | Локально объём | Нет локально | Нет объём | % готово(шт)']
tot = [0, 0, 0, 0, 0, 0]
for t in TARGETS:
    rn = len(rem_files[t]); rb = sum(s for _, s in rem_files[t])
    ln, lb = loc[t]
    mn = len(miss[t]); mb = sum(s for _, s in miss[t])
    pct = (100.0 * (rn - mn) / rn) if rn else 100.0
    L.append(f'{t} | {rn} | {gb(rb)} | {ln} | {gb(lb)} | {mn} | {gb(mb)} | {pct:.1f}%')
    for i, v in enumerate([rn, rb, ln, lb, mn, mb]): tot[i] += v
L.append(f'ИТОГО 6 папок | {tot[0]} | {gb(tot[1])} | {tot[2]} | {gb(tot[3])} | {tot[4]} | {gb(tot[5])} | {100.0*(tot[0]-tot[4])/tot[0]:.1f}%')
L.append('')
L.append('Детализация отсутствующих — в diff_К1М1.txt (раздел MISSING TREE), полные деревья — tree-М1.txt.')
out = BASE / 'analysis_М1.txt'
out.write_text('\n'.join(L), encoding='utf-8')
print('\n'.join(L), flush=True)
