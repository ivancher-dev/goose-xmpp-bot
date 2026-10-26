import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import xml.etree.ElementTree as ET

import httpx
import pytest
import pytest_asyncio
from slixmpp import Message

from goose.config import ConfigError, NewsConfig, NewsSource, WeatherConfig, load_config
from goose.dialogue import Dialogues
from goose.facts import Facts, FactsError, MAX_BYTES, headlines, intent, weather_text
from goose.omemo import LEGACY_NS
from goose.storage import Database
from goose.transport import GooseClient

PARENT, CHILD = "parent@example.org", "pupil1@example.org"
NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
SOURCE = NewsSource("agency", "Агентство", "https://agency.example.org/feed.xml")
PLACE = {"id": 10, "name": "Город", "country": "Страна", "admin1": "Область",
         "latitude": 55.0, "longitude": 37.0}


def forecast():
    return {"timezone": "Europe/Moscow",
            "current": {"time": "2026-10-26T15:00", "temperature_2m": 4,
                        "apparent_temperature": 2, "weather_code": 3, "wind_speed_10m": 8},
            "current_units": {"temperature_2m": "°C", "apparent_temperature": "°C",
                              "weather_code": "wmo code", "wind_speed_10m": "km/h"},
            "daily_units": {"temperature_2m_min": "°C", "temperature_2m_max": "°C",
                            "weather_code": "wmo code", "precipitation_probability_max": "%"},
            "daily": {"time": ["2026-10-26", "2026-10-27"], "temperature_2m_min": [1, 0],
                      "temperature_2m_max": [6, 5], "weather_code": [3, 61],
                      "precipitation_probability_max": [20, 70]}}


def rss(items):
    return ("<rss><channel>" + "".join(
        f"<item><title>{escape(title)}</title><link>{escape(link)}</link><pubDate>{escape(stamp)}</pubDate></item>"
        for title, link, stamp in items) + "</channel></rss>").encode()


def feed():
    return rss([(f"Новость {n}", f"https://agency.example.org/item/{n}", format_datetime(NOW - timedelta(minutes=n)))
                for n in range(7)])


@pytest_asyncio.fixture
async def system(config, database):
    calls, sent = [], []
    def route(request):
        calls.append(request)
        if request.url.host == "agency.example.org":
            return httpx.Response(200, content=feed())
        if request.url.path == "/v1/search":
            return httpx.Response(200, json={"results": [PLACE]})
        if request.url.path == "/v1/get":
            return httpx.Response(200, json=PLACE)
        return httpx.Response(200, json=forecast())
    async def prepare(jid, reply):
        return lambda: sent.append((jid, reply))
    config = replace(config, weather=WeatherConfig(True), news=NewsConfig(True, (SOURCE,)))
    engine = Facts(config, database, prepare, transport=httpx.MockTransport(route), now=lambda: NOW)
    try:
        yield engine, calls, sent
    finally:
        await engine.close()


async def idle(engine):
    await asyncio.wait_for(asyncio.gather(*tuple(engine.tasks)), 1)


@pytest.mark.parametrize("body,expected", [
    ("погода в Нью-Йорке", ("weather", "Нью-Йорке")),
    ("Гусик, какая погода завтра в Москве?", ("weather", "завтра Москве")),
    ("!погода tomorrow #10", ("weather", "tomorrow #10")),
    ("Гусман, покажи свежие новости с agency.example.org!", ("news", "agency.example.org")),
    ("Напиши рассказ о погоде", None), ("!admin status", None),
])
def test_routing(body, expected):
    assert intent(body) == expected


async def test_city_is_per_jid_persistent_and_one_off_does_not_change_it(system, database):
    engine, calls, sent = system
    engine.submit(PARENT + "/phone", "!weather city Город")
    engine.submit(PARENT, "!weather")
    await idle(engine)
    assert "сохранён" in sent[0][1] and "4 °C" in sent[1][1]
    assert "Europe/Moscow" in sent[1][1] and "Open-Meteo" in sent[1][1]
    assert await database.weather_location(CHILD) is None
    engine.submit(CHILD, "!weather")
    engine.submit(PARENT, "погода в Нью-Йорке")
    await idle(engine)
    assert "Сначала" in next(text for jid, text in sent if jid == CHILD)
    assert calls[-1].url.params.get("name") == "Нью-Йорк"  # прогноз взят из кэша
    assert (await database.weather_location(PARENT))["id"] == 10
    reopened = Database(database.path.parent, database.account)
    await reopened.initialize({})
    assert await reopened.weather_location(PARENT) == PLACE
    await database.clear_context(PARENT)
    assert await database.weather_location(PARENT) == PLACE
    engine.submit(PARENT, "!weather city clear")
    await idle(engine)
    assert await database.weather_location(PARENT) is None
    assert all("example.org" not in str(r.url.params) for r in calls)


