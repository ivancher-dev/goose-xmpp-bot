import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from goose.config import ConfigError, WeatherConfig, load_config
from goose.commands import Commands
from goose.facts import Facts, FactsError, weather_report, wttr_weather_report
from goose.storage import Database
from goose.weather_alerts import WeatherAlerts, due_slots, parse_times
from goose.weather_view import Forecast, WeatherReport, weather_advice
from test_facts import forecast
from test_wttr import payload, PLACE, NOW, PARENT, CHILD


@pytest.mark.parametrize('failure', ['http', 'timeout', 'html', 'stale', 'redirect', 'oversize'])
async def test_primary_failure_uses_independent_fallback(config, database, failure):
    calls = []
    def route(request):
        calls.append(request.url.host)
        if request.url.host == 'wttr.in':
            return httpx.Response(200, json=payload())
        if failure == 'timeout':
            raise httpx.ReadTimeout('private-details')
        if failure == 'html':
            return httpx.Response(200, text='<html>error</html>')
        if failure == 'stale':
            data = forecast()
            data['current']['time'] = '2026-10-25T15:00'
            return httpx.Response(200, json=data)
        if failure == 'redirect':
            return httpx.Response(302, headers={'location': 'https://foreign.example.org'})
        if failure == 'oversize':
            return httpx.Response(200, headers={'content-length': '9999999'})
        return httpx.Response(503)
    engine = Facts(replace(config, weather=WeatherConfig(True)), database, None,
                   transport=httpx.MockTransport(route), now=lambda: NOW)
    try:
        report = await engine.get_weather(PLACE)
        assert report.source == 'wttr.in' and report.forecast.maximum == 12
        assert calls == ['api.open-meteo.com', 'wttr.in']
        assert 'Open-Meteo' not in report.render()
        # На две минуты не стучимся к только что отказавшему источнику.
        await engine.get_weather(PLACE)
        assert len(calls) == 2
    finally:
        await engine.close()


async def test_reverse_order_recovery_and_expired_cooldown(config, database):
    calls, clock = [], [0]
    def route(request):
        calls.append(request.url.host)
        return httpx.Response(503) if request.url.host == 'wttr.in' else httpx.Response(200, json=forecast())
    engine = Facts(replace(config, weather=WeatherConfig(True, 'wttr')), database, None,
                   transport=httpx.MockTransport(route), now=lambda: NOW, clock=lambda: clock[0])
    try:
        assert (await engine.get_weather(PLACE)).source == 'Open-Meteo'
        assert calls == ['wttr.in', 'api.open-meteo.com']
        clock[0] = 121
        await engine.get_weather(PLACE)
        assert calls[-1] == 'wttr.in' and len(calls) == 3
    finally:
        await engine.close()


async def test_recent_failure_only_changes_priority_and_does_not_remove_fallback(config, database):
    calls, recovered = [], [False]
    def route(request):
        host = request.url.host
        calls.append(host)
        if host == 'api.open-meteo.com':
            return httpx.Response(200, json=forecast()) if recovered[0] else httpx.Response(503)
        return httpx.Response(503) if recovered[0] else httpx.Response(200, json=payload())
    engine = Facts(replace(config, weather=WeatherConfig(True)), database, None,
                   transport=httpx.MockTransport(route), now=lambda: NOW)
    try:
        assert (await engine.get_weather(PLACE)).source == 'wttr.in'
        recovered[0] = True
        engine.cache.clear()
        assert (await engine.get_weather(PLACE)).source == 'Open-Meteo'
        assert calls == ['api.open-meteo.com', 'wttr.in', 'wttr.in', 'api.open-meteo.com']
    finally:
        await engine.close()


