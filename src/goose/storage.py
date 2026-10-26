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

from .config import ConfigError, profile_value


@contextmanager
def data_lock(directory: Path):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise ConfigError("Каталог данных должен иметь права 0700.")
    with (directory / ".lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfigError("Хранилище уже используется; остановите сервис перед управлением устройствами.") from exc
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
            if version not in (0, 1, 2, 3):
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
            db.execute("""CREATE TABLE IF NOT EXISTS history (
                jid TEXT NOT NULL, turn INTEGER NOT NULL, role TEXT NOT NULL,
                body TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(jid, turn, role), CHECK(role IN ('user', 'assistant')))""")
            db.execute("CREATE INDEX IF NOT EXISTS history_created ON history(created)")
            db.execute("CREATE TABLE IF NOT EXISTS weather_locations (jid TEXT PRIMARY KEY, value TEXT NOT NULL)")
            for jid, fields in profiles.items():
                db.execute("INSERT OR IGNORE INTO profiles VALUES (?, ?, ?, ?, ?)",
                           (jid, fields["grade"], fields["academic_year"], fields["schedule_class_id"], fields["explanation_style"]))
            db.execute("PRAGMA user_version=3")
        await self.call(create)
        os.chmod(self.path, 0o600)

    async def profile(self, jid):
        row = await self.call(lambda db: db.execute("SELECT * FROM profiles WHERE jid=?", (jid,)).fetchone())
        return dict(row) if row else None

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
            db.execute("UPDATE dialogues SET mode=?, suspended=0, revision=revision+1 WHERE jid=?", (mode, jid))
        await self.call(update)

    async def suspend_auto(self):
        # До этапа 3 нет MAM-синхронизации ручных сообщений после отсутствия бота.
        await self.call(lambda db: db.execute(
            "UPDATE dialogues SET suspended=1, revision=revision+1 WHERE mode='auto'"))

    async def clear_context(self, jid):
        def clear(db):
            db.execute("INSERT OR IGNORE INTO dialogues(jid) VALUES (?)", (jid,))
            db.execute("UPDATE dialogues SET revision=revision+1 WHERE jid=?", (jid,))
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
               ORDER BY turn DESC, role ASC LIMIT ?""", (jid, turn, cutoff, limit)).fetchall())
        return [{"role": row["role"], "content": row["body"]} for row in reversed(rows)]

    async def expire_history(self, retention_days):
        cutoff = time.time() - retention_days * 86400
        await self.call(lambda db: db.execute("DELETE FROM history WHERE created<=?", (cutoff,)))


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
