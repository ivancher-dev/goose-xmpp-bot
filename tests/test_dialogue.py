import asyncio
from copy import deepcopy
from dataclasses import replace
import sqlite3
import time

import pytest
import pytest_asyncio

from goose.commands import Commands
from goose.config import ConfigError
from goose.dialogue import Dialogues, estimate_tokens
from goose.model import ModelError
from goose.storage import Database

PARENT = "parent@example.org"
CHILD = "pupil1@example.org"
OTHER = "pupil2@example.org"


class Model:
    def __init__(self):
        self.calls, self.gates = [], {}
        self.entered = asyncio.Queue()
        self.ignore_cancel = False
        self.failure = None
        self.active = self.peak = 0

    async def complete(self, messages):
        self.calls.append(deepcopy(messages))
        body = messages[-1]["content"]
        self.entered.put_nowait(body)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if body in self.gates:
                try:
                    await self.gates[body].wait()
                except asyncio.CancelledError:
                    if not self.ignore_cancel:
                        raise
                    await self.gates[body].wait()
            if self.failure:
                raise self.failure
            return "Ответ: " + body
        finally:
            self.active -= 1

    async def close(self):
        pass


@pytest.fixture
def enabled(config, tmp_path):
    persona = tmp_path / "persona.md"
    persona.write_text("Дружелюбный учебный Гусь.")
    return replace(config, llm=replace(config.llm, enabled=True, persona_file=persona,
                                      base_url="https://api.example.org/v1", model="fixture-model"))


@pytest_asyncio.fixture
async def system(enabled, database):
    model, sent = Model(), []
    async def prepare(jid, text):
        def send():
            sent.append((jid, text))
            return True
        return send
    engine = Dialogues(enabled, database, model, prepare)
    await engine.initialize()
    try:
        yield engine, model, sent
    finally:
        for gate in model.gates.values():
            gate.set()
        await engine.close()


async def idle(engine):
    if engine.tasks:
        await asyncio.wait_for(asyncio.gather(*tuple(engine.tasks), return_exceptions=True), 2)


async def entered(model, body):
    assert await asyncio.wait_for(model.entered.get(), 1) == body


async def test_manual_default_and_disabled_model_do_not_request_api(system, config, database):
    engine, model, sent = system
    await engine.submit(CHILD + "/phone", "Подсказка")
    assert not model.calls and not sent
    assert len(await database.history(CHILD, 100, 24, 30)) == 1
    disabled = Dialogues(config, database, None, engine.prepare_reply)
    await disabled.initialize()
    try:
        await disabled.submit(CHILD, "Не сохранять при выключенной модели")
        assert len(await database.history(CHILD, 100, 24, 30)) == 1
        with pytest.raises(ConfigError, match="отключена"):
            await disabled.set_mode(CHILD, "auto")
    finally:
        await disabled.close()


async def test_histories_profiles_and_clearing_are_isolated(system, database):
    engine, model, sent = system
    for jid in (CHILD, OTHER):
        await engine.set_mode(jid, "auto")
    await engine.submit(CHILD, "Первый учебный пример")
    await engine.submit(OTHER, "Другой учебный пример")
    await idle(engine)
    first, other = model.calls
    assert "Другой учебный пример" not in str(first)
    assert "Первый учебный пример" not in str(other)
    for call in model.calls:
        assert "@example.org" not in str(call)
        assert "cohort-example" not in str(call)
    assert {jid for jid, _ in sent} == {CHILD, OTHER}
    before = await database.history(OTHER, 100, 24, 30)
    profile = await database.profile(CHILD)
    await engine.clear(CHILD)
    assert await database.history(CHILD, 100, 24, 30) == []
    assert await database.history(OTHER, 100, 24, 30) == before
    assert await database.profile(CHILD) == profile


