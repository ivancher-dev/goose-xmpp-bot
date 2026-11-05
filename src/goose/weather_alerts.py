"""Добровольная погодная рассылка: местное время города и постоянные отметки попыток."""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import re
from zoneinfo import ZoneInfo

import httpx

from .config import ConfigError
from .facts import FactsError

log = logging.getLogger('goose')
PERIODS = {'утром': ('08:00', 'Доброе утро!'),
           'днём': ('13:00', 'Добрый день!'),
           'вечером': ('18:00', 'Добрый вечер!')}
ALIASES = {'утро': 'утром', 'morning': 'утром', 'день': 'днём', 'днем': 'днём',
           'day': 'днём', 'вечер': 'вечером', 'evening': 'вечером'}
WINDOW = 600
ALERT_WORK_TIMEOUT = 35
HELP = ('Рассылка погоды: !weather notify утром [08:00] днём [13:00] вечером [18:00]. '
        'Выбери от одного до трёх уведомлений; команда заменяет весь набор. '
        'Можно указать только время: !weather notify 08:30 18:15. '
        '!weather notify — настройки; !weather notify off — выключить. '
        'Время — в часовом поясе своего города; рассылка работает и в manual.')


def valid_time(value):
    return isinstance(value, str) and re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value) is not None


def validate_times(times):
    if (not isinstance(times, dict) or len(times) > 3
            or any(period not in PERIODS or not valid_time(value) for period, value in times.items())
            or len(set(times.values())) != len(times)):
        raise ConfigError(HELP)


def parse_times(words):
    times, words = {}, [word.casefold() for word in words]
    if words in (['off'], ['выкл']):
        return times
    while words:
        value = words.pop(0).lower()
        period = ALIASES.get(value, value)
        if valid_time(value):
            hour = int(value[:2])
            period = 'утром' if hour < 12 else 'днём' if hour < 17 else 'вечером'
            clock = value
        elif period in PERIODS:
            clock = words.pop(0) if words and valid_time(words[0]) else PERIODS[period][0]
        else:
            raise ConfigError(HELP)
        if period in times:
            raise ConfigError('Не больше одного уведомления на утро, день и вечер. ' + HELP)
        times[period] = clock
    validate_times(times)
    return times


def due_slots(subscription, zone, now):
    local = now.astimezone(zone)
    for day in (local.date() - timedelta(days=1), local.date()):
        for period, clock in subscription['times'].items():
            hour, minute = map(int, clock.split(':'))
            due = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
            # Пропавшее при DST время не переносим на другой час; повторный час — один раз.
            if due.astimezone(timezone.utc).astimezone(zone) != due:
                continue
            stamp = due.timestamp()
            if subscription['changed'] <= stamp and 0 <= now.timestamp() - stamp < WINDOW:
                yield period, day.isoformat(), stamp