async def test_real_source_deadline_leaves_time_for_fallback(config, database, monkeypatch):
    monkeypatch.setattr('goose.facts.WEATHER_SOURCE_TIMEOUT', .01)
    async def route(request):
        if request.url.host == 'api.open-meteo.com':
            await asyncio.Event().wait()
        return httpx.Response(200, json=payload())
    engine = Facts(replace(config, weather=WeatherConfig(True)), database, None,
                   transport=httpx.MockTransport(route), now=lambda: NOW)
    try:
        assert (await engine.get_weather(PLACE)).source == 'wttr.in'
    finally:
        await engine.close()


async def test_all_fail_no_stale_weather_or_private_exception(config, database):
    engine = Facts(replace(config, weather=WeatherConfig(True)), database, None,
                   transport=httpx.MockTransport(lambda r: httpx.Response(503)), now=lambda: NOW)
    try:
        with pytest.raises(FactsError, match='свежий прогноз'):
            await engine.get_weather(PLACE)
    finally:
        await engine.close()


def test_pool_is_configurable_and_legacy_provider_is_preserved(config_file):
    config_file.write_text(config_file.read_text().replace('provider = "open_meteo"',
                           'provider = "wttr"\nfallback_providers = ["open_meteo"]'))
    assert load_config(config_file).weather.providers == ('wttr', 'open_meteo')
    for value in ('["wttr"]', '["foreign"]', '["open_meteo", "open_meteo"]', 'null', '"open_meteo"'):
        text = config_file.read_text().replace('["open_meteo"]', value)
        config_file.write_text(text)
        with pytest.raises(ConfigError):
            load_config(config_file)
        config_file.write_text(text.replace(value, '["open_meteo"]'))
    config_file.write_text(config_file.read_text().replace('["open_meteo"]', '[]'))
    assert load_config(config_file).weather.providers == ('wttr',)


@pytest.mark.parametrize('minimum,wind,hat', [(7.9, 5, True), (8, 5, False), (7, 4.9, False), (7, None, False)])
def test_hat_requires_both_cold_and_wind(minimum, wind, hat):
    tips = ' '.join(weather_advice(Forecast('облачно', minimum, 14, 80, 'осадков', wind)))
    assert ('шапку' in tips) == hat
    assert 'зонт' not in tips  # неопределённые осадки не объявляются дождём


def test_rain_and_snow_advice_uses_provider_data():
    data = forecast()
    data['daily']['weather_code'][0] = 71
    data['daily']['precipitation_probability_max'][0] = 90
    assert 'зонт' not in weather_report(data, PLACE, False, NOW).render(advice=True)
    data['daily']['weather_code'][0] = 61
    assert 'зонт' in weather_report(data, PLACE, False, NOW).render(advice=True)
    assert 'зонт' in wttr_weather_report(payload(), PLACE, False, NOW).render(advice=True)


@pytest.mark.parametrize('words,expected', [
    (['утром'], {'утром': '08:00'}),
    (['утром', 'днем', 'вечером'], {'утром': '08:00', 'днём': '13:00', 'вечером': '18:00'}),
    (['утром', '08:30', 'вечером', '18:15'], {'утром': '08:30', 'вечером': '18:15'}),
    (['08:15', '18:45'], {'утром': '08:15', 'вечером': '18:45'}),
    (['off'], {}),
])
def test_select_count_and_times(words, expected):
    assert parse_times(words) == expected


@pytest.mark.parametrize('words', [['утром', '25:00'], ['утром', 'утром'], ['08:00', '09:00'],
                                 ['утром', 'днём', '13:00', 'вечером', '13:00'], ['child@example.org']])
def test_invalid_schedules_do_not_silently_reinterpret_input(words):
    with pytest.raises(ConfigError):
        parse_times(words)


