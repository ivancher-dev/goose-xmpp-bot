"""Время виртуальное, API подставной: перехват, оценка и отправка выполняются реально."""

import asyncio
from dataclasses import replace
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from goose.config import ConfigError, load_config
from goose.dialogue import Dialogues, estimate_tokens
from goose.model import OpenAICompatible
from goose.storage import Database
import goose.storage as storage
from goose.takeover import unanswered

CHILD, OTHER = 'pupil1@example.org', 'pupil2@example.org'


@pytest_asyncio.fixture
async def auto_system(config, database, tmp_path, monkeypatch):
    persona = tmp_path/'persona.md'
    persona.write_text('Добрый учебный Гусь.')
    cfg = replace(config, llm=replace(config.llm, enabled=True, persona_file=persona))
    model = AsyncMock()
    model.complete.return_value = '{"certain":false,"unanswered":[]}'
    sent, now = [], [time.time()]
    monkeypatch.setattr(storage, 'time', SimpleNamespace(time=lambda: now[0]))
    async def prepare(jid, text):
        def send():
            sent.append((jid, text))
            return True
        return send
    engine = Dialogues(cfg, database, model, prepare, clock=lambda: now[0],
                       wall_clock=lambda: now[0], can_resume=lambda jid: True)
    await engine.initialize()
    for jid in (CHILD, OTHER):
        await database.save_archive_cursor(jid, None, now[0], confirmed=True)
        await engine.set_mode(jid, 'auto')
    try:
        yield engine, model, sent, now
    finally:
        await engine.close()


async def idle(engine):
    await asyncio.wait_for(asyncio.gather(*tuple(engine.tasks), return_exceptions=True), 2)


async def manual(engine, now, text='Ручное объяснение', ident='manual', *, jid=CHILD, live=True):
    await engine.restore(jid, (ident,), 'manual', text, now[0], live=live)


async def question_during_pause(engine, now):
    await manual(engine, now)
    await engine.submit(CHILD, 'Почему листья зелёные?')
    return (await engine.database.dialogue(CHILD))['next_turn']


async def test_timeout_is_per_dialogue_owner_only_and_duplicates_do_not_extend(auto_system):
    engine, model, sent, now = auto_system
    await manual(engine, now)
    now[0] += 59
    await engine.resume_takeovers()
    assert await engine.mode(CHILD) == 'manual' and await engine.mode(OTHER) == 'auto'
    # Ни сообщение ребёнка, ни повтор той же ручной доставки не продлевают паузу.
    await engine.submit(CHILD, 'Спасибо!')
    await manual(engine, now, ident='manual')
    now[0] += 1
    await engine.resume_takeovers()
    await idle(engine)
    assert await engine.mode(CHILD) == 'auto' and await engine.mode(OTHER) == 'auto'
    assert not sent
    model.complete.assert_awaited_once()  # Только оценка «спасибо».
    await engine.resume_takeovers()
    assert model.complete.await_count == 1


async def test_each_new_manual_message_restarts_minute(auto_system):
    engine, model, sent, now = auto_system
    await manual(engine, now)
    now[0] += 45
    await manual(engine, now, ident='second')
    now[0] += 59
    await engine.resume_takeovers()
    assert await engine.mode(CHILD) == 'manual'
    now[0] += 1
    await engine.resume_takeovers()
    assert await engine.mode(CHILD) == 'auto'
    assert not model.complete.called and not sent


@pytest.mark.parametrize('when', ['before', 'after'])
async def test_explicit_manual_never_expires(auto_system, when):
    engine, model, sent, now = auto_system
    if when == 'before':
        await engine.set_mode(CHILD, 'manual')
    await manual(engine, now)
    if when == 'after':
        await engine.set_mode(CHILD, 'manual')
    now[0] += 3601
    await engine.resume_takeovers()
    assert await engine.mode(CHILD) == 'manual' and not sent and not model.complete.called


@pytest.mark.parametrize('block', ['holds', 'disconnected', 'archive', 'unconfirmed'])
async def test_return_waits_for_verified_transport_and_archive(auto_system, block):
    engine, model, sent, now = auto_system
    await manual(engine, now)
    now[0] += 61
    if block == 'holds':
        engine.hold(CHILD)
    elif block == 'disconnected':
        engine.can_resume = lambda jid: False
    elif block == 'archive':
        await engine.archive_failed(CHILD, transient=True)
    else:
        await engine.database.save_archive_cursor(CHILD, None, now[0], confirmed=False)
    await engine.resume_takeovers()
    assert (await engine.database.dialogue(CHILD))['mode'] == 'manual'
    assert not sent and not model.complete.called


