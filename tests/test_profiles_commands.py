import asyncio
from dataclasses import replace
import threading

import pytest

from goose.commands import Commands
from goose.config import ConfigError
from goose.storage import Database, OmemoStorage, data_lock

PARENT = "parent@example.org"
CHILD = "pupil1@example.org"
OTHER = "pupil2@example.org"


async def test_profiles_survive_restart_without_reset(config, database):
    before = await database.profile(OTHER)
    await database.update_profile(CHILD, "grade", 3)
    await database.update_profile(CHILD, "academic_year", "2027-2028")
    reopened = Database(config.data_dir, config.jid)
    await reopened.initialize(config.users)
    profile = await reopened.profile(CHILD)
    assert profile["grade"] == 3
    assert profile["academic_year"] == "2027-2028"
    assert profile["schedule_class_id"] == config.users[CHILD]["schedule_class_id"]
    assert await reopened.profile(OTHER) == before
    await reopened.update_profile(CHILD, "schedule_class_id", "new-class")
    assert (await reopened.profile(CHILD))["grade"] == 3


async def test_no_cross_account_storage(config, database):
    other = Database(config.data_dir, "other@example.org")
    with pytest.raises(ConfigError, match="другому аккаунту"):
        await other.initialize({})


async def test_schema_future_rejected(config, database):
    await database.call(lambda db: db.execute("PRAGMA user_version=7"))
    with pytest.raises(ConfigError, match="версия БД"):
        await database.initialize({})


def test_exclusive_storage(config):
    with data_lock(config.data_dir):
        with pytest.raises(ConfigError, match="уже используется"):
            with data_lock(config.data_dir):
                pass


async def test_storage_keys_and_deletion_survive_reopen(config, database):
    storage = OmemoStorage(database)
    await storage.store_bytes("key", b"test-key-material")
    reopened = OmemoStorage(Database(config.data_dir, config.jid))
    assert (await reopened.load_bytes("key")).from_just() == b"test-key-material"
    await reopened.delete("key")
    assert (await storage.load("key")).is_nothing


async def test_dedup_persists(config, database):
    assert await database.claim("one", 60)
    assert not await Database(config.data_dir, config.jid).claim("one", 60)
    assert await database.claim("two", -1)
    assert await database.claim("two", 60)


async def test_cancellation_waits_for_database_commit(database):
    started, finish = threading.Event(), threading.Event()
    def operation(db):
        started.set()
        assert finish.wait(2)
        db.execute("INSERT INTO metadata VALUES ('cancellation-test', 'committed')")
    task = asyncio.create_task(database.call(operation))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert database.lock.locked()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = await database.call(lambda db: db.execute("SELECT value FROM metadata WHERE key='cancellation-test'").fetchone())
    assert row[0] == "committed"


async def test_commands_and_authorization(config, database):
    commands = Commands(config, database)
    assert await commands.handle(PARENT + "/phone", "!ping") == "Га-Га-Га!"
    assert "!context clear" in await commands.handle(CHILD, "!help")
    assert await commands.handle(CHILD, "обычный текст") is None
    assert await commands.handle("unknown@example.org", "!ping") is None
    assert "владельцу" in await commands.handle(CHILD, f"!admin user show {OTHER}")
    assert "владельцу" in await commands.handle(CHILD, f"!admin user set {OTHER} grade=4")
    assert (await database.profile(OTHER))["grade"] == 7
    assert "grade=7" in await commands.handle(PARENT, f"!admin user show {OTHER}")
    assert "обновлён" in await commands.handle(PARENT, f"!admin user set {OTHER} grade=8")
    assert (await database.profile(OTHER))["grade"] == 8
    assert "отключена" in await commands.handle(PARENT, "!admin status")


@pytest.mark.parametrize("body", [
    "!admin", "!admin user", "!admin user show", "!admin user show pupil1@example.org extra",
    "!admin user delete pupil1@example.org", "!admin user set pupil1@example.org grade=true",
    "!admin user set pupil1@example.org grade=0", "!admin user set pupil1@example.org grade=3;DROP",
    "!admin user set pupil1@example.org academic_year=2026-2029",
    "!admin user set pupil1@example.org schedule_class_id=",
    "!admin user set pupil1@example.org explanation_style=changed",
    "!admin user set missing@example.org grade=3",
    "!admin user set pupil1@example.org/mobile grade=3",
    "!admin user set pupil1@example.org grade=3 extra=4",
])
async def test_invalid_admin_has_no_partial_changes(config, database, body):
    before = await database.profile(CHILD)
    reply = await Commands(config, database).handle(PARENT, body)
    assert reply and reply != "Профиль обновлён."
    assert await database.profile(CHILD) == before


async def test_echo_opt_in_owner_only_and_literal(config, database, tmp_path):
    commands = Commands(config, database)
    assert await commands.handle(PARENT, "!echo ping") == "Эхо недоступно."
    enabled = Commands(replace(config, echo_enabled=True), database)
    assert await enabled.handle(CHILD, "!echo text") == "Эхо недоступно."
    assert await enabled.handle("unknown@example.org", "!echo text") is None
    payload = f"$(touch {tmp_path}/must-not-exist)"
    assert await enabled.handle(PARENT, "!echo " + payload) == payload
    assert not (tmp_path / "must-not-exist").exists()
