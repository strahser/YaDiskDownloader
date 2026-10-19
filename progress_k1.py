# -*- coding: utf-8 -*-
"""Fresh К1 coverage vs the Oct-16 tree snapshot (no Yandex API calls)."""
import os, re
from pathlib import Path

BASE = Path(__file__).parent
LOCAL_ROOT = Path(r'd:\StrakhovYandexDisk')
LOCAL = LOCAL_ROOT / 'К1'


def gb(b):
    return f'{b/1024**3:.2f} GB'


def norm(s):
    return s.strip().lower()


remote = []
cur = None
for line in (BASE / 'tree-К1.txt').read_text(encoding='utf-8').splitlines():
    if line.startswith('disk:/К1'):
        cur = line.rstrip('/')
    elif line.startswith('disk:/'):
        cur = None
    elif cur and line.startswith('    '):
        m = re.match(r'\s+(.*)\s+\[(\d+) B\]$', line)
        if m:
            remote.append((cur, m.group(1), int(m.group(2))))

local = set()
if LOCAL.exists():
    for dp, dn, fn in os.walk(LOCAL):
        for f in fn:
            local.add(norm(os.path.relpath(Path(dp) / f, LOCAL).replace('\\', '/')))

rn = len(remote)
rb = sum(s for _, _, s in remote)
present = miss = miss_b = 0
for folder, name, size in remote:
    rel = folder[len('disk:/К1/'):] + '/' + name if folder != 'disk:/К1' else name
    if norm(rel) in local:
        present += 1
    else:
        miss += 1
        miss_b += size

out = [f'К1: на Диске={rn} ({gb(rb)}), локально={present} ({100.0*present/rn:.1f}%), '
       f'нет={miss} ({gb(miss_b)})',
       f'локальных файлов в папке К1 всего={len(local)} (включая лишние/переименованные)']
(BASE / 'analysis_К1_fresh.txt').write_text('\n'.join(out), encoding='utf-8')
print('\n'.join(out))