async def test_certain_unanswered_question_gets_one_reply_and_no_foreign_context(auto_system):
    engine, model, sent, now = auto_system
    await engine.submit(OTHER, 'Чужой пример')
    await idle(engine)
    model.reset_mock(); sent.clear()
    turn = await question_during_pause(engine, now)
    model.complete.side_effect = [json.dumps({'certain': True, 'unanswered': [turn]}), 'В листьях есть хлорофилл.']
    now[0] += 61
    await engine.resume_takeovers(); await idle(engine)
    assert sent == [(CHILD, 'В листьях есть хлорофилл.')]
    assert model.complete.await_count == 2
    for call in model.complete.call_args_list:
        assert 'Чужой пример' not in str(call) and '@example.org' not in str(call)
    history = await engine.database.history(CHILD, 100, 24, 30)
    assert history[-1]['content'] == 'В листьях есть хлорофилл.'
    await engine.resume_takeovers(); await idle(engine)
    assert len(sent) == 1 and model.complete.await_count == 2


async def test_human_answer_is_in_review_and_empty_decision_sends_nothing(auto_system):
    engine, model, sent, now = auto_system
    turn = await question_during_pause(engine, now)
    await manual(engine, now, text='Листья зелёные из-за хлорофилла.', ident='answer')
    now[0] += 61
    await engine.resume_takeovers(); await idle(engine)
    payload = json.loads(model.complete.call_args.args[0][1]['content'])
    assert payload['candidate_ids'] == [turn]
    assert any(m['body'] == 'Листья зелёные из-за хлорофилла.' for m in payload['conversation'])
    assert not sent and await engine.mode(CHILD) == 'auto'


async def test_completed_bot_question_is_not_reassessed(auto_system):
    engine, model, sent, now = auto_system
    model.complete.return_value = 'Ответ до вмешательства'
    await engine.submit(CHILD, 'Объясни пример')
    await idle(engine)
    await manual(engine, now)
    model.reset_mock(); sent.clear(); now[0] += 61
    await engine.resume_takeovers(); await idle(engine)
    assert not model.complete.called and not sent


@pytest.mark.parametrize('change', ['new_user', 'manual', 'explicit_manual', 'clear'])
async def test_change_during_review_blocks_late_result_even_if_api_ignores_cancel(auto_system, change):
    engine, model, sent, now = auto_system
    turn = await question_during_pause(engine, now)
    entered, release = asyncio.Event(), asyncio.Event()
    async def review(messages):
        if 'candidate_ids' not in messages[-1]['content']:
            return 'Ответ новому вопросу'
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return json.dumps({'certain': True, 'unanswered': [turn]})
    model.complete.side_effect = review
    now[0] += 61
    await engine.resume_takeovers()
    await asyncio.wait_for(entered.wait(), 1)
    if change == 'new_user':
        await engine.submit(CHILD, 'Новый вопрос')
    elif change == 'manual':
        await manual(engine, now, ident='new-owner')
    elif change == 'explicit_manual':
        await engine.set_mode(CHILD, 'manual')
    else:
        await engine.clear(CHILD)
    release.set(); await idle(engine)
    assert sent == ([(CHILD, 'Ответ новому вопросу')] if change == 'new_user' else [])


async def test_manual_during_followup_encryption_blocks_send(auto_system):
    engine, model, sent, now = auto_system
    turn = await question_during_pause(engine, now)
    model.complete.side_effect = [json.dumps({'certain': True, 'unanswered': [turn]}), 'Отложенный ответ']
    entered, release = asyncio.Event(), asyncio.Event()
    async def prepare(*args):
        entered.set()
        try: await release.wait()
        except asyncio.CancelledError: await release.wait()
        return lambda: sent.append(args)
    engine.prepare_reply = prepare
    now[0] += 61; await engine.resume_takeovers()
    await asyncio.wait_for(entered.wait(), 1)
    await manual(engine, now, ident='interrupt')
    release.set(); await idle(engine)
    assert not sent


@pytest.mark.parametrize('reason', ['unreadable', 'old', 'budget', 'archive_only', 'disabled_review'])
async def test_no_review_for_incomplete_or_ineligible_context(auto_system, reason):
    engine, model, sent, now = auto_system
    if reason == 'archive_only':
        await engine.restore(CHILD, ('old-question',), 'incoming', 'Вопрос из архива', now[0], live=False)
        await manual(engine, now, live=False)
    else:
        await question_during_pause(engine, now)
        if reason == 'unreadable':
            await manual(engine, now, text=None, ident='unreadable')
        elif reason == 'old':
            now[0] += 601
        elif reason == 'budget':
            engine.config = replace(engine.config, memory=replace(engine.config.memory, context_token_budget=500))
        else:
            engine.config = replace(engine.config, interaction=replace(engine.config.interaction, takeover_review_enabled=False))
    now[0] += 61
    await engine.resume_takeovers(); await idle(engine)
    assert await engine.mode(CHILD) == 'auto' and not model.complete.called and not sent