async def test_ambiguous_search_requires_choice_without_changing_default(system, database):
    engine, _, _ = system
    await database.set_weather_location(PARENT, PLACE)
    other = {**PLACE, "id": 11, "admin1": "Другой регион"}
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"results": [PLACE, other]})))
    with pytest.raises(FactsError, match="#10"):
        await engine.geocode("Одноимённый город")
    assert await database.weather_location(PARENT) == PLACE
    await engine.client.aclose()
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=other)))
    assert "сохранён" in await engine.answer(PARENT, "weather", "city #11")
    assert (await database.weather_location(PARENT))["id"] == 11


@pytest.mark.parametrize("change", ["old", "bad-unit", "missing-date", "nonfinite", "unknown-zone"])
def test_weather_rejects_stale_or_malformed_data(change):
    data = forecast()
    if change == "old":
        data["current"]["time"] = "2026-10-25T15:00"
    elif change == "bad-unit":
        data["current_units"]["temperature_2m"] = "°F"
    elif change == "missing-date":
        data["daily"]["time"] = ["2026-10-25"]
    elif change == "nonfinite":
        data["current"]["temperature_2m"] = float("nan")
    else:
        data["timezone"] = "invalid/zone"
    with pytest.raises((ValueError, KeyError)):
        weather_text(data, PLACE, False, NOW)


def test_tomorrow_uses_local_date_and_does_not_claim_current_weather():
    result = weather_text(forecast(), PLACE, True, NOW)
    assert "завтра, 2026-10-27" in result and "70%" in result and "На 15:00" not in result


async def test_news_five_sorted_headlines_and_allowlisted_source_only(system):
    engine, calls, sent = system
    engine.submit(PARENT, "новости с agency.example.org")
    await idle(engine)
    result = sent[0][1]
    assert result.count("https://agency.example.org/item/") == 5
    assert result.index("Новость 0") < result.index("Новость 4")
    assert "26.10.2026" in result and "Новость 5" not in result
    engine.submit(PARENT, "!news https://unlisted.example.org/")
    await idle(engine)
    assert "не настроен" in sent[-1][1] and len(calls) == 1


def test_rss_filters_dates_duplicates_foreign_links_and_html():
    payload = rss([
        ("<b>Свежая</b> & новость", "/fresh", format_datetime(NOW)),
        ("Дубликат", "/fresh", format_datetime(NOW - timedelta(minutes=1))),
        ("Старая", "/old", format_datetime(NOW - timedelta(days=3))),
        ("Будущая", "/future", format_datetime(NOW + timedelta(days=1))),
        ("Без даты", "/undated", ""),
        ("Чужая", "https://agency.example.org.evil.test/post", format_datetime(NOW)),
        ("Скрипт", "javascript:alert(1)", format_datetime(NOW)),
    ])
    entries = headlines(payload, SOURCE, NOW, 48)
    assert entries == [(NOW, "Свежая & новость", "https://agency.example.org/fresh")]


