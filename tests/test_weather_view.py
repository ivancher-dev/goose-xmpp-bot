from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo

import pytest

from goose.facts import weather_text, wttr_condition, wttr_weather_text
from goose.weather_view import Forecast, render_weather

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
PLACE = {"name": "Город", "admin1": "", "country": "Страна", "latitude": 55,
         "longitude": 37, "timezone": "Europe/Moscow"}


def provider_data(provider):
    if provider == "open_meteo":
        return {"timezone": "Europe/Moscow",
                "current": {"time": "2026-10-26T15:00", "temperature_2m": 10,
                            "apparent_temperature": 7, "weather_code": 3, "wind_speed_10m": 21},
                "current_units": {"temperature_2m": "°C", "apparent_temperature": "°C",
                                  "weather_code": "wmo code", "wind_speed_10m": "km/h"},
                "daily": {"time": ["2026-10-26", "2026-10-27"], "temperature_2m_min": [5, 4],
                          "temperature_2m_max": [12, 11], "precipitation_probability_max": [30, 40],
                          "weather_code": [3, 61], "wind_speed_10m_max": [36, 36]},
                "daily_units": {"temperature_2m_min": "°C", "temperature_2m_max": "°C",
                                "precipitation_probability_max": "%", "weather_code": "wmo code",
                                "wind_speed_10m_max": "km/h"}}
    return {"nearest_area": [{"latitude": "55", "longitude": "37"}],
            "current_condition": [{"observation_time": "11:00 AM", "temp_C": "10", "FeelsLikeC": "7",
                                   "windspeedKmph": "21", "weatherCode": "122",
                                   "lang_ru": [{"value": "Overcast"}]}],
            "weather": [{"date": day, "mintempC": "5", "maxtempC": "12", "hourly": [
                {"time": "0", "chanceofrain": "10", "windspeedKmph": "18", "weatherCode": "119"},
                {"time": "1200", "chanceofrain": "40", "windspeedKmph": "36", "weatherCode": "176",
                 "weatherDesc": [{"value": "Patchy rain nearby"}]}]} for day in ("2026-10-26", "2026-10-27")]}


def render(provider, data, tomorrow=False):
    renderer = weather_text if provider == "open_meteo" else wttr_weather_text
    return renderer(data, PLACE, tomorrow, NOW)


@pytest.mark.parametrize("provider", ["open_meteo", "wttr"])
@pytest.mark.parametrize("tomorrow", [False, True])
def test_both_providers_share_russian_goose_layout_and_convert_wind(provider, tomorrow):
    result = render(provider, provider_data(provider), tomorrow)
    assert result.startswith("Га-Га-Га! Гусман докладывает о погоде!\n\nГород, Страна")
    assert ("завтра, 27.10.2026" if tomorrow else "сегодня, 26.10.2026") in result
    assert "Время местное: UTC+03:00." in result
    assert "Ветер по прогнозу: до 10 м/с." in result  # 36 км/ч = 10 м/с
    assert "км/ч" not in result and "Europe/Moscow" not in result
    assert "Императорская сводка готова — возвращаюсь на диван!" in result
    if tomorrow:
        assert "По данным на" not in result and "ощущается" not in result
    else:
        assert "Ветер: 5,8 м/с." in result  # 21 км/ч = 5,833... м/с
        assert "Температура: +10 °C, ощущается как +7 °C." in result
    description = result.split("Источник:")[0].replace("UTC", "").replace("°C", "")
    assert not re.search(r"[A-Za-z]", description)


@pytest.mark.parametrize("entry,expected", [
    ({"weatherCode": "176", "weatherDesc": [{"value": "Patchy rain nearby"}]}, "местами возможен дождь"),
    ({"weatherCode": "113", "lang_ru": [{"value": "Sunny"}]}, "ясно"),
    ({"weatherCode": "311"}, "небольшой ледяной дождь"),
    ({"weatherCode": "999", "weatherDesc": [{"value": "Unknown conditions"}]}, "описание погоды недоступно"),
    ({"lang_ru": [{"value": "<b>Облачно</b>"}]}, "облачно"),
    ({"lang_ru": [{"value": "Unknown weather"}]}, "описание погоды недоступно"),
    ({"lang_ru": [{"value": None}]}, "описание погоды недоступно"),
])
def test_wwo_codes_translate_conditions_without_model_or_english_fallback(entry, expected):
    assert wttr_condition(entry) == expected


@pytest.mark.parametrize("provider", ["open_meteo", "wttr"])
@pytest.mark.parametrize("missing", [True, False])
def test_missing_forecast_wind_is_not_replaced_by_observed_wind(provider, missing):
    data = provider_data(provider)
    if provider == "open_meteo":
        if missing:
            del data["daily"]["wind_speed_10m_max"]
        else:
            data["daily"]["wind_speed_10m_max"][0] = None
    else:
        if missing:
            del data["weather"][0]["hourly"][0]["windspeedKmph"]
        else:
            data["weather"][0]["hourly"][0]["windspeedKmph"] = None
    result = render(provider, data)
    assert "Ветер: 5,8 м/с." in result and "Ветер по прогнозу: нет данных." in result


@pytest.mark.parametrize("provider", ["open_meteo", "wttr"])
def test_wrong_wind_unit_or_invalid_speed_is_rejected(provider):
    data = provider_data(provider)
    if provider == "open_meteo":
        data["daily_units"]["wind_speed_10m_max"] = "mph"
    else:
        data["weather"][0]["hourly"][0]["windspeedKmph"] = "-36"
    with pytest.raises(ValueError):
        render(provider, data)


@pytest.mark.parametrize("zone,day,expected", [
    ("America/New_York", "2026-11-01", "UTC−05:00"),  # Переход с летнего времени в день прогноза.
    ("Asia/Kathmandu", "2026-10-27", "UTC+05:45"),
])
def test_forecast_timezone_offset_uses_target_date_and_preserves_partial_hours(zone, day, expected):
    report = render_weather("Город", day, ZoneInfo(zone), Forecast("ясно", 0, 5, 10, "осадков"),
                            source="Open-Meteo", source_url="https://open-meteo.com/", tomorrow=True)
    assert expected in report and zone not in report


def test_current_date_warning_and_real_observation_time_survive_the_character_voice():
    result = render("wttr", provider_data("wttr"))
    assert "По данным на 14:00: пасмурно." in result
    assert "Дата наблюдения не указана источником." in result
    assert "Вероятность дождя по интервалам: до 40%." in result
    assert "В 12:00: местами возможен дождь." in result