async def test_restart_keeps_pause_and_questions_but_waits_for_mam(auto_system):
    engine, model, sent, now = auto_system
    turn = await question_during_pause(engine, now)
    await engine.close()
    reopened = Database(engine.config.data_dir, engine.config.jid)
    await reopened.initialize(engine.config.users)
    replacement = Dialogues(engine.config, reopened, model, engine.prepare_reply,
                            clock=lambda: now[0], wall_clock=lambda: now[0], can_resume=lambda jid: True)
    await replacement.initialize()
    try:
        now[0] += 61
        await replacement.resume_takeovers()
        assert (await reopened.dialogue(CHILD))['mode'] == 'manual' and not model.complete.called
        row = await reopened.dialogue(CHILD)
        await reopened.resume_auto(CHILD, row['revision'])
        model.complete.side_effect = [json.dumps({'certain': True, 'unanswered': [turn]}), 'Ответ после проверки архива']
        await replacement.resume_takeovers(); await idle(replacement)
        assert sent == [(CHILD, 'Ответ после проверки архива')]
    finally:
        await replacement.close()


async def test_question_before_intervention_is_recovered_and_cancelled_reply_never_returns(auto_system):
    engine, model, sent, now = auto_system
    entered = asyncio.Event()
    async def pending(messages):
        entered.set()
        await asyncio.Event().wait()
    model.complete.side_effect = pending
    await engine.submit(CHILD, 'Объясни фотосинтез')
    await asyncio.wait_for(entered.wait(), 1)
    turn = (await engine.database.dialogue(CHILD))['next_turn']
    await manual(engine, now, text='Минутку, сейчас посмотрю')
    await idle(engine)
    model.complete.side_effect = [json.dumps({'certain': True, 'unanswered': [turn]}), 'Ответ про фотосинтез']
    now[0] += 61; await engine.resume_takeovers(); await idle(engine)
    assert sent == [(CHILD, 'Ответ про фотосинтез')]
    history = await engine.database.history(CHILD, 100, 24, 30)
    assert [m['content'] for m in history] == ['Объясни фотосинтез', 'Минутку, сейчас посмотрю', 'Ответ про фотосинтез']
    model.reset_mock(); sent.clear()
    await manual(engine, now, ident='another-pause')
    now[0] += 61; await engine.resume_takeovers(); await idle(engine)
    assert not model.complete.called and not sent


async def test_wall_clock_jump_does_not_shorten_live_minute(auto_system):
    engine, model, sent, now = auto_system
    mono = [now[0]]
    engine.clock = lambda: mono[0]
    await manual(engine, now)
    now[0] += 3600
    await engine.resume_takeovers()
    assert await engine.mode(CHILD) == 'manual'
    mono[0] += 60
    await engine.resume_takeovers()
    assert await engine.mode(CHILD) == 'auto' and not model.complete.called


async def test_poller_actually_returns_without_another_message(auto_system):
    engine, model, sent, now = auto_system
    await manual(engine, now)
    now[0] += 61
    async def returned():
        while await engine.mode(CHILD) != 'auto':
            await asyncio.sleep(0.01)
    await asyncio.wait_for(returned(), 2)
    assert not sent and not model.complete.called


async def test_duplicate_question_does_not_cancel_review(auto_system):
    engine, model, sent, now = auto_system
    await manual(engine, now)
    await engine.submit(CHILD, 'Вопрос', keys=('incoming',), stamp=now[0])
    turn = (await engine.database.dialogue(CHILD))['next_turn']
    entered, release = asyncio.Event(), asyncio.Event()
    async def response(messages):
        if 'candidate_ids' in messages[-1]['content']:
            entered.set(); await release.wait()
            return json.dumps({'certain': True, 'unanswered': [turn]})
        return 'Продолжение'
    model.complete.side_effect = response
    now[0] += 61; await engine.resume_takeovers()
    await asyncio.wait_for(entered.wait(), 1)
    await engine.submit(CHILD, 'Вопрос', keys=('incoming',), stamp=now[0])
    release.set(); await idle(engine)
    assert sent == [(CHILD, 'Продолжение')] and model.complete.await_count == 2


