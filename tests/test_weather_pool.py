import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import gzip
import json
from zoneinfo import ZoneInfo

import httpx
import pytest

from goose.config import ConfigError, WeatherConfig, load_config
from goose.facts import Facts, FactsError
from goose.storage import Database
from goose.weather_sources import (WEATHER_PROVIDERS, met_no_report, seven_timer_report, yandex_report)
from test_weather_alerts import setup_alerts, finish_jobs
from test_wttr import PLACE, PARENT

NOW = datetime(2026, 11, 5, 5, tzinfo=timezone.utc)


def met_data():
    start = NOW.replace(hour=4)
    return {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [37., 55., 100]},
            'properties': {'meta': {'updated_at': (NOW - timedelta(minutes=10)).isoformat(),
                'units': {'air_temperature': 'celsius', 'wind_speed': 'm/s'}},
            'timeseries': [{'time': (start + timedelta(hours=i)).isoformat(), 'data': {
                'instant': {'details': {'air_temperature': 3 + i % 7, 'wind_speed': 6}},
                'next_1_hours': {'summary': {'symbol_code': 'lightrain_day'},
                                  'details': {'precipitation_amount': .1}}}} for i in range(72)]}}


def seven_data():
    return {'product': 'civil', 'init': '2026110500', 'dataseries': [
        {'timepoint': i, 'temp2m': 3 + i % 7, 'wind10m': {'direction': 'NW', 'speed': 3},
         'weather': 'lightrainday', 'prec_type': 'rain'} for i in range(0, 73, 3)]}


