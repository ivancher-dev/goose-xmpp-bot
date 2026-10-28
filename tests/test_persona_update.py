"""Обновление раздела сохраняет остальные правки, права и защищённую копию."""

from pathlib import Path
import runpy
import stat

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/update-persona-inventions.py'
update = runpy.run_path(str(SCRIPT))['update']


@pytest.fixture
def persona(tmp_path):
    path = tmp_path / 'persona.md'
    path.write_text('Мои собственные правила до раздела.\n\n'
                    'Рассказ об изобретении — два хода.\nПрежнее правило.\n'
                    '\nОтвечай по-русски. Мои собственные правила после раздела.\n')
    path.chmod(0o640)
    return path


def test_only_section_replaced_and_backup_is_complete_and_private(persona):
    original = persona.read_bytes()
    before = persona.stat()
    status, backup, size = update(persona)
    assert status == 'updated' and backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert persona.read_text().startswith('Мои собственные правила до раздела.\n\n')
    assert persona.read_text().endswith('\nОтвечай по-русски. Мои собственные правила после раздела.\n')
    after = persona.stat()
    assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (before.st_uid, before.st_gid, 0o640)
    assert size == len(persona.read_bytes()) <= 8192
    assert update(persona)[0] == 'unchanged'
    assert len(list(persona.parent.glob('persona.md.before-inventions.*'))) == 1


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'oversized', 'symlink'])
def test_ambiguous_or_unsupported_file_is_not_changed(persona, damage):
    if damage == 'missing':
        persona.write_text('Другая структура, которую нужно сохранить.')
    elif damage == 'duplicate':
        persona.write_text(persona.read_text() + '\nРассказ об изобретении — два хода.')
    elif damage == 'oversized':
        persona.write_bytes(b'x' * 8193)
    else:
        target = persona.with_name('original.md')
        persona.rename(target)
        persona.symlink_to(target)
    original = persona.read_bytes()
    with pytest.raises(ValueError):
        update(persona)
    assert persona.read_bytes() == original
    assert not list(persona.parent.glob('*.before-inventions.*'))


def test_check_does_not_write_files(persona):
    original = persona.read_bytes()
    assert update(persona, check=True)[0] == 'checked'
    assert persona.read_bytes() == original
    assert list(persona.parent.iterdir()) == [persona]


def test_replace_failure_keeps_original_and_backup(persona, monkeypatch):
    original = persona.read_bytes()
    def fail(*args):
        raise OSError('simulated disk failure')
    monkeypatch.setattr('os.replace', fail)
    with pytest.raises(OSError):
        update(persona)
    assert persona.read_bytes() == original
    copies = list(persona.parent.glob('persona.md.before-inventions.*'))
    assert len(copies) == 1 and copies[0].read_bytes() == original
    assert not list(persona.parent.glob('.persona.md.*'))


def test_concurrent_edit_is_preserved(persona, monkeypatch):
    import os
    original = persona.read_bytes()
    fsync = os.fsync
    edited = False
    def edit_during_backup(fd):
        nonlocal edited
        fsync(fd)
        if not edited:
            edited = True
            persona.write_text('Правка владельца в другом редакторе.')
    monkeypatch.setattr('os.fsync', edit_during_backup)
    with pytest.raises(ValueError, match='другим процессом'):
        update(persona)
    assert persona.read_text() == 'Правка владельца в другом редакторе.'
    copies = list(persona.parent.glob('persona.md.before-inventions.*'))
    assert len(copies) == 1 and copies[0].read_bytes() == original
