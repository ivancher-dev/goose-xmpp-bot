"""Погода и заголовки из фиксированных API; содержимое не передаётся модели."""

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html
import json
import logging
import math
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
import httpx

from .config import bare_jid

log = logging.getLogger("goose")
GEO = "https://geocoding-api.open-meteo.com/v1/"
FORECAST = "https://api.open-meteo.com/v1/forecast"
MAX_BYTES = 524288
WEATHER_HELP = ("Погода: !weather [город], !weather tomorrow [город]. "
                "Свой город: !weather city <название> или #ID; просмотр: !weather city; "
                "удаление: !weather city clear. Можно написать: погода в Нью-Йорке.")


class FactsError(Exception):
    pass


def plain(value, limit=160):
    if not isinstance(value, str):
        raise ValueError
    value = html.unescape(re.sub(r"<[^>]*>", "", value))
    return " ".join("".join(c for c in value if c.isprintable() or c.isspace()).split())[:limit]


def number(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError
    return value


def location(value):
    if not isinstance(value, dict):
        raise ValueError
    ident = value["id"]
    if type(ident) is not int or not 0 < ident < 2**31:
        raise ValueError
    name = plain(value["name"], 100)
    if not name:
        raise ValueError
    return {"id": ident, "name": name, "admin1": plain(value.get("admin1", ""), 100),
            "country": plain(value.get("country", ""), 100),
            "latitude": number(value["latitude"], -90, 90),
            "longitude": number(value["longitude"], -180, 180)}


def place_name(place):
    return ", ".join(dict.fromkeys(p for p in (place["name"], place["admin1"], place["country"]) if p))


def intent(body):
    text = body.strip()
    match = re.fullmatch(r"!(weather|погода|news|новости)(?:\s+(.*))?", text, re.I)
    if match:
        return ("weather" if match[1].lower() in {"weather", "погода"} else "news", match[2] or "")
    text = re.sub(r"^(?:гусь|гусик|гусман)[,! ]+", "", text, flags=re.I).strip(" ?!.")
    match = re.fullmatch(r"(?:какая\s+)?погода(?:\s+(.*))?", text, re.I)
    if match:
        tail = match[1] or ""
        # Предлог относится к месту; tomorrow/завтра обрабатываются отдельно.
        tail = re.sub(r"(^|\s)в\s+", r"\1", tail, count=1, flags=re.I)
        return "weather", tail
    match = re.fullmatch(r"(?:покажи\s+)?(?:свежие\s+)?новости(?:\s+(.*))?", text, re.I)
    if match:
        return "news", re.sub(r"^(?:из|с|от)\s+", "", match[1] or "", flags=re.I)
    return None


def article_url(value, feed_url):
    value = value.strip()
    if not value or any(ord(c) <= 32 for c in value):
        raise ValueError
    address, feed = urlsplit(urljoin(feed_url, value)), urlsplit(feed_url)
    host = address.hostname or ""
    if (address.scheme not in {"https", "http"} or address.username or address.password
            or address.port not in (None, 80, 443)
            or not (host == feed.hostname or host.endswith("." + feed.hostname))):
        raise ValueError
    return urlunsplit(("https", host, address.path or "/", address.query, ""))


def headlines(payload, source, now, max_age):
    root = ElementTree.fromstring(payload, forbid_dtd=True)
    atom = "{http://www.w3.org/2005/Atom}"
    if root.tag == "rss":
        entries, is_atom = root.findall("./channel/item"), False
    elif root.tag == atom + "feed":
        entries, is_atom = root.findall(atom + "entry"), True
    else:
        raise ValueError
    found = {}
    for entry in entries:
        try:
            if is_atom:
                title = plain(entry.findtext(atom + "title"))
                date = entry.findtext(atom + "published") or entry.findtext(atom + "updated")
                published = datetime.fromisoformat(date.replace("Z", "+00:00"))
                links = [e.get("href", "") for e in entry.findall(atom + "link")
                         if e.get("rel", "alternate") == "alternate"]
                link = article_url(links[0], source.url)
            else:
                title = plain(entry.findtext("title"))
                published = parsedate_to_datetime(entry.findtext("pubDate"))
                link = article_url(entry.findtext("link"), source.url)
            if (not title or published.tzinfo is None
                    or not now - timedelta(hours=max_age) <= published <= now + timedelta(minutes=5)):
                continue
            item = (published, title, link)
            if link not in found or published > found[link][0]:
                found[link] = item
        except (ValueError, TypeError, AttributeError, IndexError, OverflowError):
            continue
    return sorted(found.values(), reverse=True)


def condition(code):
    number(code, 0, 99)
    groups = (({0}, "ясно"), ({1}, "преимущественно ясно"), ({2}, "переменная облачность"),
              ({3}, "пасмурно"), ({45, 48}, "туман"), ({51, 53, 55}, "морось"),
              ({56, 57}, "переохлаждённая морось"), ({61, 63, 65}, "дождь"),
              ({66, 67}, "ледяной дождь"), ({71, 73, 75, 77}, "снег"),
              ({80, 81, 82}, "ливень"), ({85, 86}, "снежный ливень"),
              ({95}, "гроза"), ({96, 99}, "гроза с градом"))
    return next((label for codes, label in groups if code in codes), f"код погоды {code}")


def weather_text(data, place, tomorrow, now):
    zone = ZoneInfo(data["timezone"])
    local = now.astimezone(zone)
    daily, units = data["daily"], data["daily_units"]
    required = {"temperature_2m_min": "°C", "temperature_2m_max": "°C",
                "precipitation_probability_max": "%", "weather_code": "wmo code"}
    if any(units.get(key) != unit for key, unit in required.items()):
        raise ValueError
    day = (local + timedelta(days=int(tomorrow))).date().isoformat()
    idx = daily["time"].index(day)
    minimum = number(daily["temperature_2m_min"][idx], -100, 70)
    maximum = number(daily["temperature_2m_max"][idx], minimum, 70)
    rain = number(daily["precipitation_probability_max"][idx], 0, 100)
    summary = condition(daily["weather_code"][idx])
    lines = [f"{place_name(place)} — {'завтра' if tomorrow else 'сегодня'}, {day} ({zone.key})."]
    if not tomorrow:
        current, current_units = data["current"], data["current_units"]
        if (current_units.get("temperature_2m") != "°C" or current_units.get("apparent_temperature") != "°C"
                or current_units.get("wind_speed_10m") != "km/h" or current_units.get("weather_code") != "wmo code"):
            raise ValueError
        stamp = datetime.fromisoformat(current["time"]).replace(tzinfo=zone)
        if abs((now - stamp).total_seconds()) > 10800:
            raise ValueError
        temp = number(current["temperature_2m"], -100, 70)
        feels = number(current["apparent_temperature"], -120, 90)
        wind = number(current["wind_speed_10m"], 0, 500)
        lines.append(f"На {stamp:%H:%M}: {condition(current['weather_code'])}, {temp:g} °C, "
                     f"ощущается как {feels:g} °C; ветер {wind:g} км/ч.")
    lines.append(f"Прогноз: {summary}; от {minimum:g} до {maximum:g} °C. Вероятность осадков: {rain:g}%.")
    lines.append("Источник: Open-Meteo — https://open-meteo.com/")
    return "\n".join(lines)


@dataclass
class State:
    pending: deque = field(default_factory=deque)
    task: asyncio.Task | None = None
    revision: int = 0


class Facts:
    def __init__(self, config, database, prepare_reply, *, transport=None,
                 clock=time.monotonic, now=lambda: datetime.now(timezone.utc)):
        self.config, self.database, self.prepare_reply = config, database, prepare_reply
        self.transport, self.clock, self.now = transport, clock, now
        self.client = None
        self.states = {jid: State() for jid in config.allowed}
        self.tasks = set()
        self.slots = asyncio.Semaphore(2)
        self.cache = OrderedDict()
        self.attempts = deque()
        self.closing = False

    def submit(self, jid, body):
        request = intent(body)
        if request is None:
            return False
        jid = bare_jid(jid)
        if jid not in self.states or self.closing or len(body) > 2048:
            return True
        state = self.states[jid]
        if len(state.pending) + bool(state.task) >= 4 or sum(len(s.pending) for s in self.states.values()) + len(self.tasks) >= 32:
            log.warning("facts_ignored reason=queue_full")
            return True
        state.pending.append((request, self.clock() + 30, state.revision))
        if state.task is None:
            state.task = asyncio.create_task(self._work(jid, state))
            self.tasks.add(state.task)
            state.task.add_done_callback(self.tasks.discard)
        return True

    def cancel(self, jid):
        state = self.states[bare_jid(jid)]
        state.revision += 1
        state.pending.clear()
        if state.task:
            state.task.cancel()
            state.task = None

    async def _work(self, jid, state):
        try:
            while state.pending and not self.closing and state.task is asyncio.current_task():
                request, deadline, revision = state.pending.popleft()
                if self.clock() >= deadline:
                    continue
                def valid():
                    return not self.closing and state.revision == revision and self.clock() < deadline
                try:
                    async with asyncio.timeout(max(0, deadline - self.clock())):
                        try:
                            async with self.slots:
                                reply = await self.answer(jid, *request, valid=valid)
                        except FactsError as exc:
                            reply = str(exc)
                        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, OverflowError):
                            log.warning("facts_failed reason=source_unavailable")
                            reply = "Источник временно недоступен или вернул неверные данные. Попробуй позже."
                        if valid():
                            send = await self.prepare_reply(jid, reply)
                            if send and valid():
                                send()
                except TimeoutError:
                    log.warning("facts_failed reason=deadline")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("facts_failed reason=processing")
        finally:
            if state.task is asyncio.current_task():
                state.task = None

    async def fetch(self, url, params=None):
        key = (url, tuple(sorted((params or {}).items())))
        cached = self.cache.get(key)
        if cached and self.clock() - cached[0] < 300:
            self.cache.move_to_end(key)
            return cached[1]
        while self.attempts and self.clock() - self.attempts[0] >= 60:
            self.attempts.popleft()
        if len(self.attempts) >= 60:
            raise FactsError("Лимит запросов к источникам исчерпан. Попробуй через минуту.")
        self.attempts.append(self.clock())
        if self.client is None:
            self.client = httpx.AsyncClient(transport=self.transport, timeout=10, follow_redirects=False,
                                          trust_env=False, headers={"User-Agent": "goose-xmpp-bot/0.2.1",
                                                                    "Accept-Encoding": "identity"})
        async with self.client.stream("GET", url, params=params) as response:
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError
            if int(response.headers.get("content-length", "0")) > MAX_BYTES:
                raise ValueError
            size, chunks = 0, []
            async for chunk in response.aiter_bytes(chunk_size=8192):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError
                chunks.append(chunk)
        result = b"".join(chunks)
        self.cache[key] = (self.clock(), result)
        self.cache.move_to_end(key)
        while len(self.cache) > 64:
            self.cache.popitem(last=False)
        return result

    async def geocode(self, query):
        if not query or len(query) > 100 or any(ord(c) < 32 for c in query):
            raise FactsError(WEATHER_HELP)
        if re.fullmatch(r"#[1-9][0-9]{0,9}", query):
            data = json.loads(await self.fetch(GEO + "get", {"id": query[1:], "language": "ru", "format": "json"}))
            result = location(data)
            if result["id"] != int(query[1:]):
                raise ValueError
            return result
        # Частые предложные формы; для остальных мест API использует нечёткий поиск.
        query = {"москве": "Москва", "нью-йорке": "Нью-Йорк", "санкт-петербурге": "Санкт-Петербург"}.get(query.casefold(), query)
        data = json.loads(await self.fetch(GEO + "search", {"name": query, "count": 5, "language": "ru", "format": "json"}))
        if not isinstance(data, dict):
            raise ValueError
        results = data.get("results", [])
        if not isinstance(results, list):
            raise ValueError
        places = [location(value) for value in results[:5]]
        if not places:
            raise FactsError("Город не найден. Попробуй название в именительном падеже, добавив регион или страну.")
        if len(places) > 1:
            choices = "\n".join(f"{place_name(p)} — #{p['id']}" for p in places)
            raise FactsError("Нашёл несколько мест. Выбери ID: !weather city #ID для своего города "
                             "или !weather #ID для разового запроса.\n" + choices
                             + "\nПоиск: Open-Meteo / GeoNames — https://geocoding-api.open-meteo.com/")
        return places[0]

    async def answer(self, jid, kind, arg, *, valid=lambda: True):
        if kind == "news":
            return await self.news(arg)
        if not self.config.weather.enabled:
            return "Погода отключена владельцем в настройках."
        if arg == "city" or arg == "city clear":
            if arg.endswith(" clear"):
                await self.database.set_weather_location(jid, None)
                return "Свой город удалён."
            place = await self.database.weather_location(jid)
            return "Свой город: " + place_name(location(place)) if place else "Свой город ещё не задан. " + WEATHER_HELP
        if arg.startswith("city "):
            place = await self.geocode(arg[5:].strip())
            if not valid():
                return ""
            await self.database.set_weather_location(jid, place)
            return "Свой город сохранён: " + place_name(place) + ".\nПоиск: Open-Meteo / GeoNames — https://open-meteo.com/"
        tomorrow = bool(re.search(r"(?:^|\s)(?:tomorrow|завтра)(?:\s|$)", arg, re.I))
        query = re.sub(r"(?:^|\s)(?:tomorrow|завтра|сегодня)(?=\s|$)", " ", arg, flags=re.I).strip()
        place = await self.geocode(query) if query else await self.database.weather_location(jid)
        if place is None:
            return "Сначала задай свой город. " + WEATHER_HELP
        place = location(place)
        payload = await self.fetch(FORECAST, {
            "latitude": place["latitude"], "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": 2, "timezone": "auto", "temperature_unit": "celsius", "wind_speed_unit": "kmh"})
        return weather_text(json.loads(payload), place, tomorrow, self.now())

    async def news(self, arg):
        config = self.config.news
        if not config.enabled:
            return "Новости отключены владельцем в настройках."
        def aliases(source):
            return {source.key.casefold(), source.name.casefold(), source.name.split()[0].casefold(),
                    urlsplit(source.url).hostname.casefold()}
        sources = [s for s in config.sources if not arg or arg.casefold() in aliases(s)]
        if not sources:
            return "Источник не настроен. Доступны: " + ", ".join(s.key for s in config.sources) + ". Команда: !news [источник]"
        if arg and len(sources) > 1:
            return "Уточни источник по ID: " + ", ".join(s.key for s in sources)
        now, items, failed = self.now(), [], []
        for source in sources:
            try:
                payload = await self.fetch(source.url)
                items.extend((stamp, title, link, source.name) for stamp, title, link in
                             headlines(payload, source, now, config.max_age_hours))
            except (httpx.HTTPError, ValueError, TypeError, ElementTree.ParseError, DefusedXmlException):
                log.warning("facts_failed reason=news_source")
                failed.append(source.name)
        lines = ["Свежие новости (" + self.config.timezone + ")."]
        seen = set()
        for stamp, title, link, name in sorted(items, reverse=True):
            if link in seen:
                continue
            block = f"{len(seen) + 1}. {title}\n{stamp.astimezone(ZoneInfo(self.config.timezone)):%d.%m.%Y %H:%M} · {name}\n{link}"
            if len("\n\n".join(lines + [block])) > 3500:
                continue
            lines.append(block)
            seen.add(link)
            if len(seen) >= config.limit:
                break
        if not seen:
            lines.append(f"Свежих записей с датой за последние {config.max_age_hours} ч не получено.")
        if failed:
            lines.append("Не удалось прочитать источники: " + ", ".join(failed) + ".")
        return "\n\n".join(lines)

    async def close(self):
        self.closing = True
        for jid in self.states:
            self.cancel(jid)
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.client:
            await self.client.aclose()