def yandex_data(place=PLACE):
    zone = ZoneInfo(place['timezone'])
    local = NOW.astimezone(zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return {'data': {'serverTime': NOW.isoformat(), 'weatherByPoint': {
        'now': {'temperature': 4, 'feelsLike': 2, 'windSpeed': 6, 'condition': 'RAIN'},
        'forecast': {'days': [{'time': (start + timedelta(days=d)).isoformat(), 'hours': [
            {'time': (start + timedelta(days=d, hours=h)).isoformat(), 'temperature': 3 + h % 7,
             'windSpeed': 6, 'condition': 'RAIN'} for h in range(24)]} for d in range(2)]}}}}


def make_engine(config, database, route, **kwargs):
    return Facts(replace(config, weather=kwargs.pop('weather', WeatherConfig(True))), database, None,
                 transport=httpx.MockTransport(route), now=kwargs.pop('now', lambda: NOW), **kwargs)


def test_default_pool_has_five_distinct_sources_and_explicit_order_is_valid(config_file):
    config = load_config(config_file)
    assert len(config.weather.providers) == 5 and set(config.weather.providers) == set(WEATHER_PROVIDERS)
    config_file.write_text(config_file.read_text().replace('provider = "open_meteo"',
        'provider = "yandex"\nfallback_providers = ["wttr", "met_no", "seven_timer", "open_meteo"]'))
    assert load_config(config_file).weather.providers == ('yandex', 'wttr', 'met_no', 'seven_timer', 'open_meteo')
    for invalid in ('["met_no", "met_no"]', '["yandex"]', '["unknown"]', '["met_no", 3]'):
        old = config_file.read_text()
        config_file.write_text(old.replace('["wttr", "met_no", "seven_timer", "open_meteo"]', invalid))
        with pytest.raises(ConfigError):
            load_config(config_file)
        config_file.write_text(old)


@pytest.mark.parametrize('parser,data,source', [(met_no_report, met_data, 'MET Norway'),
    (seven_timer_report, seven_data, '7Timer! (GFS)'), (yandex_report, yandex_data, 'Яндекс Погода')])
def test_real_contracts_render_local_dates_source_and_no_invented_probabilities(parser, data, source):
    report = parser(data(), PLACE, False, NOW)
    text = report.render(advice=True)
    assert report.day == '2026-11-05' and report.source == source and 'зонт' in text
    assert 'источник не сообщает' in text
    assert 'По доступным интервалам' in text
    assert ('Прогноз обновлён:' in text) == (source != 'Яндекс Погода')
    assert parser(data(), PLACE, True, NOW).day == '2026-11-06'
    if source == '7Timer! (GFS)':
        assert '3,4–8' in text and report.forecast.wind_ms is None and 'шапку' not in text
    elif source == 'MET Norway':
        assert report.observation is None and 'CC BY 4.0' in text
    else:
        assert report.observation.temperature == 4 and 'шапку' in text


@pytest.mark.parametrize('parser,data', [(met_no_report, met_data), (seven_timer_report, seven_data),
                                       (yandex_report, yandex_data)])
def test_new_york_uses_city_day_at_utc_midnight(parser, data):
    place = {**PLACE, 'timezone': 'America/New_York'}
    payload = data(place) if parser is yandex_report else data()
    now = NOW - timedelta(hours=1)  # 04 UTC = 23:00 предыдущего дня
    if parser is met_no_report:
        payload['properties']['meta']['updated_at'] = now.isoformat()
    if parser is yandex_report:
        payload['data']['serverTime'] = now.isoformat()
    assert parser(payload, place, True, now).day == '2026-11-05'


@pytest.mark.parametrize('case', ['old', 'future', 'units', 'location', 'gap', 'duplicate', 'nan', 'missing-day'])
def test_met_rejects_wrong_or_incomplete_data(case):
    data = met_data()
    props = data['properties']
    if case == 'old': props['meta']['updated_at'] = (NOW-timedelta(days=1)).isoformat()
    elif case == 'future': props['meta']['updated_at'] = (NOW+timedelta(hours=1)).isoformat()
    elif case == 'units': props['meta']['units']['wind_speed'] = 'km/h'
    elif case == 'location': data['geometry']['coordinates'][0] = 20
    elif case == 'gap': del props['timeseries'][3:8]
    elif case == 'duplicate': props['timeseries'].append(deepcopy(props['timeseries'][0]))
    elif case == 'nan': props['timeseries'][0]['data']['instant']['details']['air_temperature'] = float('nan')
    else: props['timeseries'] = props['timeseries'][:2]
    with pytest.raises((ValueError, KeyError, TypeError)):
        met_no_report(data, PLACE, False, NOW)


@pytest.mark.parametrize('case', ['old', 'wrong-product', 'offset', 'missing-day', 'nan', 'graphql', 'naive'])
def test_yandex_and_seven_timer_reject_unsafe_data(case):
    data = seven_data()
    if case == 'old': data['init'] = '2026110300'
    elif case == 'wrong-product': data['product'] = 'astro'
    elif case == 'offset': data['dataseries'][0]['timepoint'] = '3'
    elif case == 'missing-day': data['dataseries'] = data['dataseries'][:2]
    elif case == 'nan': data['dataseries'][0]['temp2m'] = float('nan')
    else:
        data = yandex_data()
        if case == 'graphql': data['errors'] = [{'message': 'private'}]
        else: data['data']['serverTime'] = '2026-11-05T05:00:00'
        with pytest.raises(ValueError): yandex_report(data, PLACE, False, NOW)
        return
    with pytest.raises((ValueError, KeyError, TypeError)):
        seven_timer_report(data, PLACE, False, NOW)


async def test_last_of_five_sources_still_gets_attempted(config, database, monkeypatch, caplog):
    monkeypatch.setenv('GOOSE_YANDEX_WEATHER_KEY', 'fixture-yandex-secret')
    calls = []
    def route(request):
        calls.append(request)
        if request.url.host == 'www.7timer.info': return httpx.Response(200, json=seven_data())
        return httpx.Response(503)
    engine = make_engine(config, database, route)
    try:
        assert (await engine.get_weather(PLACE)).source == '7Timer! (GFS)'
        assert [r.url.host for r in calls] == ['api.open-meteo.com', 'api.weather.yandex.ru', 'wttr.in', 'api.met.no', 'www.7timer.info']
        assert all(('X-Yandex-Weather-Key' in r.headers) == (r.url.host=='api.weather.yandex.ru') for r in calls)
        assert calls[1].method == 'POST' and 'weatherByPoint' in json.loads(calls[1].content)['query']
        assert 'fixture-yandex-secret' not in caplog.text
        assert 'reason=http_status status=503' in caplog.text
    finally:
        await engine.close()


async def test_four_keyless_sources_and_timeout_chain(config, database, monkeypatch):
    monkeypatch.delenv('GOOSE_YANDEX_WEATHER_KEY', raising=False)
    monkeypatch.setattr('goose.facts.WEATHER_SOURCE_TIMEOUT', .01)
    calls = []
    async def route(request):
        calls.append(request.url.host)
        if request.url.host == 'www.7timer.info': return httpx.Response(200, json=seven_data())
        await asyncio.Event().wait()
    engine = make_engine(config, database, route)
    try:
        assert (await engine.get_weather(PLACE)).source == '7Timer! (GFS)'
        assert calls == ['api.open-meteo.com', 'wttr.in', 'api.met.no', 'www.7timer.info']
    finally:
        await engine.close()


async def test_yandex_is_never_cached_and_quota_survives_reopen(config, database, monkeypatch):
    monkeypatch.setenv('GOOSE_YANDEX_WEATHER_KEY', 'fixture-private')
    calls = []
    def route(request):
        calls.append(request)
        return httpx.Response(200, json=yandex_data())
    engine = make_engine(config, database, route, weather=WeatherConfig(True, 'yandex', ()))
    try:
        for _ in range(30):
            assert (await engine.get_weather(PLACE)).source == 'Яндекс Погода'
        reopened = Database(database.path.parent, database.account)
        await reopened.initialize({})
        engine.database = reopened
        with pytest.raises(FactsError): await engine.get_weather(PLACE)
        assert len(calls) == 30 and not engine.cache
        assert all('fixture-private' not in str(r.url) and r.method == 'POST' for r in calls)
        assert await reopened.claim_yandex_weather_call(NOW.timestamp()+86401)
        assert await database.call(lambda db: db.execute('SELECT COUNT(*) FROM api_usage').fetchone()[0]) == 0
    finally:
        await engine.close()


async def test_yandex_failure_cannot_leak_header_to_met(config, database, monkeypatch, caplog):
    monkeypatch.setenv('GOOSE_YANDEX_WEATHER_KEY', 'fixture-private')
    calls = []
    def route(request):
        calls.append(request)
        if request.url.host == 'api.weather.yandex.ru':
            return httpx.Response(200, json={'errors': [{'message': 'fixture-private'}]})
        return httpx.Response(200, json=met_data())
    engine = make_engine(config, database, route, weather=WeatherConfig(True, 'yandex', ('met_no',)))
    try:
        assert (await engine.get_weather(PLACE)).source == 'MET Norway'
        assert 'x-yandex-weather-key' not in calls[1].headers and 'fixture-private' not in caplog.text
    finally:
        await engine.close()


@pytest.mark.parametrize('provider', ['yandex', 'met_no', 'seven_timer'])
@pytest.mark.parametrize('payload', [[], {'data': None}, {'data': {'weatherByPoint': []}}])
async def test_malformed_source_does_not_interrupt_fallback(config, database, monkeypatch, provider, payload):
    monkeypatch.setenv('GOOSE_YANDEX_WEATHER_KEY', 'fixture-private')
    calls = []
    def route(request):
        calls.append(request.url.host)
        if len(calls) == 1:
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=seven_data() if provider != 'seven_timer' else met_data())
    fallback = 'seven_timer' if provider != 'seven_timer' else 'met_no'
    engine = make_engine(config, database, route, weather=WeatherConfig(True, provider, (fallback,)))
    try:
        assert (await engine.get_weather(PLACE)).source == ('7Timer! (GFS)' if fallback == 'seven_timer' else 'MET Norway')
        assert len(calls) == 2
    finally:
        await engine.close()