@pytest.fixture
def setup_alerts(config, database):
    config = replace(config, weather=WeatherConfig(True))
    now, sent = [datetime(2026, 10, 26, 4, 50, tzinfo=timezone.utc)], []
    report = WeatherReport('Город', '2026-10-26', ZoneInfo('Europe/Moscow'),
                           Forecast('дождь', 5, 10, 70, 'дождя', 7, rain_expected=True), 'Test', 'https://example.org')
    facts = Facts(config, database, None, now=lambda: now[0])
    facts.get_weather = AsyncMock(return_value=report)
    async def prepare(jid, body):
        return lambda: sent.append((jid, body))
    alerts = WeatherAlerts(config, database, facts, prepare, now=lambda: now[0])
    facts.alerts = alerts
    return alerts, facts, now, sent


async def finish_jobs(alerts):
    await asyncio.gather(*list(alerts.jobs.values()))
    await asyncio.sleep(0)


async def test_persistent_self_settings_replace_count_and_isolate_users(setup_alerts, database):
    alerts, facts, now, sent = setup_alerts
    await database.set_weather_location(PARENT, PLACE)
    text = await facts.answer(PARENT, 'weather', 'notify утром вечером')
    assert '08:00' in text and '18:00' in text and 'Europe/Moscow' in text
    assert await database.weather_subscription(CHILD) is None
    await facts.answer(PARENT, 'weather', 'notify утром 08:15')
    reopened = Database(database.path.parent, database.account)
    await reopened.initialize({})
    assert (await reopened.weather_subscription(PARENT))['times'] == {'утром': '08:15'}
    await database.clear_context(PARENT)
    assert (await database.weather_subscription(PARENT))['times'] == {'утром': '08:15'}
    await facts.answer(PARENT, 'weather', 'notify off')
    assert not (await database.weather_subscription(PARENT))['times']
    await facts.close()


async def test_own_city_required_and_foreign_jid_cannot_set_subscription(setup_alerts, database):
    alerts, facts, now, sent = setup_alerts
    assert 'Сначала задай' in await alerts.configure(PARENT, ['утром'])
    await database.set_weather_location(PARENT, PLACE)
    assert 'Рассылка погоды:' in await alerts.configure(PARENT, ['утром'])
    assert 'списка' in await alerts.configure('unknown@example.org', ['утром'])
    assert 'Рассылка погоды:' in await alerts.configure(CHILD, [PARENT, 'утром'])
    assert await database.weather_subscription(CHILD) is None
    await facts.close()


async def test_admin_configures_an_allowed_peer_and_child_cannot(setup_alerts, database):
    alerts, facts, now, sent = setup_alerts
    commands = Commands(alerts.config, database)
    commands.facts = facts
    await database.set_weather_location(CHILD, PLACE)
    reply = await commands.handle(CHILD, f'!admin weather {PARENT} утром')
    assert 'владельцу' in reply and await database.weather_subscription(PARENT) is None
    assert '08:00' in await commands.handle(PARENT, f'!admin weather {CHILD} утром')
    assert '18:00' in await commands.handle(PARENT, f'!admin weather {CHILD} вечером')
    assert (await database.weather_subscription(CHILD))['times'] == {'вечером': '18:00'}
    assert 'списка' in await commands.handle(PARENT, '!admin weather foreign@example.org утром')
    await facts.close()


async def test_old_city_gets_timezone_once_before_enabling_notifications(setup_alerts, database):
    alerts, facts, now, sent = setup_alerts
    await database.set_weather_location(PARENT, {k: v for k, v in PLACE.items() if k != 'timezone'})
    facts.geocode = AsyncMock(return_value=PLACE)
    assert 'Europe/Moscow' in await alerts.configure(PARENT, ['утром'])
    assert (await database.weather_location(PARENT))['timezone'] == 'Europe/Moscow'
    await alerts.configure(PARENT, ['вечером'])
    facts.geocode.assert_awaited_once_with('#10')
    await facts.close()