async def test_current_profile_and_editable_persona_applied_to_new_requests(system, enabled, database):
    engine, model, _ = system
    await engine.set_mode(CHILD, "auto")
    await engine.submit(CHILD, "Объясни пример")
    await idle(engine)
    await engine.update_profile(CHILD, "grade", 3)
    enabled.llm.persona_file.write_text("Новый образ Гуся.")
    await engine.submit(CHILD, "Объясни проще")
    await idle(engine)
    assert "Текущий класс: 2" in model.calls[0][1]["content"]
    assert "Текущий класс: 3" in model.calls[1][1]["content"]
    assert "Новый образ" in model.calls[1][0]["content"]
    assert any(m["content"] == "Объясни пример" for m in model.calls[1])
    assert (await database.profile(OTHER))["grade"] == 7


@pytest.mark.parametrize("action", ["manual", "clear", "profile"])
async def test_late_api_result_ignoring_cancellation_never_sends(system, action, database):
    engine, model, sent = system
    model.ignore_cancel = True
    model.gates["Поздний результат"] = asyncio.Event()
    await engine.set_mode(CHILD, "auto")
    await engine.submit(CHILD, "Поздний результат")
    await entered(model, "Поздний результат")
    if action == "manual":
        await engine.set_mode(CHILD, "manual")
    elif action == "clear":
        await engine.clear(CHILD)
    else:
        await engine.update_profile(CHILD, "grade", 4)
    model.gates["Поздний результат"].set()
    await idle(engine)
    assert not sent
    history = await database.history(CHILD, 100, 24, 30)
    assert all(m["role"] == "user" for m in history)


async def test_manual_during_encryption_blocks_final_send(system):
    engine, model, sent = system
    preparing, release = asyncio.Event(), asyncio.Event()
    async def prepare(jid, text):
        preparing.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        def send():
            sent.append((jid, text))
            return True
        return send
    engine.prepare_reply = prepare
    await engine.set_mode(CHILD, "auto")
    await engine.submit(CHILD, "Вопрос")
    await asyncio.wait_for(preparing.wait(), 1)
    await engine.set_mode(CHILD, "manual")
    release.set()
    await idle(engine)
    assert not sent


async def test_manual_command_is_prompt_and_does_not_pause_other_dialogue(system, enabled, database):
    engine, model, sent = system
    model.gates["Долгий вопрос"] = asyncio.Event()
    await engine.set_mode(CHILD, "auto")
    await engine.set_mode(OTHER, "auto")
    await engine.submit(CHILD, "Долгий вопрос")
    await entered(model, "Долгий вопрос")
    commands = Commands(enabled, database, dialogues=engine)
    reply = await asyncio.wait_for(commands.handle(PARENT, f"!admin mode {CHILD} manual"), 0.5)
    assert reply == "Режим: manual"
    await engine.submit(OTHER, "Независимый вопрос")
    await idle(engine)
    assert sent == [(OTHER, "Ответ: Независимый вопрос")]


async def test_order_with_new_user_message_while_previous_request_is_running(system):
    engine, model, sent = system
    model.gates["Первый"] = asyncio.Event()
    await engine.set_mode(CHILD, "auto")
    await engine.submit(CHILD, "Первый")
    await entered(model, "Первый")
    await engine.submit(CHILD, "Второй")
    model.gates["Первый"].set()
    await idle(engine)
    assert [m["content"] for m in model.calls[1][2:]] == ["Первый", "Ответ: Первый", "Второй"]
    assert sent == [(CHILD, "Ответ: Первый"), (CHILD, "Ответ: Второй")]


async def test_queue_and_global_concurrency_are_bounded(system):
    engine, model, sent = system
    engine.slots = asyncio.Semaphore(1)
    model.gates["Ожидание"] = asyncio.Event()
    await engine.set_mode(CHILD, "auto")
    await engine.set_mode(OTHER, "auto")
    await engine.submit(CHILD, "Ожидание")
    await entered(model, "Ожидание")
    for _ in range(20):
        await engine.submit(CHILD, "Очередной вопрос")
    await engine.submit(OTHER, "Другой вопрос")
    assert len(engine.states[CHILD].pending) == 8
    assert model.active == 1
    model.gates["Ожидание"].set()
    await idle(engine)
    assert model.peak == 1
    assert len(sent) == 10