class WeatherAlerts:
    def __init__(self, config, database, facts, prepare_reply, *, ready=lambda: True,
                 now=lambda: datetime.now(timezone.utc)):
        self.config, self.database, self.facts = config, database, facts
        self.prepare_reply, self.ready, self.now = prepare_reply, ready, now
        self.task = None
        self.jobs = {}
        self.closing = False
        self.wakeup = asyncio.Event()

    async def configure(self, jid, words, *, valid=lambda: True):
        try:
            return await self._configure(jid, words, valid=valid)
        except ConfigError as exc:
            return str(exc)
        except FactsError as exc:
            return str(exc)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            log.warning('weather_alert_failed reason=city_timezone')
            return 'Не удалось проверить часовой пояс города. Настройки рассылки не изменены; попробуй позже.'

    async def _configure(self, jid, words, *, valid=lambda: True):
        if jid not in self.config.allowed:
            raise ConfigError('Диалог вне списка разрешённых аккаунтов.')
        times = parse_times(words) if words else None
        place = await self.database.weather_location(jid)
        if times:
            if not self.config.weather.enabled:
                return 'Погода отключена владельцем в настройках.'
            if place is None:
                return 'Сначала задай свой город: !weather city <название>.'
            if 'timezone' not in place:
                metadata = await self.facts.geocode(f"#{place['id']}")
                if not valid():
                    return ''
                previous = place
                place = {**place, 'timezone': ZoneInfo(metadata['timezone']).key}
                if not await self.database.set_weather_location(
                        jid, place, stamp=self.now().timestamp(), expected=previous):
                    return 'Город изменился во время настройки. Проверь !weather city и повтори команду.'
            ZoneInfo(place['timezone'])
        if times is not None and valid():
            await self.database.set_weather_subscription(jid, times, self.now().timestamp())
        subscription = await self.database.weather_subscription(jid)
        if not subscription or not subscription['times']:
            return 'Рассылка погоды выключена. ' + HELP
        entries = ', '.join(f'{p} в {t}' for p, t in subscription['times'].items())
        zone = place.get('timezone') if place else None
        return (f'Рассылка погоды: {entries}.\n'
                + (f"Город: {place['name']}; часовой пояс: {zone}." if zone else 'Нет города/часового пояса; рассылка приостановлена.')
                + '\nПриходит и в manual. !weather notify off — выключить.')

    def start(self):
        if self.task is None and not self.closing and self.config.weather.enabled:
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        while not self.closing:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning('weather_alert_failed reason=scheduler')
            try:
                await asyncio.wait_for(self.wakeup.wait(), 20)
            except TimeoutError:
                pass

    async def tick(self):
        if self.closing or not self.config.weather.enabled or not self.ready():
            return
        for jid in self.config.allowed:
            if jid in self.jobs:
                continue
            subscription = await self.database.weather_subscription(jid)
            place = await self.database.weather_location(jid) if subscription and subscription['times'] else None
            if not place or 'timezone' not in place:
                continue
            try:
                zone = ZoneInfo(place['timezone'])
                due = list(due_slots(subscription, zone, self.now()))
            except (ValueError, KeyError, TypeError):
                log.warning('weather_alert_failed reason=preferences')
                continue
            if due:
                task = asyncio.create_task(self._deliver(jid, subscription, place, due))
                self.jobs[jid] = task
                task.add_done_callback(lambda task, jid=jid: self.jobs.pop(jid, None))

    async def _deliver(self, jid, subscription, place, due):
        try:
            for period, day, stamp in due:
                def valid():
                    return (not self.closing and self.ready()
                            and 0 <= self.now().timestamp() - stamp < WINDOW)
                async with asyncio.timeout(max(0, stamp + WINDOW - self.now().timestamp())):
                    async with self.facts.slots:
                        async with asyncio.timeout(ALERT_WORK_TIMEOUT):
                            if not valid():
                                continue
                            claimed = await self.database.claim_weather_delivery(
                                jid, period, day, subscription['revision'], self.now().timestamp())
                            if not claimed:
                                continue
                            try:
                                report = await self.facts.get_weather(place)
                                # Полночь могла наступить во время HTTP: сводка относится к своему слоту.
                                if report.day != day:
                                    continue
                                body = report.render(advice=True)
                            except FactsError:
                                body = 'Сейчас не удалось получить свежий прогноз. Спросить позже: !weather.'
                            if not valid():
                                continue
                            send = await self.prepare_reply(jid, PERIODS[period][1] + '\n\n' + body)
                            current = await self.database.weather_subscription(jid)
                            if (send and valid() and current and current['revision'] == subscription['revision']):
                                result = send()
                                log.info('weather_alert_attempt sent=%s', result is not False)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Не включаем город, JID или сетевой URL в обычные журналы.
            log.warning('weather_alert_failed reason=delivery')

    async def close(self):
        self.closing = True
        self.wakeup.set()
        tasks = [t for t in (self.task, *self.jobs.values()) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