async def test_timezone_lookup_cannot_overwrite_a_concurrently_changed_city(setup_alerts, database):
    alerts, facts, now, sent = setup_alerts
    await database.set_weather_location(PARENT, {k: v for k, v in PLACE.items() if k != 'timezone'})
    started, finish = asyncio.Event(), asyncio.Event()
    async def lookup(query):
        started.set()
        await finish.wait()
        return PLACE
    facts.geocode = lookup
    task = asyncio.create_task(alerts.configure(PARENT, ['утром']))
    await asyncio.wait_for(started.wait(), 1)
    new = {**PLACE, 'name': 'Другой город', 'timezone': 'Asia/Yekaterinburg'}
    await database.set_weather_location(PARENT, new)
    finish.set()
    assert 'Город изменился' in await task
    assert await database.weather_location(PARENT) == new
    assert await database.weather_subscription(PARENT) is None
    await facts.close()


async def test_failed_timezone_lookup_leaves_subscription_unchanged(setup_alerts, database):
    alerts, facts, now, sent = setup_alerts
    await database.set_weather_location(PARENT, {k: v for k, v in PLACE.items() if k != 'timezone'})
    facts.geocode = AsyncMock(side_effect=httpx.ReadTimeout('private'))
    assert 'не изменены' in await alerts.configure(PARENT, ['утром'])
    assert await database.weather_subscription(PARENT) is None
    await facts.close()


async def test_delivers_in_manual_at_city_time_once_across_restart(setup_alerts, database):
    alerts, facts, now, sent = setup_alerts
    await database.set_weather_location(PARENT, PLACE)
    await alerts.configure(PARENT, ['утром'])
    assert (await database.dialogue(PARENT))['mode'] == 'manual'
    now[0] = datetime(2026, 10, 26, 5, tzinfo=timezone.utc)  # 08:00 Москва
    await alerts.tick()
    await finish_jobs(alerts)
    assert len(sent) == 1 and sent[0][0] == PARENT
    assert 'Доброе утро!' in sent[0][1] and 'зонт' in sent[0][1] and 'шапку' in sent[0][1]
    await alerts.close()
    other = WeatherAlerts(alerts.config, database, facts, alerts.prepare_reply, now=lambda: now[0])
    await other.tick()
    await finish_jobs(other)
    assert len(sent) == 1 and facts.get_weather.await_count == 1
    await other.close()
    await facts.close()


async def test_different_cities_and_disconnect_have_no_backlog(setup_alerts, database):
    alerts, facts, now, sent = setup_alerts
    await database.set_weather_location(PARENT, PLACE)
    await database.set_weather_location(CHILD, {**PLACE, 'timezone': 'Asia/Yekaterinburg'})
    await alerts.configure(PARENT, ['утром'])
    await alerts.configure(CHILD, ['утром'])
    now[0] = datetime(2026, 10, 26, 5, tzinfo=timezone.utc)
    alerts.ready = lambda: False
    await alerts.tick()
    assert not alerts.jobs and not sent
    now[0] += timedelta(minutes=3)
    alerts.ready = lambda: True
    await alerts.tick()
    await finish_jobs(alerts)
    assert [jid for jid, text in sent] == [PARENT]  # Екатеринбург 10:03, утро не догоняем
    await alerts.close()
    await facts.close()


@pytest.mark.parametrize('change', ['off', 'city', 'disconnect'])
async def test_change_during_encryption_prevents_outdated_notification(setup_alerts, database, change):
    alerts, facts, now, sent = setup_alerts
    await database.set_weather_location(PARENT, PLACE)
    await alerts.configure(PARENT, ['утром'])
    now[0] = datetime(2026, 10, 26, 5, tzinfo=timezone.utc)
    started, finish = asyncio.Event(), asyncio.Event()
    original = alerts.prepare_reply
    async def prepare(jid, body):
        started.set()
        await finish.wait()
        return await original(jid, body)
    alerts.prepare_reply = prepare
    await alerts.tick()
    await asyncio.wait_for(started.wait(), 1)
    if change == 'off':
        await alerts.configure(PARENT, ['off'])
    elif change == 'city':
        await database.set_weather_location(PARENT, {**PLACE, 'name': 'Другой'})
    else:
        alerts.ready = lambda: False
    finish.set()
    await finish_jobs(alerts)
    assert not sent
    await alerts.close()
    await facts.close()