async def test_yandex_corrupt_quota_skips_network_and_uses_reserve(config, database, monkeypatch):
    monkeypatch.setenv('GOOSE_YANDEX_WEATHER_KEY', 'fixture-private')
    await database.call(lambda db: db.execute(
        "INSERT INTO metadata VALUES('weather_yandex_attempts', ?)", ('not-json',)))
    calls = []
    def route(request):
        calls.append(request.url.host)
        return httpx.Response(200, json=seven_data())
    engine = make_engine(config, database, route, weather=WeatherConfig(True, 'yandex', ('seven_timer',)))
    try:
        assert (await engine.get_weather(PLACE)).source == '7Timer! (GFS)'
        assert calls == ['www.7timer.info']
    finally:
        await engine.close()


async def test_met_gzip_expiry_conditional_get_and_attribution(config, database):
    calls, clock, now = [], [0], [NOW]
    modified = format_datetime(NOW-timedelta(minutes=10), usegmt=True)
    def route(request):
        calls.append(request)
        headers={'Last-Modified': modified, 'Expires': format_datetime(NOW+timedelta(seconds=600 if len(calls)==1 else 1200), usegmt=True)}
        if len(calls)==2:
            assert request.headers['If-Modified-Since']==modified
            return httpx.Response(304, headers=headers)
        headers['Content-Encoding']='gzip'
        return httpx.Response(200, content=gzip.compress(json.dumps(met_data()).encode()), headers=headers)
    engine = make_engine(config, database, route, weather=WeatherConfig(True, 'met_no', ()), clock=lambda:clock[0], now=lambda:now[0])
    try:
        await engine.get_weather(PLACE)
        clock[0]=301;now[0]=NOW+timedelta(seconds=301)
        await engine.get_weather(PLACE)
        assert len(calls)==1
        clock[0]=601;now[0]=NOW+timedelta(seconds=601)
        assert (await engine.get_weather(PLACE)).source=='MET Norway'
        assert len(calls)==2 and 'github.com/ivancher-dev/goose-xmpp-bot' in calls[0].headers['User-Agent']
        assert calls[0].url.params['lat']=='55.0000' and calls[0].url.params['lon']=='37.0000'
    finally:
        await engine.close()


