"""Единая русская сводка: характер Гуся не меняет факты провайдера."""

from dataclasses import dataclass
from datetime import datetime
from random import Random


INTRODUCTIONS = (
    "Га-Га-Га! Вот что у нас с погодой:",
    "Император Вселенной повелевает погоде быть такой:",
    "Гусь-метеоролог сверился со сводкой:",
    "Гусик изучил прогноз и готов доложить:",
    "Погодная канцелярия Гусинландии докладывает:",
    "Гусман на связи. Вот погодная сводка:",
)
FAREWELLS = (
    "Метеорологический долг исполнен. Можно и на диван!",
    "Сводку передал. Перья поправил.",
    "Император удаляется по важным гусиным делам.",
    "На этом всё. Пойду проверю запасы Синтепониума.",
    "Погоду доложил, дальше — по гусиным делам.",
    "Га-Га-Га! Канцелярия погоды откланивается.",
)


class WeatherVoice:
    """Короткие реплики одного собеседника; саму сводку не изменяют."""

    def __init__(self, *, rng=None):
        self.rng = rng if rng is not None else Random()
        self.previous = {}

    def decorate(self, report):
        # Не больше одной реплики; четверть вариантов — спокойная сводка.
        kind = self.rng.choice(("intro", "intro", "outro", None))
        if kind is None:
            return report
        phrases = INTRODUCTIONS if kind == "intro" else FAREWELLS
        phrase = self.rng.choice(tuple(p for p in phrases if p != self.previous.get(kind)))
        self.previous[kind] = phrase
        return f"{phrase}\n\n{report}" if kind == "intro" else f"{report}\n\n{phrase}"


WWO_CONDITIONS = {
    113: "ясно", 116: "переменная облачность", 119: "облачно", 122: "пасмурно",
    143: "дымка", 176: "местами возможен дождь", 179: "местами возможен снег",
    182: "местами возможен мокрый снег", 185: "местами возможна переохлаждённая морось",
    200: "возможна гроза", 227: "позёмок", 230: "метель", 248: "туман",
    260: "переохлаждённый туман", 263: "местами слабая морось", 266: "слабая морось",
    281: "переохлаждённая морось", 284: "сильная переохлаждённая морось",
    293: "местами небольшой дождь", 296: "небольшой дождь",
    299: "временами умеренный дождь", 302: "умеренный дождь",
    305: "временами сильный дождь", 308: "сильный дождь",
    311: "небольшой ледяной дождь", 314: "умеренный или сильный ледяной дождь",
    317: "небольшой мокрый снег", 320: "умеренный или сильный мокрый снег",
    323: "местами небольшой снег", 326: "небольшой снег", 329: "местами умеренный снег",
    332: "умеренный снег", 335: "местами сильный снег", 338: "сильный снег",
    350: "ледяная крупа", 353: "небольшой ливень", 356: "умеренный или сильный ливень",
    359: "очень сильный ливень", 362: "небольшие ливневые осадки с мокрым снегом",
    365: "умеренные или сильные ливневые осадки с мокрым снегом",
    368: "небольшой снежный ливень", 371: "умеренный или сильный снежный ливень",
    374: "небольшие ливневые осадки с ледяной крупой",
    377: "умеренные или сильные ливневые осадки с ледяной крупой",
    386: "местами небольшой дождь с грозой", 389: "умеренный или сильный дождь с грозой",
    392: "местами небольшой снег с грозой", 395: "умеренный или сильный снег с грозой",
}


@dataclass(frozen=True)
class Observation:
    stamp: datetime
    condition: str
    temperature: float
    feels_like: float
    wind_ms: float
    dated: bool = True


@dataclass(frozen=True)
class Forecast:
    condition: str
    minimum: float
    maximum: float
    probability: float
    precipitation: str
    wind_ms: float | None = None
    condition_minutes: int | None = None


def decimal(value, *, signed=False):
    rounded = round(value, 1)
    if rounded == 0:
        rounded = 0.0
    prefix = "+" if signed and rounded > 0 else ""
    return prefix + f"{rounded:.1f}".rstrip("0").rstrip(".").replace(".", ",")


def render_weather(place, day, zone, forecast, *, source, source_url, tomorrow=False, observation=None):
    date = datetime.strptime(day, "%Y-%m-%d").date()
    reference = observation.stamp if observation else datetime(date.year, date.month, date.day, 12, tzinfo=zone)
    offset = int(reference.utcoffset().total_seconds() / 60)
    clock_zone = f"UTC{'+' if offset >= 0 else '−'}{abs(offset) // 60:02d}:{abs(offset) % 60:02d}"
    lines = [f"{place} — {'завтра' if tomorrow else 'сегодня'}, {date:%d.%m.%Y}.",
             f"Время местное: {clock_zone}.", ""]
    if observation:
        lines.extend([
            f"По данным на {observation.stamp:%H:%M}: {observation.condition}.",
            f"Температура: {decimal(observation.temperature, signed=True)} °C, "
            f"ощущается как {decimal(observation.feels_like, signed=True)} °C.",
            f"Ветер: {decimal(observation.wind_ms)} м/с.",
        ])
        if not observation.dated:
            lines.append("Дата наблюдения не указана источником.")
        lines.append("")
    lines.append(f"На день: от {decimal(forecast.minimum, signed=True)} до {decimal(forecast.maximum, signed=True)} °C.")
    if forecast.condition_minutes is None:
        lines.append(f"Условия: {forecast.condition}.")
    else:
        minute = forecast.condition_minutes
        lines.append(f"В {minute // 60:02d}:{minute % 60:02d}: {forecast.condition}.")
    lines.append("Ветер по прогнозу: " + (f"до {decimal(forecast.wind_ms)} м/с."
                                        if forecast.wind_ms is not None else "нет данных."))
    lines.extend([f"Вероятность {forecast.precipitation} по интервалам: до {decimal(forecast.probability)}%.", "",
                  f"Источник: {source} — {source_url}"])
    return "\n".join(lines)