async def test_three_recent_questions_get_one_combined_reply(auto_system):
    engine, model, sent, now = auto_system
    await manual(engine, now)
    turns = []
    for question in ('Почему?', 'Как это работает?', 'А пример?'):
        await engine.submit(CHILD, question)
        turns.append((await engine.database.dialogue(CHILD))['next_turn'])
    model.complete.side_effect = [json.dumps({'certain': True, 'unanswered': turns}), 'Общее пояснение']
    now[0] += 61; await engine.resume_takeovers(); await idle(engine)
    assert sent == [(CHILD, 'Общее пояснение')]
    context = model.complete.call_args.args[0]
    for question in ('Почему?', 'Как это работает?', 'А пример?'):
        assert sum(question in m['content'] for m in context) == 1
    assert all(str(turn) in context[-1]['content'] for turn in turns)


async def test_followup_keeps_required_exchange_without_old_full_history(auto_system):
    engine, model, sent, now = auto_system
    engine.config.llm.persona_file.write_text('Позитивный учебный помощник. ' * 60)
    for i in range(12):
        turn = await engine.database.add_user_message(CHILD, f'Старая тема {i}: ' + 'я' * 150)
        await engine.database.add_assistant_message(CHILD, turn, 'Старое пояснение: ' + 'б' * 280)
    await manual(engine, now)
    question = 'Как устроен бумажный самолёт и почему он летит?'
    await engine.submit(CHILD, question)
    turn = (await engine.database.dialogue(CHILD))['next_turn']
    await manual(engine, now, text='Первая короткая подсказка.', ident='hint1')
    await manual(engine, now, text='Вторая короткая подсказка.', ident='hint2')
    model.complete.side_effect = [json.dumps({'certain': True, 'unanswered': [turn]}), 'Продолжение объяснения.']
    now[0] += 61; await engine.resume_takeovers(); await idle(engine)
    assert sent == [(CHILD, 'Продолжение объяснения.')]
    assert model.complete.await_count == 2
    context = model.complete.call_args.args[0]
    combined = '\n'.join(m['content'] for m in context)
    assert 'Старая тема' not in combined and 'Старое пояснение' not in combined
    assert 'Первая короткая подсказка.' in combined and 'Вторая короткая подсказка.' in combined
    assert combined.count(question) == 1
    assert 'Позитивный учебный помощник.' in combined and 'Текущий класс: 2' in combined
    assert estimate_tokens(context) <= engine.config.memory.context_token_budget


async def test_followup_preserves_manual_replies_beyond_normal_history_window(auto_system):
    engine, model, sent, now = auto_system
    turn = await question_during_pause(engine, now)
    await manual(engine, now, text='Важная первая подсказка.', ident='first-hint')
    for i in range(26):
        await manual(engine, now, text=f'Короткое дополнение {i}.', ident=f'addition-{i}')
    model.complete.side_effect = [json.dumps({'certain': True, 'unanswered': [turn]}), 'Общее продолжение']
    now[0] += 61; await engine.resume_takeovers(); await idle(engine)
    assert sent == [(CHILD, 'Общее продолжение')]
    combined = '\n'.join(m['content'] for m in model.complete.call_args.args[0])
    assert 'Важная первая подсказка.' in combined and 'Короткое дополнение 25.' in combined


async def test_mandatory_followup_overflow_skips_before_paid_review(auto_system, caplog):
    engine, model, sent, now = auto_system
    engine.config.llm.persona_file.write_text('Ж' * 4090)
    await question_during_pause(engine, now)
    caplog.set_level('INFO', logger='goose')
    now[0] += 61; await engine.resume_takeovers(); await idle(engine)
    assert await engine.mode(CHILD) == 'auto'
    assert not sent and not model.complete.called
    assert 'takeover_review_skipped reason=followup_budget' in caplog.text


async def test_followup_frames_leading_manual_reply_without_losing_evidence(auto_system):
    engine, model, sent, now = auto_system
    turn = await question_during_pause(engine, now)
    await engine.restore(CHILD, ('earlier-stamp',), 'manual', 'Подсказка с более ранним временем.', now[0]-1, live=True)
    model.complete.side_effect = [json.dumps({'certain': True, 'unanswered': [turn]}), 'Продолжение']
    now[0] += 61; await engine.resume_takeovers(); await idle(engine)
    assert sent == [(CHILD, 'Продолжение')]
    conversation = [m for m in model.complete.call_args.args[0] if m['role'] != 'system']
    assert conversation[0]['role'] == 'user'
    assert any(m['content'] == 'Подсказка с более ранним временем.' for m in conversation)