async def test_restart_keeps_mode_and_history_but_requires_explicit_resume(system, enabled, database):
    engine, model, sent = system
    await engine.set_mode(CHILD, "auto")
    await engine.submit(CHILD, "До перезапуска")
    await idle(engine)
    await engine.close()
    new_model = Model()
    reopened = Dialogues(enabled, database, new_model, engine.prepare_reply)
    await reopened.initialize()
    try:
        assert (await database.dialogue(CHILD))["mode"] == "auto"
        assert "пауза" in await reopened.mode(CHILD)
        await reopened.submit(CHILD, "Во время паузы")
        assert not new_model.calls
        await reopened.set_mode(CHILD, "auto")
        await reopened.submit(CHILD, "После подтверждения")
        await idle(reopened)
        assert len(new_model.calls) == 1
        assert any(m["content"] == "До перезапуска" for m in new_model.calls[0])
        assert await reopened.mode(OTHER) == "manual"
    finally:
        await reopened.close()


async def test_context_budget_and_errors_do_not_flood_chat(system, enabled):
    engine, model, sent = system
    now = [0]
    engine.clock = lambda: now[0]
    engine.config = replace(enabled, memory=replace(enabled.memory, context_token_budget=256))
    await engine.set_mode(CHILD, "auto")
    await engine.submit(CHILD, "Не помещается")
    await idle(engine)
    await engine.submit(CHILD, "Ещё вопрос")
    await idle(engine)
    assert not model.calls and len(sent) == 1
    assert "короче" in sent[0][1]
    now[0] = 61
    await engine.submit(CHILD, "Следующий вопрос")
    await idle(engine)
    assert len(sent) == 2


async def test_context_trims_old_messages_and_respects_utf8_budget(system, enabled):
    engine, model, _ = system
    engine.config = replace(enabled, memory=replace(enabled.memory, context_token_budget=2000))
    await engine.set_mode(CHILD, "auto")
    for i in range(10):
        await engine.submit(CHILD, f"Пример номер {i}: " + "я" * 70)
        await idle(engine)
    assert len(model.calls) == 10
    for context in model.calls:
        assert estimate_tokens(context) <= 2000
        assert context[2]["role"] == "user"
    assert not any("номер 0:" in m["content"] for m in model.calls[-1])


async def test_unknown_sender_and_command_rights_do_not_reach_api(system, enabled, database):
    engine, model, sent = system
    commands = Commands(enabled, database, dialogues=engine)
    await engine.submit("stranger@example.org", "Вопрос")
    assert await commands.handle("stranger@example.org", f"!admin mode {OTHER} auto") is None
    assert "владельцу" in await commands.handle(CHILD, f"!admin mode {OTHER} auto")
    assert "Использование" in await commands.handle(CHILD, f"!context clear {OTHER}")
    await commands.handle(CHILD, "!context clear")
    assert not model.calls and not sent
    assert await engine.mode(OTHER) == "manual"
    assert "Сначала" in await commands.handle(PARENT, f"!admin mode {PARENT} auto")


async def test_retention_and_context_clear_preserve_other_data(system, database):
    engine, _, _ = system
    await engine.submit(CHILD, "Истекло")
    await engine.submit(OTHER, "Сохранить")
    await database.call(lambda db: db.execute("UPDATE history SET created=? WHERE jid=?", (time.time() - 31 * 86400, CHILD)))
    await database.expire_history(30)
    assert await database.history(CHILD, 100, 24, 30) == []
    assert len(await database.history(OTHER, 100, 24, 30)) == 1
    assert (await database.profile(CHILD))["grade"] == 2