async def test_source_failure_sends_one_short_notice_without_advice(setup_alerts, database):
    alerts, facts, now, sent = setup_alerts
    await database.set_weather_location(PARENT, PLACE)
    await alerts.configure(PARENT, ['утром'])
    facts.get_weather.side_effect = FactsError('private diagnostic')
    now[0] = datetime(2026, 10, 26, 5, tzinfo=timezone.utc)
    await alerts.tick()
    await finish_jobs(alerts)
    await alerts.tick()
    await finish_jobs(alerts)
    assert len(sent) == 1 and 'прогноз' in sent[0][1]
    assert not any(word in sent[0][1] for word in ('зонт', 'шапку', 'private'))
    await alerts.close()
    await facts.close()


async def test_shutdown_cancels_pending_forecast_and_never_sends(setup_alerts, database):
    alerts, facts, now, sent = setup_alerts
    await database.set_weather_location(PARENT, PLACE)
    await alerts.configure(PARENT, ['утром'])
    started = asyncio.Event()
    async def slow(place):
        started.set()
        await asyncio.Event().wait()
    facts.get_weather.side_effect = slow
    now[0] = datetime(2026, 10, 26, 5, tzinfo=timezone.utc)
    await alerts.tick()
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(alerts.close(), 1)
    assert not sent and not alerts.jobs
    await facts.close()


def test_dst_gap_fold_and_midnight_are_bounded():
    zone = ZoneInfo('America/New_York')
    sub = {'times': {'утром': '02:30'}, 'changed': 0}
    assert not list(due_slots(sub, zone, datetime(2026, 3, 8, 7, 31, tzinfo=timezone.utc)))
    sub['times']['утром'] = '01:30'
    assert list(due_slots(sub, zone, datetime(2026, 11, 1, 5, 31, tzinfo=timezone.utc)))
    assert not list(due_slots(sub, zone, datetime(2026, 11, 1, 6, 31, tzinfo=timezone.utc)))
    sub['times']['утром'] = '23:58'
    assert list(due_slots(sub, ZoneInfo('UTC'), datetime(2026, 10, 27, 0, 1, tzinfo=timezone.utc)))[0][1] == '2026-10-26'


async def test_schema_7_upgrade_preserves_settings_keys_and_restorable_delivery_marks(setup_alerts, database):
    alerts, facts, now, sent = setup_alerts
    await database.set_weather_location(PARENT, PLACE)
    await database.call(lambda db: db.execute("INSERT INTO omemo VALUES('synthetic','123')"))
    await database.call(lambda db: db.execute('DROP TABLE weather_subscriptions'))
    await database.call(lambda db: db.execute('DROP TABLE weather_deliveries'))
    await database.call(lambda db: db.execute('PRAGMA user_version=7'))
    await database.initialize({})
    assert await database.weather_location(PARENT) == PLACE
    assert await database.call(lambda db: db.execute("SELECT value FROM omemo WHERE key='synthetic'").fetchone()[0]) == '123'
    await alerts.configure(PARENT, ['утром'])
    subscription = await database.weather_subscription(PARENT)
    assert await database.claim_weather_delivery(PARENT, 'утром', '2026-10-26', subscription['revision'], now[0].timestamp())
    from goose.backup import sqlite_copy
    clone = database.path.parent / 'clone'
    clone.mkdir(mode=0o700)
    sqlite_copy(database.path, clone / 'goose.sqlite3')
    restored = Database(clone, database.account)
    await restored.initialize({})
    assert (await restored.weather_subscription(PARENT))['times'] == {'утром': '08:00'}
    assert not await restored.claim_weather_delivery(PARENT, 'утром', '2026-10-26', subscription['revision'], now[0].timestamp())
    await facts.close()
