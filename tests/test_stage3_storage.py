"""Миграция схемы 3 до текущей и атомарность сведений о ручной переписке."""

import json
import sqlite3
import time

import pytest

from goose.cli import doctor
from goose.storage import Database

CHILD = 'pupil1@example.org'


def legacy(config):
    path = config.data_dir / 'goose.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''
          CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE profiles(jid TEXT PRIMARY KEY, grade INTEGER NOT NULL, academic_year TEXT NOT NULL,
            schedule_class_id TEXT NOT NULL, explanation_style TEXT NOT NULL);
          CREATE TABLE omemo(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE seen(key TEXT PRIMARY KEY, expires REAL NOT NULL);
          CREATE TABLE dialogues(jid TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'manual',
            revision INTEGER NOT NULL DEFAULT 0, suspended INTEGER NOT NULL DEFAULT 0,
            next_turn INTEGER NOT NULL DEFAULT 0, CHECK(mode IN ('manual','auto')));
          CREATE TABLE history(jid TEXT NOT NULL, turn INTEGER NOT NULL, role TEXT NOT NULL,
            body TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(jid,turn,role));
          CREATE TABLE weather_locations(jid TEXT PRIMARY KEY, value TEXT NOT NULL);
          PRAGMA user_version=3;''')
        db.execute("INSERT INTO metadata VALUES('account',?)", (config.jid,))
        db.execute("INSERT INTO omemo VALUES('test-key','{\"synthetic\":true}')")
        db.execute("INSERT INTO seen VALUES('handled',?)", (time.time() + 60,))
        db.execute("INSERT INTO profiles VALUES(?,5,'2026-2027','changed','short')", (CHILD,))
        db.execute("INSERT INTO dialogues VALUES(?,'auto',7,0,1)", (CHILD,))
        db.execute("INSERT INTO history VALUES(?,1,'user','Старая память',?)", (CHILD, time.time()))
        db.execute("INSERT INTO weather_locations VALUES(?,?)", (CHILD, json.dumps({'id': 10, 'name': 'Город'})))
    path.chmod(0o600)
    return path


async def test_migration_preserves_old_state_and_doctor_is_read_only(config, capsys):
    path = legacy(config)
    db = Database(config.data_dir, config.jid)
    await db.initialize(config.users)
    row = await db.dialogue(CHILD)
    assert (row['mode'], row['revision'], row['next_turn']) == ('auto', 7, 1)
    assert row['cleared_at'] == row['mode_changed_at'] == 0
    assert (await db.profile(CHILD))['grade'] == 5
    assert (await db.weather_location(CHILD))['name'] == 'Город'
    assert await db.was_seen('handled')
    assert await db.history(CHILD, 10, 24, 30) == [{'role': 'user', 'content': 'Старая память'}]
    assert await db.call(lambda c: c.execute('SELECT value FROM omemo').fetchone()[0]) == '{"synthetic":true}'
    assert await db.archive_cursor(CHILD) is None
    before = path.read_bytes()
    doctor(config)
    assert 'SQLite: схема 5' in capsys.readouterr().out
    assert path.read_bytes() == before
    await db.initialize(config.users)
    assert (await db.dialogue(CHILD))['revision'] == 7


async def test_failed_migration_rolls_back_schema_and_old_data(config):
    path = legacy(config)
    with sqlite3.connect(path) as c:
        c.executescript("CREATE TRIGGER fail_profiles BEFORE INSERT ON profiles BEGIN SELECT RAISE(ABORT,'fixture'); END;")
    db = Database(config.data_dir, config.jid)
    with pytest.raises(sqlite3.IntegrityError):
        await db.initialize(config.users)
    with sqlite3.connect(path) as c:
        assert c.execute('PRAGMA user_version').fetchone()[0] == 3
        assert 'cleared_at' not in {r[1] for r in c.execute('PRAGMA table_info(dialogues)')}
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='archive_cursors'").fetchone() is None
        assert c.execute('SELECT body FROM history').fetchone()[0] == 'Старая память'
        assert c.execute('SELECT value FROM omemo').fetchone()[0] == '{"synthetic":true}'


async def test_manual_dedup_mode_and_history_share_one_transaction(database):
    await database.set_mode(CHILD, 'auto')
    def inconsistent(c):
        c.execute("INSERT INTO history VALUES(?,1,'assistant','existing',?)", (CHILD, time.time()))
        c.execute('UPDATE dialogues SET next_turn=0 WHERE jid=?', (CHILD,))
    await database.call(inconsistent)
    with pytest.raises(sqlite3.IntegrityError):
        await database.remember_event(CHILD, ('key',), 'manual', body='Ручной ответ')
    assert (await database.dialogue(CHILD))['mode'] == 'auto'
    assert await database.event_kind(('key',)) is None


async def test_duplicate_aliases_cannot_grow_persistent_journal_without_bound(database):
    def fill(c):
        c.execute("INSERT INTO message_events VALUES('known',?,'manual',?)", (CHILD, time.time() + 1))
        c.executemany("INSERT INTO message_events VALUES(?,?,'bot',?)",
                      [(str(i), CHILD, time.time()) for i in range(10000)])
    await database.call(fill)
    assert not await database.remember_event(CHILD, ('known', 'extra-alias'), 'manual')
    assert await database.call(lambda c: c.execute('SELECT COUNT(*) FROM message_events').fetchone()[0]) == 10000


async def test_restored_manual_history_is_chronological_without_breaking_answer_pairs(database):
    base = time.time() - 20
    def setup(c):
        c.execute("INSERT INTO dialogues(jid,next_turn) VALUES(?,2)", (CHILD,))
        c.executemany('INSERT INTO history VALUES(?,?,?,?,?)', [
            (CHILD, 1, 'user', 'Первый вопрос', base),
            (CHILD, 1, 'assistant', 'Ответ модели', base + 8),
            (CHILD, 2, 'user', 'Новый вопрос', base + 10)])
    await database.call(setup)
    await database.remember_event(CHILD, ('manual',), 'manual', body='Ручное уточнение', stamp=base + 9)
    assert [m['content'] for m in await database.history(CHILD, 100, 24, 30)] == [
        'Первый вопрос', 'Ответ модели', 'Ручное уточнение', 'Новый вопрос']