def test_atom_date_and_alternate_link():
    payload = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom title</title>
    <published>2026-10-26T11:00:00Z</published><link rel="self" href="https://evil.example.org/"/>
    <link href="https://agency.example.org/atom"/></entry></feed>'''
    assert headlines(payload, SOURCE, NOW, 48)[0][2] == "https://agency.example.org/atom"


@pytest.mark.parametrize("payload", [b"<broken", b"<html>error</html>",
    b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel>&x;</channel></rss>'])
async def test_unreadable_news_is_an_explicit_failure(system, payload):
    engine, _, _ = system
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload)))
    reply = await engine.news("")
    assert "Не удалось прочитать" in reply and "Свежих записей" in reply


async def test_partial_news_failure_still_returns_verified_items(system):
    engine, _, _ = system
    engine.config = replace(engine.config, news=replace(engine.config.news, sources=(SOURCE,
        NewsSource("other", "Другой", "https://other.example.org/feed"))))
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
        httpx.Response(200, content=feed()) if request.url.host == "agency.example.org" else httpx.Response(503)))
    reply = await engine.news("")
    assert "Новость 0" in reply and "Не удалось прочитать источники: Другой" in reply


async def test_disabled_features_and_unauthorized_users_make_no_requests(config, database):
    sent = []
    async def prepare(jid, reply):
        return lambda: sent.append(reply)
    engine = Facts(config, database, prepare)
    try:
        engine.submit(PARENT, "!weather city Город")
        engine.submit(PARENT, "!news")
        engine.submit("unknown@example.org", "!weather")
        await idle(engine)
        assert len(sent) == 2 and all("отключен" in text for text in sent)
        assert engine.client is None
    finally:
        await engine.close()


async def test_cache_expires_and_does_not_use_old_data_on_failure(system):
    engine, calls, _ = system
    clock = [0]
    engine.clock = lambda: clock[0]
    await engine.news("")
    await engine.news("")
    assert len(calls) == 1
    clock[0] = 301
    await engine.client.aclose()
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    result = await engine.news("")
    assert "Новость 0" not in result and "Не удалось" in result


async def test_http_budget_and_cache_memory_are_bounded(system):
    engine, calls, _ = system
    clock = [0]
    engine.clock = lambda: clock[0]
    for index in range(60):
        await engine.fetch("https://agency.example.org/" + str(index))
    with pytest.raises(FactsError, match="Лимит"):
        await engine.fetch("https://agency.example.org/61")
    assert len(calls) == 60
    clock[0] = 61
    for index in range(60, 70):
        await engine.fetch("https://agency.example.org/" + str(index))
    assert len(engine.cache) == 64 and len(calls) == 70


async def test_expired_queue_job_makes_no_request(system):
    engine, calls, sent = system
    clock = [0]
    engine.clock = lambda: clock[0]
    engine.submit(PARENT, "!news")
    clock[0] = 31
    await idle(engine)
    assert not calls and not sent


@pytest.mark.parametrize("failure", ["timeout", "redirect", "oversize", "compressed", "json-root"])
async def test_http_failure_is_bounded_and_no_raw_details_leak(system, failure):
    engine, calls, sent = system
    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("sensitive-url-or-content", request=request)
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://unlisted.example.org/"})
        if failure == "compressed":
            return httpx.Response(200, headers={"content-encoding": "br"}, content=b"not compressed")
        if failure == "json-root":
            return httpx.Response(200, json=[])
        return httpx.Response(200, content=b"x" * (MAX_BYTES + 1))
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False)
    engine.submit(PARENT, "!weather Город")
    await idle(engine)
    assert len(calls) == 1 and "Попробуй позже" in sent[0][1] and "sensitive" not in sent[0][1]
    if failure == "timeout":
        assert "не ответил вовремя" in sent[0][1]
    elif failure == "redirect":
        assert "HTTP" in sent[0][1]


async def test_queue_limits_and_cancel_during_encryption_prevent_late_send(system):
    engine, _, sent = system
    started, finish = asyncio.Event(), asyncio.Event()
    async def prepare(jid, reply):
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            await finish.wait()
        return lambda: sent.append((jid, reply))
    engine.prepare_reply = prepare
    for _ in range(10):
        engine.submit(PARENT, "!news")
    assert len(engine.states[PARENT].pending) == 3  # первая задача учитывается отдельно
    await asyncio.wait_for(started.wait(), 1)
    engine.cancel(PARENT)
    finish.set()
    await idle(engine)
    assert not sent


async def test_old_cancelled_worker_cannot_consume_new_queue_or_save_city(system, database):
    engine, _, sent = system
    started, finish = asyncio.Event(), asyncio.Event()
    async def slow_geocode(query):
        if query == "Старый":
            started.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                await finish.wait()
        return {**PLACE, "name": query}
    engine.geocode = slow_geocode
    engine.submit(PARENT, "!weather city Старый")
    await asyncio.wait_for(started.wait(), 1)
    engine.cancel(PARENT)
    engine.submit(PARENT, "!weather city Новый")
    engine.submit(PARENT, "!weather city")
    finish.set()
    await idle(engine)
    assert (await database.weather_location(PARENT))["name"] == "Новый"
    assert len(sent) == 2 and all("Новый" in text for _, text in sent)


async def test_shutdown_cancels_waiting_requests(system):
    engine, _, sent = system
    started = asyncio.Event()
    async def slow(request):
        started.set()
        await asyncio.Event().wait()
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    engine.submit(PARENT, "!news")
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(engine.close(), 1)
    assert not sent and not engine.tasks and engine.client.is_closed


async def test_schema_two_upgrade_preserves_identity_modes_history_and_preferences(database, config):
    await database.call(lambda db: db.execute("INSERT INTO omemo VALUES('fixture','{\"key\":true}')"))
    await database.set_mode(PARENT, "auto")
    await database.add_user_message(PARENT, "Пример")
    await database.call(lambda db: db.execute("DROP TABLE weather_locations"))
    await database.call(lambda db: db.execute("PRAGMA user_version=2"))
    await database.initialize(config.users)
    assert (await database.dialogue(PARENT))["mode"] == "auto"
    assert (await database.history(PARENT, 100, 24, 30))[0]["content"] == "Пример"
    assert await database.call(lambda db: db.execute("SELECT value FROM omemo").fetchone()[0]) == '{"key":true}'
    assert await database.profile(CHILD) == {"jid": CHILD, **config.users[CHILD]}
    await database.set_weather_location(CHILD, PLACE)
    assert await database.weather_location(CHILD) == PLACE


@pytest.mark.parametrize("extra", [
    '[news]\nenabled=true', '[weather]\nenabled="yes"', '[news]\nlimit=50',
    '[news]\nmax_age_hours=0', '[news.sources.bad]\nname="test"\nurl="http://example.org/feed"',
    '[news.sources.bad]\nname="test"\nurl="https://example.org/feed?token=secret"',
])
def test_configuration_rejects_invalid_extension(config_file, extra):
    text = config_file.read_text()
    start, end = text.index("[weather]"), text.index("[schedule]")
    config_file.write_text(text[:start] + extra + "\n" + text[end:])
    with pytest.raises(ConfigError):
        load_config(config_file)


async def test_transport_deduplicates_tool_requests_and_pause_stays_responsive(system, database, monkeypatch, carbon_message):
    engine, calls, _ = system
    client = GooseClient(engine.config, "fixture", database)
    client.init_plugins()
    client.ready.set()
    started, finish = asyncio.Event(), asyncio.Event()
    async def slow(request):
        calls.append(request)
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            await finish.wait()
        return httpx.Response(200, content=feed())
    engine.client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    client.facts = client.commands.facts = engine
    engine.prepare_reply = client.prepare_reply
    client.commands.dialogues = Dialogues(engine.config, database, None, client.prepare_reply)
    outgoing = Message()
    ET.SubElement(outgoing.xml, f"{{{LEGACY_NS}}}encrypted")
    monkeypatch.setattr(outgoing, "send", Mock())
    async def decrypt(stanza):
        return stanza, SimpleNamespace(bare_jid=PARENT, trust_level_name="TRUSTED")
    plugin = SimpleNamespace(is_encrypted=lambda m: {LEGACY_NS}, decrypt_message=AsyncMock(side_effect=decrypt),
                             encrypt_message=AsyncMock(return_value=(outgoing, frozenset())))
    monkeypatch.setitem(client.plugin._plugins, "xep_0384", plugin)
    def message(ident, body):
        m = Message()
        m["from"], m["to"], m["type"], m["id"], m["body"] = PARENT, engine.config.jid, "chat", ident, body
        return m
    try:
        query = message("query", "!news")
        await asyncio.wait_for(client.process_message(query), 0.5)
        await asyncio.wait_for(started.wait(), 1)
        client.admit(carbon_message(query))
        await client.process_message(client.queue.get_nowait())
        await asyncio.wait_for(client.process_message(message("pause", "!admin mode parent@example.org manual")), 0.5)
        assert plugin.encrypt_message.call_args.args[0]["body"] == "Режим: manual"
        finish.set()
        await idle(engine)
        outgoing.send.assert_called_once()
        assert len(calls) == 1
    finally:
        finish.set()
        await client.commands.dialogues.close()
