# -*- coding: utf-8 -*-
"""Сводка «что осталось скачать» после раскладки файлов из архивов.

Читает К1_не_скачано.txt / М1_не_скачано.txt (отчёт list_missing.py) и для
каждого недостающего файла ищет рядом локальную версию с суффиксом _recover.

Пишет осталось_после_раскладки.txt — по одному блоку на комплекс:
  1) нет вовсе (Яндекс не отдаёт / ещё не пробовали);
  2) есть локально, но размер не совпал (партиал, докачка через Range).
В каждой строке пометка, есть ли версия из архива.
"""
import os
import re
from pathlib import Path

BASE = Path(__file__).parent
LOCAL = {'К1': Path(r'd:\StrakhovYandexDisk\К1'),
         'М1': Path(r'd:\StrakhovYandexDisk\М1')}
REPORTS = {'К1': BASE / 'К1_не_скачано.txt', 'М1': BASE / 'М1_не_скачано.txt'}

ROW = re.compile(r'^(\[!\]|   )\s+(\S+ ?\S*)\s+(?:\(локально (\S+ ?\S*)\)\s+)?(.+)$')


def human(b: int) -> str:
    if b >= 1024 ** 3:
        return f'{b / 1024 ** 3:.2f} ГБ'
    if b >= 1024 ** 2:
        return f'{b / 1024 ** 2:.1f} МБ'
    if b >= 1024:
        return f'{b / 1024:.0f} КБ'
    return f'{b} Б'


def to_bytes(s: str) -> int:
    parts = s.split()
    v = float(parts[0].replace(',', '.'))
    unit = parts[-1] if len(parts) > 1 else 'Б'
    return int(v * {'ГБ': 1024 ** 3, 'МБ': 1024 ** 2, 'КБ': 1024}.get(unit, 1))


def parse_report(path: Path):
    """-> (missing, mismatched), элемент = (dead, size_str, rel, local_size|None)."""
    missing, mismatched = [], []
    cur = missing
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        if 'размер не совпадает' in line:
            cur = mismatched
            continue
        if not line.startswith(('[!]', '   ')):
            continue
        dead = line.startswith('[!]')
        rest = line[3:].strip()
        m = re.match(r'^(\S+ ?\S*)\s+(.*)$', rest)
        if not m:
            continue
        size, tail = m.group(1), m.group(2)
        local_size = None
        lm = re.match(r'^\(локально (\S+ ?\S*)\)\s+(.*)$', tail)
        if lm:
            local_size, tail = lm.group(1), lm.group(2)
        try:
            to_bytes(size)
        except ValueError:
            continue
        cur.append((dead, size, tail, local_size))
    return missing, mismatched


def recover_near(root: Path, rel: str):
    """Есть ли рядом файл с _recover вместо ожидаемого имени."""
    p = root / rel.replace('/', os.sep)
    d = p.parent
    if not d.exists():
        return None
    stem = p.stem
    try:
        names = os.listdir(d)
    except OSError:
        return None
    for n in names:
        low = n.lower()
        if '_recover' in low and low.split('_recover')[0] == (stem.lower()):
            sp = d / n
            try:
                return sp, sp.stat().st_size
            except OSError:
                return None
    return None


def main():
    out = ['Что осталось скачать после раскладки из архивов',
           'Сформировано: ' + __import__('time').strftime('%d.%m.%Y %H:%M'),
           '',
           'Обозначения:',
           '  [!] — Яндекс уже не отдал файл (таймаут), повтор при истечении кэша;',
           '  (архив) — рядом лежит версия из архива с суффиксом _recover,',
           '            оригинал всё равно ждём с Диска;',
           '  (локально X) — файл есть, но недокачан до дискового размера.',
           '']

    for comp in ('К1', 'М1'):
        root = LOCAL[comp]
        missing, mismatched = parse_report(REPORTS[comp])
        with_arch = sum(1 for d, s, rel, ls in missing if recover_near(root, rel))
        mis_arch = sum(1 for d, s, rel, ls in mismatched if recover_near(root, rel))
        mb = sum(to_bytes(s) for d, s, rel, ls in missing)
        out.append('=' * 72)
        out.append(f'{comp}: нет {len(missing)} файлов ({human(mb)}), '
                   f'из них с версией из архива: {with_arch}')
        out.append(f'     недокачано (размер не совпал): {len(mismatched)}, '
                   f'из них с версией из архива: {mis_arch}')
        out.append('=' * 72)
        out.append('')
        out.append(f'--- {comp}: файлов нет вовсе ({len(missing)}) ---')
        for dead, size, rel, _ in sorted(missing, key=lambda r: r[2].lower()):
            arch = recover_near(root, rel)
            mark = f'[!] {size:>9}' if dead else f'    {size:>9}'
            tag = ' (архив)' if arch else ''
            out.append(f'{mark}  {rel}{tag}')
        out.append('')
        if mismatched:
            out.append(f'--- {comp}: есть локально, но недокачано ({len(mismatched)}) ---')
            for dead, size, rel, ls in sorted(mismatched, key=lambda r: r[2].lower()):
                mark = '[!]' if dead else '   '
                out.append(f'{mark} {size:>9} (локально {ls})  {rel}')
            out.append('')

    dst = BASE / 'осталось_после_раскладки.txt'
    dst.write_text('\n'.join(out), encoding='utf-8-sig')
    print('written', dst, flush=True)


if __name__ == '__main__':
    main()
