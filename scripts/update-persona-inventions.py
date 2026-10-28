#!/usr/bin/env python3
"""Заменить только раздел об изобретениях, сохранив внешний образ и его копию."""

import argparse
import os
from pathlib import Path
import stat
import tempfile

START = 'Рассказ об изобретении — два хода.'
END = '\nОтвечай по-русски'
TEMPLATE = Path(__file__).resolve().parents[1] / 'config/persona.example.md'


def bounds(text):
    if text.count(START) != 1:
        raise ValueError('Нужен ровно один прежний раздел об изобретениях; файл не изменён.')
    start = text.index(START)
    end = text.find(END, start)
    if end < 0:
        raise ValueError('Не найдена граница раздела перед «Отвечай по-русски»; файл не изменён.')
    return start, end


def update(path, template=TEMPLATE, *, check=False):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Нужен обычный файл образа, без символической ссылки.')
    payload = path.read_bytes()
    if len(payload) > 8192 or b'\0' in payload:
        raise ValueError('Нужен UTF-8 образ до 8192 байт без NUL; файл не изменён.')
    original = payload.decode('utf-8')
    replacement = template.read_text(encoding='utf-8')
    start, end = bounds(original)
    new_start, new_end = bounds(replacement)
    result = (original[:start] + replacement[new_start:new_end] + original[end:]).encode('utf-8')
    if len(result) > 8192:
        raise ValueError('Новый образ превышает 8192 байт; файл не изменён.')
    if result == payload:
        return 'unchanged', None, len(result)
    if check:
        return 'checked', None, len(result)
    # Сначала полная защищённая копия. Новые данные ставятся атомарно.
    backup_fd, backup_name = tempfile.mkstemp(prefix=path.name + '.before-inventions.', dir=path.parent)
    with os.fdopen(backup_fd, 'wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    staged_fd, staged_name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(staged_fd, 'wb') as stream:
            stream.write(result)
            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            stream.flush()
            os.fsync(stream.fileno())
        current = path.lstat()
        def signature(value):
            return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
                    value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if signature(current) != signature(info) or path.read_bytes() != payload:
            raise ValueError('Образ изменён другим процессом; повторите проверку. Ваша правка сохранена.')
        os.replace(staged_name, path)
    finally:
        Path(staged_name).unlink(missing_ok=True)
    return 'updated', Path(backup_name), len(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('persona', type=Path)
    parser.add_argument('--check', action='store_true', help='Только проверить замену, без записи')
    args = parser.parse_args()
    try:
        status, backup, size = update(args.persona, check=args.check)
    except (ValueError, UnicodeError) as exc:
        parser.exit(2, str(exc) + '\n')
    except OSError:
        parser.exit(2, 'Не удалось прочитать или записать образ; проверьте путь, права и место на диске.\n')
    print({'unchanged': 'Раздел уже обновлён.', 'checked': 'Замена проверена; файл пока не изменён.',
           'updated': 'Обновлён только раздел об изобретениях.'}[status])
    print('Размер образа:', size, 'байт.')
    if backup:
        print('Прежний образ сохранён:', backup)


if __name__ == '__main__':
    main()