async def test_legacy_six_thousand_budget_keeps_large_persona_and_manual_replies(auto_system):
    engine, model, sent, now = auto_system
    persona = ('Дружелюбный учебный помощник. ' * 100).encode()[:4029].decode('utf-8', errors='ignore')
    engine.config.llm.persona_file.write_text(persona)
    engine.config = replace(engine.config, memory=replace(engine.config.memory, context_token_budget=6000))
    turn = await question_during_pause(engine, now)
    await manual(engine, now, text='Первая подсказка.', ident='first')
    await manual(engine, now, text='Вторая подсказка.', ident='second')
    model.complete.side_effect = [json.dumps({'certain': True, 'unanswered': [turn]}), 'Продолжение']
    now[0] += 61; await engine.resume_takeovers(); await idle(engine)
    assert sent == [(CHILD, 'Продолжение')]
    context = model.complete.call_args.args[0]
    assert estimate_tokens(context) <= 6000
    assert persona.strip() in context[0]['content']
    assert 'Первая подсказка.' in str(context) and 'Вторая подсказка.' in str(context)


async def test_schema_six_upgrade_preserves_explicit_manual_usage_and_identity(config, database):
    await database.set_mode(CHILD, 'manual')
    await database.set_mode(OTHER, 'auto')
    await database.call(lambda c: c.execute("INSERT INTO omemo VALUES('fixture','{}')"))
    await database.start_api_usage('a', 'b', CHILD, 'https://api.example.org/v1', 'fixture', 1, time.time())
    await database.finish_api_usage('a', 'success', (10, 2, 12), time.time())
    await database.call(lambda c: c.execute('DROP TABLE takeover_pauses'))
    await database.call(lambda c: c.execute('DROP TABLE takeover_answers'))
    await database.call(lambda c: c.execute('PRAGMA user_version=6'))
    reopened = Database(config.data_dir, config.jid)
    await reopened.initialize(config.users)
    assert (await reopened.dialogue(CHILD))['mode'] == 'manual'
    assert (await reopened.dialogue(OTHER))['mode'] == 'auto'
    assert await reopened.due_takeovers(time.time()+1000, 60) == []
    assert await reopened.call(lambda c: c.execute('SELECT total_tokens FROM api_usage').fetchone()[0]) == 12
    assert await reopened.call(lambda c: c.execute("SELECT value FROM omemo WHERE key='fixture'").fetchone()[0]) == '{}'
    assert await reopened.call(lambda c: c.execute('PRAGMA user_version').fetchone()[0]) == 7


@pytest.mark.parametrize('text', ['{}','{"certain":false,"unanswered":[1]}',
    '{"certain":true,"unanswered":[true]}','{"certain":true,"unanswered":[99]}',
    '{"certain":true,"unanswered":[1,1]}',
    '{"certain":true,"certain":false,"unanswered":[1]}','текст {"certain":true,"unanswered":[1]}'])
def test_bad_review_is_silent(text):
    assert unanswered(text, {1: 'Вопрос'}) == ()


def test_full_json_wrapper_is_supported():
    assert unanswered('```json\n{"certain":true,"unanswered":[1]}\n```', {1: 'Вопрос'}) == (1,)


@pytest.mark.parametrize('value', ['true','-1','1','3601','"60"'])
def test_timeout_config_is_strict(config_file, value):
    config_file.write_text(config_file.read_text().replace('[interaction]', '[interaction]\ntakeover_timeout_seconds = '+value))
    with pytest.raises(ConfigError): load_config(config_file)


async def test_real_api_usage_includes_review_and_followup(auto_system, monkeypatch):
    engine, old_model, sent, now = auto_system
    turn = await question_during_pause(engine, now)
    monkeypatch.setenv('GOOSE_LLM_API_KEY', 'fixture-key')
    replies = iter([json.dumps({'certain': True, 'unanswered': [turn]}), 'Обычный ответ'])
    def response(request):
        return httpx.Response(200, json={'choices': [{'message': {'content': next(replies)}}],
                                       'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}})
    config = replace(engine.config.llm, base_url='https://api.example.org/v1', model='fixture-model')
    model = OpenAICompatible(config, transport=httpx.MockTransport(response))
    engine.model = model
    try:
        now[0] += 61; await engine.resume_takeovers(); await idle(engine)
        rows, _ = await engine.database.api_usage(now[0]-120, now[0]+120, CHILD)
        assert sum(r['attempts'] for r in rows) == 2 and sum(r['total'] for r in rows) == 24
        assert sent == [(CHILD, 'Обычный ответ')]
    finally:
        await model.close()
