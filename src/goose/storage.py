"""Постоянные профили и ключи; SQLite-ввод/вывод выполняется в рабочем потоке."""

import asyncio
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time

from omemo.storage import Just, Nothing, Storage

from .config import ConfigError, TransientError, profile_value
from .language import DEFAULTS, preference_value


@contextmanager
def data_lock(directory: Path):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise ConfigError("Каталог данных должен иметь права 0700.")
    with (directory / ".lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TransientError("Хранилище уже используется; остановите сервис перед управлением устройствами.") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Database:
    def __init__(self, directory: Path, account: str):
        self.path = directory / "goose.sqlite3"
        self.account = account
        self.lock = asyncio.Lock()

    async def call(self, operation):
        async with self.lock:
            task = asyncio.create_task(asyncio.to_thread(self._call, operation))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Освобождаем asyncio-lock и файловую блокировку только после commit/rollback.
                await task
                raise

    def _call(self, operation):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN")
            return operation(db)

    async def initialize(self, profiles):
        def create(db):
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5):
                raise ConfigError("Неподдерживаемая версия БД; автоматический откат запрещён.")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            existing = db.execute("SELECT value FROM metadata WHERE key='account'").fetchone()
            if existing is not None and existing[0] != self.account:
                raise ConfigError("Хранилище принадлежит другому аккаунту XMPP.")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('account', ?)", (self.account,))
            db.execute("""CREATE TABLE IF NOT EXISTS profiles (
                jid TEXT PRIMARY KEY, grade INTEGER NOT NULL, academic_year TEXT NOT NULL,
                schedule_class_id TEXT NOT NULL, explanation_style TEXT NOT NULL)""")
            db.execute("CREATE TABLE IF NOT EXISTS omemo (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, expires REAL NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS dialogues (
                jid TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'manual',
                revision INTEGER NOT NULL DEFAULT 0, suspended INTEGER NOT NULL DEFAULT 0,
                next_turn INTEGER NOT NULL DEFAULT 0,
                CHECK(mode IN ('manual', 'auto')))""")
            columns = {row[1] for row in db.execute('PRAGMA table_info(dialogues)')}
            for name in ('cleared_at', 'mode_changed_at'):
                if name not in columns:
                    db.execute(f'ALTER TABLE dialogues ADD COLUMN {name} REAL NOT NULL DEFAULT 0')
            db.execute("""CREATE TABLE IF NOT EXISTS history (
                jid TEXT NOT NULL, turn INTEGER NOT NULL, role TEXT NOT NULL,
                body TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(jid, turn, role), CHECK(role IN ('user', 'assistant')))""")
            db.execute("CREATE INDEX IF NOT EXISTS history_created ON history(created)")
            db.execute("CREATE TABLE IF NOT EXISTS weather_locations (jid TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS message_events (
                key TEXT PRIMARY KEY, jid TEXT NOT NULL, kind TEXT NOT NULL, created REAL NOT NULL,
                CHECK(kind IN ('incoming', 'manual', 'bot', 'ignored')))""")
            db.execute("CREATE INDEX IF NOT EXISTS message_events_created ON message_events(created)")
            db.execute("""CREATE TABLE IF NOT EXISTS archive_cursors (
                jid TEXT PRIMARY KEY, anchor TEXT, stamp REAL NOT NULL,
                confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0, 1)))""")
            db.execute("""CREATE TABLE IF NOT EXISTS language_feedback (
                jid TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                mode TEXT NOT NULL DEFAULT 'brief' CHECK(mode IN ('brief','detailed')),
                max_notes INTEGER NOT NULL DEFAULT 2 CHECK(max_notes BETWEEN 1 AND 5))""")
            for jid, fields in profiles.items():
                db.execute("INSERT OR IGNORE INTO profiles VALUES (?, ?, ?, ?, ?)",
                           (jid, fields["grade"], fields["academic_year"], fields["schedule_class_id"], fields["explanation_style"]))
            db.execute("PRAGMA user_version=5")
        await self.call(create)
        os.chmod(self.path, 0o600)

    async def profile(self, jid):
        row = await self.call(lambda db: db.execute("SELECT * FROM profiles WHERE jid=?", (jid,)).fetchone())
        return dict(row) if row else None

    async def language_preferences(self, jid):
        row = await self.call(lambda db: db.execute(
            "SELECT enabled,mode,max_notes FROM language_feedback WHERE jid=?", (jid,)).fetchone())
        return {'enabled': bool(row['enabled']), 'mode': row['mode'], 'max_notes': row['max_notes']} if row else dict(DEFAULTS)

    async def update_language(self, jid, field, value):
        preference_value(field, value)
        def update(db):
            if db.execute('SELECT 1 FROM profiles WHERE jid=?', (jid,)).fetchone() is None:
                raise ConfigError('Профиль не найден.')
            db.execute('INSERT OR IGNORE INTO language_feedback(jid) VALUES(?)', (jid,))
            # Имя поля проверено по закрытому списку; значения параметризованы.
            db.execute(f'UPDATE language_feedback SET {field}=? WHERE jid=?', (value, jid))
            db.execute('UPDATE dialogues SET revision=revision+1 WHERE jid=?', (jid,))
        await self.call(update)

    async def weather_location(self, jid):
        row = await self.call(lambda db: db.execute(
            "SELECT value FROM weather_locations WHERE jid=?", (jid,)).fetchone())
        return json.loads(row[0]) if row else None

    async def set_weather_location(self, jid, value):
        if value is None:
            await self.call(lambda db: db.execute("DELETE FROM weather_locations WHERE jid=?", (jid,)))
        else:
            payload = json.dumps(value, ensure_ascii=True)
            await self.call(lambda db: db.execute(
                "INSERT INTO weather_locations VALUES (?, ?) ON CONFLICT(jid) DO UPDATE SET value=excluded.value",
                (jid, payload)))

    async def update_profile(self, jid, field, value):
        if field not in {"grade", "academic_year", "schedule_class_id"}:
            raise ConfigError("Изменение этого поля запрещено.")
        profile_value(field, value)
        # Имя столбца ограничено константным набором выше; значения параметризованы.
        def update(db):
            count = db.execute(f"UPDATE profiles SET {field}=? WHERE jid=?", (value, jid)).rowcount
            if count == 1:
                db.execute("UPDATE dialogues SET revision=revision+1 WHERE jid=?", (jid,))
            return count
        count = await self.call(update)
        if count != 1:
            raise ConfigError("Профиль не найден.")

    async def claim(self, key, ttl):
        now = time.time()
        def claim(db):
            db.execute("DELETE FROM seen WHERE expires <= ?", (now,))
            inserted = db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (key, now + ttl)).rowcount
            # Ограничиваем постоянный журнал, даже при потоке уникальных команд.
            db.execute("DELETE FROM seen WHERE key IN (SELECT key FROM seen ORDER BY expires DESC LIMIT -1 OFFSET 10000)")
            return bool(inserted)
        return await self.call(claim)

    async def dialogue(self, jid):
        def read(db):
            db.execute("INSERT OR IGNORE INTO dialogues(jid) VALUES (?)", (jid,))
            return dict(db.execute("SELECT * FROM dialogues WHERE jid=?", (jid,)).fetchone())
        return await self.call(read)

    async def set_mode(self, jid, mode):
        if mode not in {"manual", "auto"}:
            raise ConfigError("Режим: manual или auto.")
        def update(db):
            db.execute("INSERT OR IGNORE INTO dialogues(jid) VALUES (?)", (jid,))
            db.execute("UPDATE dialogues SET mode=?, suspended=0, revision=revision+1, mode_changed_at=? WHERE jid=?",
                       (mode, time.time(), jid))
            if mode == 'auto':
                db.execute('UPDATE archive_cursors SET confirmed=1 WHERE jid=?', (jid,))
        await self.call(update)

    async def suspend_auto(self):
        # До завершения MAM-синхронизации старый auto не разрешает отправку.
        await self.call(lambda db: db.execute(
            "UPDATE dialogues SET suspended=1, revision=revision+1 WHERE mode='auto'"))

    async def clear_context(self, jid):
        def clear(db):
            db.execute("INSERT OR IGNORE INTO dialogues(jid) VALUES (?)", (jid,))
            db.execute("UPDATE dialogues SET revision=revision+1, cleared_at=? WHERE jid=?", (time.time(), jid))
            db.execute("DELETE FROM history WHERE jid=?", (jid,))
        await self.call(clear)

    async def add_user_message(self, jid, body):
        def insert(db):
            db.execute("INSERT OR IGNORE INTO dialogues(jid) VALUES (?)", (jid,))
            db.execute("UPDATE dialogues SET next_turn=next_turn+1 WHERE jid=?", (jid,))
            turn = db.execute("SELECT next_turn FROM dialogues WHERE jid=?", (jid,)).fetchone()[0]
            db.execute("INSERT INTO history VALUES (?, ?, 'user', ?, ?)", (jid, turn, body, time.time()))
            # Не более 500 ходов на диалог; окно API и срок хранения ограничены отдельно.
            db.execute("""DELETE FROM history WHERE jid=? AND turn NOT IN
                (SELECT DISTINCT turn FROM history WHERE jid=? ORDER BY turn DESC LIMIT 500)""", (jid, jid))
            return turn
        return await self.call(insert)

    async def add_assistant_message(self, jid, turn, body):
        await self.call(lambda db: db.execute(
            "INSERT INTO history VALUES (?, ?, 'assistant', ?, ?)", (jid, turn, body, time.time())))

    async def history(self, jid, turn, limit, retention_days):
        cutoff = time.time() - retention_days * 86400
        rows = await self.call(lambda db: db.execute(
            """SELECT role, body FROM history WHERE jid=? AND turn<=? AND created>?
               ORDER BY (SELECT MIN(h.created) FROM history h
                         WHERE h.jid=history.jid AND h.turn=history.turn) DESC,
                        turn DESC, role ASC LIMIT ?""", (jid, turn, cutoff, limit)).fetchall())
        return [{"role": row["role"], "content": row["body"]} for row in reversed(rows)]

    async def expire_history(self, retention_days):
        cutoff = time.time() - retention_days * 86400
        await self.call(lambda db: db.execute("DELETE FROM history WHERE created<=?", (cutoff,)))

    async def event_kind(self, keys):
        def read(db):
            for key in keys:
                row = db.execute('SELECT kind FROM message_events WHERE key=?', (key,)).fetchone()
                if row:
                    return row[0]
            return None
        return await self.call(read)

    async def was_seen(self, key):
        # Совместимость с журналом доставок 0.1/0.2 при первом обновлении.
        return await self.call(lambda db: db.execute(
            'SELECT 1 FROM seen WHERE key=? AND expires>?', (key, time.time())).fetchone() is not None)

    async def remember_event(self, jid, keys, kind, *, body=None, stamp=None, live=False):
        if kind not in {'incoming', 'manual', 'bot', 'ignored'} or not keys:
            raise ValueError('Invalid event')
        stamp = time.time() if stamp is None else stamp
        def record(db):
            existing = next((row for key in keys if (row := db.execute(
                'SELECT kind FROM message_events WHERE key=?', (key,)).fetchone())), None)
            for key in keys:
                db.execute('INSERT OR IGNORE INTO message_events VALUES (?, ?, ?, ?)',
                           (key, jid, existing[0] if existing else kind, time.time()))
            db.execute('DELETE FROM message_events WHERE created<?', (time.time() - 35 * 86400,))
            db.execute('''DELETE FROM message_events WHERE key IN
                (SELECT key FROM message_events ORDER BY created DESC LIMIT -1 OFFSET 10000)''')
            if existing:
                return False
            db.execute('INSERT OR IGNORE INTO dialogues(jid) VALUES (?)', (jid,))
            row = db.execute('SELECT * FROM dialogues WHERE jid=?', (jid,)).fetchone()
            if kind == 'manual' and (live or stamp >= row['mode_changed_at']):
                db.execute("""UPDATE dialogues SET mode='manual', suspended=0,
                    revision=revision+1, mode_changed_at=? WHERE jid=?""", (stamp, jid))
            if body and (live or stamp > row['cleared_at']):
                db.execute('UPDATE dialogues SET next_turn=next_turn+1 WHERE jid=?', (jid,))
                turn = db.execute('SELECT next_turn FROM dialogues WHERE jid=?', (jid,)).fetchone()[0]
                db.execute('INSERT INTO history VALUES (?, ?, ?, ?, ?)',
                           (jid, turn, 'assistant' if kind == 'manual' else 'user', body, stamp))
                db.execute('''DELETE FROM history WHERE jid=? AND turn NOT IN
                    (SELECT DISTINCT turn FROM history WHERE jid=? ORDER BY turn DESC LIMIT 500)''', (jid, jid))
            return True
        return await self.call(record)

    async def owner_event_keys(self):
        return await self.call(lambda db: [r[0] for r in db.execute(
            "SELECT key FROM message_events WHERE kind IN ('bot','manual','ignored') ORDER BY created DESC LIMIT 10000")])

    async def archive_cursor(self, jid):
        row = await self.call(lambda db: db.execute('SELECT * FROM archive_cursors WHERE jid=?', (jid,)).fetchone())
        return dict(row) if row else None

    async def save_archive_cursor(self, jid, anchor, stamp, *, confirmed=None):
        def save(db):
            row = db.execute('SELECT confirmed FROM archive_cursors WHERE jid=?', (jid,)).fetchone()
            trusted = int(confirmed) if confirmed is not None else (row[0] if row else 0)
            db.execute('''INSERT INTO archive_cursors VALUES (?, ?, ?, ?) ON CONFLICT(jid)
                DO UPDATE SET anchor=excluded.anchor, stamp=excluded.stamp, confirmed=excluded.confirmed''',
                       (jid, anchor, stamp, trusted))
        await self.call(save)

    async def invalidate_archive(self, jid, *, transient=False):
        def invalidate(db):
            if not transient:
                db.execute('UPDATE archive_cursors SET confirmed=0 WHERE jid=?', (jid,))
            db.execute("UPDATE dialogues SET suspended=1, revision=revision+1 WHERE jid=? AND mode='auto'", (jid,))
        await self.call(invalidate)

    async def resume_auto(self, jid, revision):
        return await self.call(lambda db: bool(db.execute('''UPDATE dialogues SET suspended=0, revision=revision+1
            WHERE jid=? AND mode='auto' AND suspended=1 AND revision=?
              AND EXISTS (SELECT 1 FROM archive_cursors WHERE jid=? AND confirmed=1)''',
            (jid, revision, jid)).rowcount))


class OmemoStorage(Storage):
    def __init__(self, database):
        super().__init__(disable_cache=True)
        self.database = database

    async def _load(self, key):
        row = await self.database.call(lambda db: db.execute("SELECT value FROM omemo WHERE key=?", (key,)).fetchone())
        return Nothing() if row is None else Just(json.loads(row[0]))

    async def _store(self, key, value):
        payload = json.dumps(value, ensure_ascii=True)
        await self.database.call(lambda db: db.execute("INSERT INTO omemo VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, payload)))

    async def _delete(self, key):
        await self.database.call(lambda db: db.execute("DELETE FROM omemo WHERE key=?", (key,)))