@pytest.mark.parametrize('target,succeeds', [('https://api.met.no/redirected',True),
    ('https://foreign.example.org/steal',False), ('http://api.met.no/unsecure',False)])
async def test_met_redirects_stay_on_same_https_origin(config, database, target, succeeds):
    calls=[]
    def route(request):
        calls.append(request)
        if len(calls)==1: return httpx.Response(302, headers={'location':target})
        return httpx.Response(200, json=met_data())
    engine=make_engine(config,database,route,weather=WeatherConfig(True,'met_no',()))
    try:
        if succeeds: assert (await engine.get_weather(PLACE)).source=='MET Norway'
        else:
            with pytest.raises(FactsError): await engine.get_weather(PLACE)
        assert len(calls)==(2 if succeeds else 1)
    finally:
        await engine.close()


async def test_waiting_for_a_slot_does_not_spend_notification_work_budget(setup_alerts, database, monkeypatch):
    alerts, facts, now, sent=setup_alerts
    monkeypatch.setattr('goose.weather_alerts.ALERT_WORK_TIMEOUT',.5)
    facts.slots=asyncio.Semaphore(0)
    await database.set_weather_location(PARENT,PLACE)
    await alerts.configure(PARENT,['утром'])
    now[0]=datetime(2026,10,26,5,tzinfo=timezone.utc)
    await alerts.tick()
    await asyncio.sleep(.6)
    assert not sent and not facts.get_weather.called
    facts.slots.release()
    await finish_jobs(alerts)
    assert len(sent)==1
    await alerts.close();await facts.close()