async def test_legacy_migration_preserves_identity_profiles_and_dedup(config):
    path = config.data_dir / "goose.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE profiles(jid TEXT PRIMARY KEY,grade INTEGER,academic_year TEXT,schedule_class_id TEXT,explanation_style TEXT);
          CREATE TABLE omemo(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE seen(key TEXT PRIMARY KEY,expires REAL NOT NULL);
          PRAGMA user_version=1;""")
        db.execute("INSERT INTO metadata VALUES('account',?)", (config.jid,))
        db.execute("INSERT INTO omemo VALUES('fixture-identity','{\"fixture\":true}')")
        db.execute("INSERT INTO seen VALUES('already-handled',?)", (time.time() + 100,))
        db.execute("INSERT INTO profiles VALUES(?,5,'2027-2028','changed-group','style')", (CHILD,))
    database = Database(config.data_dir, config.jid)
    await database.initialize(config.users)
    assert (await database.profile(CHILD))["grade"] == 5
    assert not await database.claim("already-handled", 60)
    assert await database.call(lambda db: db.execute("SELECT value FROM omemo").fetchone()[0]) == '{"fixture":true}'
    assert await database.call(lambda db: db.execute("PRAGMA user_version").fetchone()[0]) == 2


async def test_migration_failure_rolls_back_schema_and_old_data(config):
    path = config.data_dir / "goose.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE profiles(jid TEXT PRIMARY KEY,grade INTEGER,academic_year TEXT,schedule_class_id TEXT,explanation_style TEXT);
          CREATE TABLE omemo(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TRIGGER reject_seed BEFORE INSERT ON profiles BEGIN SELECT RAISE(ABORT,'fixture failure'); END;
          PRAGMA user_version=1;""")
        db.execute("INSERT INTO metadata VALUES('account',?)", (config.jid,))
        db.execute("INSERT INTO omemo VALUES('fixture','preserve')")
    database = Database(config.data_dir, config.jid)
    with pytest.raises(sqlite3.IntegrityError):
        await database.initialize(config.users)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT value FROM omemo").fetchone()[0] == "preserve"
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='dialogues'").fetchall()


async def test_cancelled_old_worker_cannot_consume_new_revision_queue(system):
    engine, model, sent = system
    model.ignore_cancel = True
    model.gates["Старый"] = asyncio.Event()
    model.gates["Новый первый"] = asyncio.Event()
    await engine.set_mode(CHILD, "auto")
    await engine.submit(CHILD, "Старый")
    await entered(model, "Старый")
    old_task = engine.states[CHILD].task
    await engine.set_mode(CHILD, "manual")
    await engine.set_mode(CHILD, "auto")
    await engine.submit(CHILD, "Новый первый")
    await entered(model, "Новый первый")
    await engine.submit(CHILD, "Новый второй")
    model.gates["Старый"].set()
    await asyncio.wait_for(old_task, 1)
    assert len(engine.states[CHILD].pending) == 1
    model.gates["Новый первый"].set()
    await idle(engine)
    assert sent == [(CHILD, "Ответ: Новый первый"), (CHILD, "Ответ: Новый второй")]


async def test_expired_job_and_shutdown_never_deliver_late_response(system):
    engine, model, sent = system
    now = [0]
    engine.clock = lambda: now[0]
    model.gates["Просроченный"] = asyncio.Event()
    await engine.set_mode(CHILD, "auto")
    await engine.submit(CHILD, "Просроченный")
    await entered(model, "Просроченный")
    now[0] = 1000
    model.gates["Просроченный"].set()
    await idle(engine)
    assert not sent
    model.ignore_cancel = True
    model.gates["Завершение"] = asyncio.Event()
    await engine.submit(CHILD, "Завершение")
    await entered(model, "Завершение")
    closing = asyncio.create_task(engine.close())
    await asyncio.sleep(0)
    model.gates["Завершение"].set()
    await asyncio.wait_for(closing, 1)
    assert not sent


async def test_private_provider_exception_is_not_logged(system, caplog):
    engine, model, sent = system
    model.failure = RuntimeError("private-body-and-api-secret")
    await engine.set_mode(CHILD, "auto")
    await engine.submit(CHILD, "Текст только в своём контексте")
    with caplog.at_level("ERROR", logger="goose"):
        await idle(engine)
    assert "private-body-and-api-secret" not in caplog.text
    assert "Текст только" not in caplog.text
    assert "reason=internal" in caplog.text
    assert not sent
