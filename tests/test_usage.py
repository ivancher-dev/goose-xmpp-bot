"""Синтетические API/тарифы; без внешних запросов и семейных сообщений."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import sqlite3

import httpx
import pytest

from goose.commands import Commands
from goose.config import ConfigError, LlmConfig, UsageConfig, load_config
from goose.model import ModelError, OpenAICompatible
from goose.dialogue import Dialogues
from goose.storage import Database
from goose import storage
from goose.usage import period_bounds, token_usage, usage_report, usage_scope

PARENT, CHILD, OTHER = 'parent@example.org', 'pupil1@example.org', 'pupil2@example.org'
STAMP = datetime(2026, 10, 30, 12, tzinfo=timezone.utc).timestamp()
URL, MODEL = 'https://api.example.org/v1', 'fixture-model'


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setenv('GOOSE_LLM_API_KEY', 'fixture-secret-token')
    return LlmConfig(enabled=True, base_url=URL, model=MODEL)


def packet(usage=None, content='Синтетический ответ.'):
    data = {'choices': [{'message': {'content': content}}]}
    if usage is not None:
        data['usage'] = usage
    return data


async def rows(database):
    return await database.call(lambda db: [dict(r) for r in db.execute('SELECT * FROM api_usage ORDER BY started,id')])


@pytest.mark.parametrize('usage,expected', [
    ({'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13}, (10, 3, 13)),
    ({'prompt_tokens': 10**9, 'completion_tokens': 10**9, 'total_tokens': 2*10**9}, (10**9, 10**9, 2*10**9)),
    ({'prompt_tokens': 0, 'completion_tokens': 0}, (0, 0, 0)),
    ({'prompt_tokens': 4}, (4, None, None)),
    ({'total_tokens': 12}, (None, None, 12)),
    ({'prompt_tokens': 4, 'total_tokens': 12}, (4, None, 12)),
    ({'prompt_tokens': 4, 'completion_tokens': 2, 'prompt_tokens_details': {'cached_tokens': 3},
      'completion_tokens_details': {'reasoning_tokens': 1}}, (4, 2, 6)),
    ({'prompt_tokens': True, 'completion_tokens': 2}, (None, None, None)),
    ({'prompt_tokens': -1, 'completion_tokens': 2}, (None, None, None)),
    ({'prompt_tokens': '10', 'completion_tokens': 2}, (None, None, None)),
    ({'prompt_tokens': 1.5, 'completion_tokens': 2}, (None, None, None)),
    ({'prompt_tokens': 10**12, 'completion_tokens': 2}, (None, None, None)),
    ({'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 11}, (None, None, None)),
    ({'prompt_tokens': 10, 'total_tokens': 2}, (None, None, None)),
    ({}, (None, None, None)), (None, (None, None, None)),
    ([], (None, None, None))])
def test_usage_counters_are_optional_validated_and_not_double_counted(usage, expected):
    assert token_usage(json.dumps(packet(usage))) == expected


async def test_success_is_persisted_once_and_reports_do_not_call_model(config, database, llm):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=packet({'prompt_tokens': 12, 'completion_tokens': 3, 'total_tokens': 15}))
    model = OpenAICompatible(llm, transport=httpx.MockTransport(handler), wall_clock=lambda: STAMP)
    try:
        with usage_scope(database, CHILD):
            assert await model.complete([{'role': 'user', 'content': 'private-fixture-body'}]) == 'Синтетический ответ.'
        saved = await rows(database)
        assert len(saved) == 1 and saved[0]['jid'] == CHILD and saved[0]['outcome'] == 'success'
        assert (saved[0]['prompt_tokens'], saved[0]['completion_tokens'], saved[0]['total_tokens']) == (12, 3, 15)
        assert 'private-fixture-body' not in str(saved) and 'fixture-secret-token' not in str(saved)
        commands = Commands(config, database, clock=lambda: STAMP)
        report = await commands.handle(PARENT+'/desktop', f'!admin usage {CHILD} today')
        assert 'попыток API: 1' in report and 'всего — 15' in report and OTHER not in report
        assert len(calls) == 1
        before = await rows(database)
        assert 'владельцу' in await commands.handle(CHILD, '!admin usage all month')
        assert await commands.handle('outside@example.org', '!admin usage') is None
        assert await rows(database) == before
        await database.clear_context(CHILD)
        assert await rows(database) == before
        reopened = Database(config.data_dir, config.jid)
        await reopened.initialize(config.users)
        assert await rows(reopened) == before
    finally:
        await model.close()


@pytest.mark.parametrize('usage', [None, {'prompt_tokens': 'bad'}, {'prompt_tokens': 11}, {'total_tokens': 20}])
async def test_unknown_or_partial_usage_keeps_answer_and_does_not_claim_zero(config, database, llm, usage):
    model = OpenAICompatible(llm, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=packet(usage))), wall_clock=lambda: STAMP)
    try:
        with usage_scope(database, CHILD):
            assert await model.complete([]) == 'Синтетический ответ.'
        report = await usage_report(config, database, CHILD, now=STAMP)
        assert 'Полные данные о токенах: 0 из 1' in report
        assert 'нет данных' in report
    finally:
        await model.close()


async def test_retries_are_two_attempts_of_one_call_with_unknown_first_charge(config, database, llm):
    count = 0
    def handler(_):
        nonlocal count
        count += 1
        return httpx.Response(503) if count == 1 else httpx.Response(200, json=packet({'prompt_tokens': 5, 'completion_tokens': 2}))
    async def no_sleep(_): pass
    model = OpenAICompatible(llm, transport=httpx.MockTransport(handler), sleep=no_sleep, wall_clock=lambda: STAMP)
    try:
        with usage_scope(database, CHILD):
            await model.complete([])
        saved = await rows(database)
        assert len(saved) == 2 and len({r['call_id'] for r in saved}) == 1
        assert {r['outcome'] for r in saved} == {'http_error', 'success'}
        report = await usage_report(config, database, CHILD, now=STAMP)
        assert 'Обращений к модели: 1; попыток API: 2; повторных: 1' in report
        assert '1 из 2 попыток' in report
    finally:
        await model.close()


@pytest.mark.parametrize('case,outcome', [('authentication', 'authentication'), ('invalid', 'invalid_response'),
    ('timeout', 'timeout'), ('connect', 'network')])
async def test_failed_attempt_is_not_erased_or_replaced_with_zero(database, llm, case, outcome):
    def handler(_):
        if case == 'timeout': raise httpx.ReadTimeout('private-fixture')
        if case == 'connect': raise httpx.ConnectError('private-fixture')
        return httpx.Response(401) if case == 'authentication' else httpx.Response(200, json=packet({'prompt_tokens': 2, 'completion_tokens': 3}, content=None))
    model = OpenAICompatible(replace(llm, max_attempts=1), transport=httpx.MockTransport(handler), wall_clock=lambda: STAMP)
    try:
        with usage_scope(database, CHILD), pytest.raises(ModelError):
            await model.complete([])
        saved = await rows(database)
        assert len(saved) == 1 and saved[0]['outcome'] == outcome
        assert saved[0]['total_tokens'] == (5 if case == 'invalid' else None)
    finally:
        await model.close()


async def test_cancelled_request_is_counted_and_diagnostic_without_scope_is_excluded(database, llm):
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(_):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=packet())
    model = OpenAICompatible(llm, transport=httpx.MockTransport(handler), wall_clock=lambda: STAMP)
    try:
        with usage_scope(database, CHILD):
            task = asyncio.create_task(model.complete([]))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert (await rows(database))[0]['outcome'] == 'cancelled'
        release.set()
        await model.complete([])
        assert len(await rows(database)) == 1
    finally:
        release.set()
        await model.close()


async def test_parallel_accounts_share_transport_but_not_accounting(config, database, llm):
    barrier = asyncio.Event()
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 2: barrier.set()
        await barrier.wait()
        value = int(json.loads(request.content)['messages'][0]['content'])
        assert '@example.org' not in request.content.decode()
        return httpx.Response(200, json=packet({'prompt_tokens': value, 'completion_tokens': 1}))
    model = OpenAICompatible(llm, transport=httpx.MockTransport(handler), wall_clock=lambda: STAMP)
    async def invoke(jid, value):
        with usage_scope(database, jid):
            await model.complete([{'role': 'user', 'content': str(value)}])
    try:
        await asyncio.wait_for(asyncio.gather(invoke(CHILD, 10), invoke(OTHER, 20)), 3)
        saved = {r['jid']: r['prompt_tokens'] for r in await rows(database)}
        assert saved == {CHILD: 10, OTHER: 20}
        assert OTHER not in await usage_report(config, database, CHILD, now=STAMP)
    finally:
        await model.close()


async def test_local_rate_limit_does_not_add_an_api_attempt(database, llm):
    model = OpenAICompatible(replace(llm, requests_per_minute=1), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=packet())), wall_clock=lambda: STAMP)
    try:
        with usage_scope(database, CHILD):
            await model.complete([])
            with pytest.raises(ModelError, match='rate_limited'):
                await model.complete([])
        assert len(await rows(database)) == 1
    finally:
        await model.close()


async def test_pricing_is_an_explicit_current_tariff_for_matching_provider_and_model(config, database):
    await database.start_api_usage('a', 'call', CHILD, URL, MODEL, 1, STAMP)
    await database.finish_api_usage('a', 'success', (10000, 1000, 11000), STAMP)
    pricing = UsageConfig(MODEL, 'RUB', Decimal('100'), Decimal('200'))
    configured = replace(config, llm=replace(config.llm, base_url=URL), usage=pricing)
    report = await usage_report(configured, database, CHILD, now=STAMP)
    assert '≈ 1,2 RUB' in report and 'Без оценки: 0' in report and 'текущему тарифу' in report
    different = replace(configured, usage=replace(pricing, price_model='different-model'))
    unpriced = await usage_report(different, database, CHILD, now=STAMP)
    assert 'Без оценки: 1' in unpriced and '≈ 0' not in unpriced
    assert 'тариф не задан' in await usage_report(config, database, CHILD, now=STAMP)


@pytest.mark.parametrize('options', ['price_model="x"\ninput_per_million="1"',
    'input_per_million="1"\noutput_per_million="2"',
    'price_model="x"\ninput_per_million=true\noutput_per_million="2"',
    'price_model="x"\ninput_per_million="NaN"\noutput_per_million="2"',
    'price_model="x"\ninput_per_million="-1"\noutput_per_million="2"',
    'currency="ruble"', 'daily_limit=5'])
def test_invalid_or_incomplete_pricing_does_not_silently_estimate(config_file, options):
    config_file.write_text(config_file.read_text()+'\n[usage]\n'+options+'\n')
    with pytest.raises(ConfigError): load_config(config_file)


def test_explicit_pricing_config_and_calendar_periods(config_file):
    config_file.write_text(config_file.read_text()+'\n[usage]\nprice_model="x"\ninput_per_million="100"\noutput_per_million=200\n')
    assert load_config(config_file).usage.input_per_million == Decimal(100)
    start, end, _ = period_bounds('2026-03-29', 'Europe/Berlin')
    assert end-start == 23*3600
    start, end, _ = period_bounds('2026-10-25', 'Europe/Berlin')
    assert end-start == 25*3600
    _, _, label = period_bounds('today', 'Pacific/Kiritimati', datetime(2026,10,30,18,tzinfo=timezone.utc).timestamp())
    assert label == '31.10.2026'
    start, end, _ = period_bounds('2026-02', 'Europe/Moscow')
    assert end-start == 28*86400
    for value in ('2026-02-30','2026-13','9999-12','9999-12-31','month;DROP'):
        with pytest.raises(ConfigError): period_bounds(value, 'Europe/Moscow')


async def test_finish_is_idempotent_and_restart_marks_inflight_usage_unknown(config, database):
    await database.start_api_usage('a', 'call', CHILD, URL, MODEL, 1, STAMP)
    await database.finish_api_usage('a', 'success', (1, 2, 3), STAMP)
    await database.finish_api_usage('a', 'success', (100, 200, 300), STAMP)
    assert (await rows(database))[0]['total_tokens'] == 3
    await database.start_api_usage('b', 'call2', OTHER, URL, MODEL, 1, STAMP)
    reopened = Database(config.data_dir, config.jid)
    await reopened.initialize(config.users)
    saved = {r['id']:r for r in await rows(reopened)}
    assert saved['b']['outcome'] == 'interrupted' and saved['b']['total_tokens'] is None


async def test_old_unfinished_attempts_are_bounded_and_late_known_usage_can_be_recorded(database):
    await database.start_api_usage('old', 'c1', CHILD, URL, MODEL, 1, STAMP-7200)
    await database.start_api_usage('new', 'c2', CHILD, URL, MODEL, 1, STAMP)
    saved = {r['id']:r for r in await rows(database)}
    assert saved['old']['outcome'] == 'interrupted' and saved['old']['total_tokens'] is None
    assert saved['new']['outcome'] == 'pending'
    await database.finish_api_usage('old', 'success', (1,2,3), STAMP)
    assert (await database.api_usage(0, STAMP+1, CHILD))[0][0]['total'] == 3


async def test_bounded_storage_discloses_partial_coverage(config, database, monkeypatch):
    monkeypatch.setattr(storage, 'MAX_ROWS', 2)
    for i in range(4):
        await database.start_api_usage(str(i), str(i), CHILD, URL, MODEL, 1, STAMP+i)
        await database.finish_api_usage(str(i), 'success', (1,1,2), STAMP+i)
    assert len(await rows(database)) == 2
    _, metadata = await database.api_usage(0, STAMP+10)
    assert metadata['cutoff'] == STAMP+1
    await database.call(lambda db: db.execute("UPDATE metadata SET value=? WHERE key='usage_started_at'", (str(STAMP-2*86400),)))
    assert 'Период покрыт не полностью' in await usage_report(config, database, CHILD, now=STAMP)


async def test_schema_five_to_six_preserves_old_rows_and_failure_rolls_back(config, database):
    await database.update_language(CHILD, 'enabled', True)
    await database.add_user_message(CHILD, 'Синтетическая сохранённая история')
    await database.call(lambda db: db.execute('DROP TABLE api_usage'))
    await database.call(lambda db: db.execute('PRAGMA user_version=5'))
    before = await database.call(lambda db: {name: [tuple(r) for r in db.execute('SELECT * FROM '+name)]
        for name in ('metadata','profiles','omemo','history','dialogues','language_feedback')})
    reopened = Database(config.data_dir, config.jid)
    await reopened.initialize(config.users)
    after = await database.call(lambda db: {name: [tuple(r) for r in db.execute('SELECT * FROM '+name)] for name in before})
    assert after == before and not await rows(database)
    assert await database.call(lambda db: db.execute('PRAGMA user_version').fetchone()[0]) == 6
    await database.call(lambda db: db.execute('DROP TABLE api_usage'))
    await database.call(lambda db: db.execute('PRAGMA user_version=5'))
    await database.call(lambda db: db.execute("CREATE TRIGGER fail_usage_migration BEFORE INSERT ON profiles BEGIN SELECT RAISE(ABORT,'fixture'); END"))
    with pytest.raises(sqlite3.IntegrityError): await reopened.initialize(config.users)
    assert await database.call(lambda db: db.execute('PRAGMA user_version').fetchone()[0]) == 5
    assert await database.call(lambda db: db.execute("SELECT 1 FROM sqlite_master WHERE name='api_usage'").fetchone()) is None


async def test_recording_failure_prevents_untracked_call_but_finish_failure_keeps_answer(database, llm, monkeypatch, caplog):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=packet({'prompt_tokens': 5, 'completion_tokens': 2}))
    model = OpenAICompatible(llm, transport=httpx.MockTransport(handler), wall_clock=lambda: STAMP)
    original = database.start_api_usage
    async def broken(*args, **kwargs): raise OSError('private-fixture-error')
    try:
        with caplog.at_level('ERROR', logger='goose'):
            monkeypatch.setattr(database, 'start_api_usage', broken)
            with usage_scope(database, CHILD), pytest.raises(ModelError, match='unavailable'):
                await model.complete([])
            assert not calls
            monkeypatch.setattr(database, 'start_api_usage', original)
            monkeypatch.setattr(database, 'finish_api_usage', broken)
            with usage_scope(database, CHILD):
                assert await model.complete([]) == 'Синтетический ответ.'
        assert len(calls) == 1 and (await rows(database))[0]['outcome'] == 'pending'
        assert 'phase=start' in caplog.text and 'phase=finish' in caplog.text
        assert 'private-fixture' not in caplog.text and 'fixture-secret-token' not in caplog.text
    finally:
        await model.close()


async def test_cancel_after_reservation_before_http_does_not_claim_a_sent_request(config, database, llm, monkeypatch):
    original = database.start_api_usage
    async def cancelled(*args):
        await original(*args)
        raise asyncio.CancelledError()
    monkeypatch.setattr(database, 'start_api_usage', cancelled)
    def forbidden(_): raise AssertionError('HTTP must not start')
    model = OpenAICompatible(llm, transport=httpx.MockTransport(forbidden), wall_clock=lambda: STAMP)
    try:
        with usage_scope(database, CHILD), pytest.raises(asyncio.CancelledError): await model.complete([])
        assert (await rows(database))[0]['outcome'] == 'not_sent'
        assert 'попыток API: 0' in await usage_report(config, database, CHILD, now=STAMP)
    finally:
        await model.close()


async def test_paid_response_counts_even_when_manual_cancels_delivery(config, database, llm, tmp_path):
    persona = tmp_path/'usage-persona.md'
    persona.write_text('Тестовый образ без личных данных.')
    cfg = replace(config, llm=replace(llm, persona_file=persona))
    model = OpenAICompatible(cfg.llm, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=packet({'prompt_tokens': 4, 'completion_tokens': 2}))), wall_clock=lambda: STAMP)
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    async def prepare(jid, text):
        entered.set()
        await release.wait()
        return lambda: sent.append((jid, text)) or True
    engine = Dialogues(cfg, database, model, prepare)
    await engine.initialize()
    try:
        await engine.set_mode(CHILD, 'auto')
        await engine.submit(CHILD, 'Синтетический вопрос')
        await asyncio.wait_for(entered.wait(), 3)
        await engine.set_mode(CHILD, 'manual')
        release.set()
        if engine.tasks:
            await asyncio.gather(*tuple(engine.tasks), return_exceptions=True)
        assert not sent
        saved = await rows(database)
        assert len(saved) == 1 and saved[0]['jid'] == CHILD and saved[0]['total_tokens'] == 6
        assert saved[0]['outcome'] == 'success'
        commands = Commands(cfg, database, dialogues=engine, clock=lambda: STAMP)
        await commands.handle(CHILD, '!ping')
        await engine.submit(CHILD, 'В manual модель не вызывается')
        await engine.restore(CHILD, ('archive-usage-test',), 'incoming', 'Архивная запись', 1)
        assert len(await rows(database)) == 1
    finally:
        release.set()
        await engine.close()
