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
            return operation(db)

    async def initialize(self, profiles):
        def create(db):
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
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
            for jid, fields in profiles.items():
                db.execute("INSERT OR IGNORE INTO profiles VALUES (?, ?, ?, ?, ?)",
                           (jid, fields["grade"], fields["academic_year"], fields["schedule_class_id"], fields["explanation_style"]))
            db.execute("PRAGMA user_version=1")
        await self.call(create)
        os.chmod(self.path, 0o600)

    async def profile(self, jid):
        row = await self.call(lambda db: db.execute("SELECT * FROM profiles WHERE jid=?", (jid,)).fetchone())
        return dict(row) if row else None

    async def update_profile(self, jid, field, value):
        if field not in {"grade", "academic_year", "schedule_class_id"}:
            raise ConfigError("Изменение этого поля запрещено.")
        profile_value(field, value)
        # Имя столбца ограничено константным набором выше; значения параметризованы.
        count = await self.call(lambda db: db.execute(f"UPDATE profiles SET {field}=? WHERE jid=?", (value, jid)).rowcount)
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
