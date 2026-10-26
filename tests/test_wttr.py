from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

from goose.cli import doctor
from goose.config import ConfigError, WeatherConfig, load_config
from goose.facts import Facts, location, wttr_weather_text

PARENT, CHILD = "parent@example.org", "pupil1@example.org"
NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
PLACE = {"id": 10, "name": "Город", "admin1": "Область", "country": "Страна",
         "latitude": 55.0, "longitude": 37.0, "timezone": "Europe/Moscow"}


def payload():
    return {"nearest_area": [{"latitude": "55.0", "longitude": "37.0"}],
            "current_condition": [{"localObsDateTime": "2026-10-26 03:00 PM", "temp_C": "10",
                "FeelsLikeC": "8", "windspeedKmph": "12", "weatherCode": "122", "weatherDesc": [{"value": "Overcast"}],
                "lang_ru": [{"value": "Пасмурно"}]}],
            "weather": [{"date": day, "mintempC": "5", "maxtempC": "12", "hourly": [
                {"time": "0", "chanceofrain": "10", "windspeedKmph": "18", "weatherCode": "119", "weatherDesc": [{"value": "Cloudy"}]},
                {"time": "1200", "chanceofrain": "60", "windspeedKmph": "25.2", "weatherCode": "176", "weatherDesc": [{"value": "Patchy rain nearby"}]}]} for day in ("2026-10-26", "2026-10-27")]}


def test_current_and_tomorrow_use_explicit_units_source_and_forecast_intervals():
    data = payload()
    current = wttr_weather_text(data, PLACE, False, NOW)
    assert "По данным на 15:00: пасмурно." in current
    assert "Температура: +10 °C, ощущается как +8 °C." in current
    assert "Ветер: 3,3 м/с." in current and "Ветер по прогнозу: до 7 м/с." in current
    assert "от +5 до +12 °C" in current and "В 12:00: местами возможен дождь" in current
    assert "по интервалам: до 60%" in current and "Источник: wttr.in" in current
    assert "UTC+03:00" in current
    tomorrow = wttr_weather_text(data, PLACE, True, NOW)
    assert "завтра, 27.10.2026" in tomorrow and "По данным на" not in tomorrow


def test_new_york_date_is_local_and_hourly_slot_is_not_presented_as_whole_day():
    place = {**PLACE, "timezone": "America/New_York"}
    data = payload()
    data["current_condition"][0]["localObsDateTime"] = "2026-10-26 21:00"
    for day in data["weather"]:
        day["hourly"] = day["hourly"][:1]
    result = wttr_weather_text(data, place, True, datetime(2026, 10, 27, 1, tzinfo=timezone.utc))
    assert "завтра, 27.10.2026" in result and "UTC−04:00" in result and "В 00:00: облачно" in result


def time_only_payload(observed="11:00 AM"):
    data = payload()
    del data["current_condition"][0]["localObsDateTime"]
    data["current_condition"][0]["observation_time"] = observed
    return data


@pytest.mark.parametrize("optional", ["absent", None, ""])
def test_missing_optional_local_date_uses_utc_time_and_discloses_unknown_date(optional):
    data = time_only_payload()
    if optional != "absent":
        data["current_condition"][0]["localObsDateTime"] = optional
    result = wttr_weather_text(data, PLACE, False, NOW)
    assert "По данным на 14:00: пасмурно." in result  # 11:00 UTC, не 11:00 местного времени
    assert "Дата наблюдения не указана источником." in result
    tomorrow = wttr_weather_text(data, PLACE, True, NOW)
    assert "завтра, 27.10.2026" in tomorrow and "По данным на" not in tomorrow


def test_utc_time_without_date_rolls_over_utc_midnight():
    data = time_only_payload("11:00 PM")
    data["weather"][0]["date"], data["weather"][1]["date"] = "2026-10-27", "2026-10-28"
    result = wttr_weather_text(data, PLACE, False, datetime(2026, 10, 27, 0, 30, tzinfo=timezone.utc))
    assert "сегодня, 27.10.2026" in result and "UTC+03:00" in result and "По данным на 02:00" in result


def test_date_free_utc_observation_uses_the_chosen_city_timezone():
    data = time_only_payload("12:30 AM")
    result = wttr_weather_text(data, {**PLACE, "timezone": "America/New_York"}, False,
                               datetime(2026, 10, 27, 1, tzinfo=timezone.utc))
    assert "сегодня, 26.10.2026" in result and "UTC−04:00" in result and "По данным на 20:30" in result


@pytest.mark.parametrize("failure", ["stale-hour", "future-hour", "old-forecast", "missing-clock", "bad-clock"])
def test_date_free_observation_still_rejects_invalid_or_stale_data(failure):
    data = time_only_payload()
    if failure == "stale-hour":
        data["current_condition"][0]["observation_time"] = "06:00 AM"
    elif failure == "future-hour":
        data["current_condition"][0]["observation_time"] = "01:00 PM"
    elif failure == "old-forecast":
        data["weather"][0]["date"] = "2026-10-25"
    elif failure == "missing-clock":
        del data["current_condition"][0]["observation_time"]
    else:
        data["current_condition"][0]["observation_time"] = "25:61"
    with pytest.raises((ValueError, KeyError)):
        wttr_weather_text(data, PLACE, False, NOW)


@pytest.mark.parametrize("local_date", ["2026-10-25 03:00 PM", "not-a-date"])
def test_stale_dated_observation_cannot_be_hidden_by_recent_time_only_field(local_date):
    data = time_only_payload()
    data["current_condition"][0]["localObsDateTime"] = local_date
    with pytest.raises(ValueError):
        wttr_weather_text(data, PLACE, False, NOW)


@pytest.mark.parametrize("failure", ["old", "future", "missing-observation", "missing-day", "bad-number",
    "bad-wind", "empty-hourly", "bad-hour", "wrong-location", "no-timezone", "malformed-root"])
def test_unusable_data_is_rejected_without_invented_weather(failure):
    data, place = payload(), deepcopy(PLACE)
    if failure == "old":
        data["current_condition"][0]["localObsDateTime"] = "2026-10-25 03:00 PM"
    elif failure == "future":
        data["current_condition"][0]["localObsDateTime"] = "2026-10-27 03:00 PM"
    elif failure == "missing-observation":
        del data["current_condition"][0]["localObsDateTime"]
    elif failure == "missing-day":
        data["weather"] = []
    elif failure == "bad-number":
        data["weather"][0]["maxtempC"] = "NaN"
    elif failure == "bad-wind":
        data["current_condition"][0]["windspeedKmph"] = "-10"
    elif failure == "empty-hourly":
        data["weather"][0]["hourly"] = []
    elif failure == "bad-hour":
        data["weather"][0]["hourly"][0]["time"] = "1260"
    elif failure == "wrong-location":
        data["nearest_area"][0]["latitude"] = "0"
    elif failure == "no-timezone":
        del place["timezone"]
    else:
        data = []
    with pytest.raises((ValueError, KeyError, TypeError)):
        wttr_weather_text(data, place, False, NOW)


@pytest.mark.parametrize("has_local_date", [True, False])
async def test_old_saved_city_is_enriched_without_changing_preferences(config, database, has_local_date):
    old = {key: value for key, value in PLACE.items() if key != "timezone"}
    await database.set_weather_location(PARENT, old)
    calls = []
    def route(request):
        calls.append(request)
        if request.url.host == "geocoding-api.open-meteo.com":
            assert request.url.path == "/v1/get" and request.url.params["id"] == "10"
            return httpx.Response(200, json=PLACE)
        assert request.url.host == "wttr.in" and request.url.path == "/55.000000,37.000000"
        assert dict(request.url.params) == {"format": "j1", "lang": "ru"}
        return httpx.Response(200, json=payload() if has_local_date else time_only_payload())
    config = replace(config, weather=WeatherConfig(True, "wttr"))
    engine = Facts(config, database, None, transport=httpx.MockTransport(route), now=lambda: NOW)
    try:
        result = await engine.answer(PARENT, "weather", "")
        assert "10 °C" in result and "wttr.in" in result
        assert await database.weather_location(PARENT) == old
        assert await database.weather_location(CHILD) is None
        assert len(calls) == 2
        # Второй запрос получает общие публичные данные из ограниченного кэша.
        assert "завтра" in await engine.answer(PARENT, "weather", "tomorrow")
        assert len(calls) == 2
        assert not any(request.url.host == "api.open-meteo.com" for request in calls)
        assert all("example.org" not in str(request.url) for request in calls)
    finally:
        await engine.close()


async def test_one_off_query_with_timezone_never_changes_default(config, database):
    await database.set_weather_location(PARENT, PLACE)
    other = {**PLACE, "id": 11, "name": "Другой город"}
    calls = []
    def route(request):
        calls.append(request)
        if request.url.path == "/v1/search":
            return httpx.Response(200, json={"results": [other]})
        return httpx.Response(200, json=payload())
    engine = Facts(replace(config, weather=WeatherConfig(True, "wttr")), database, None,
                   transport=httpx.MockTransport(route), now=lambda: NOW)
    try:
        result = await engine.answer(PARENT, "weather", "Другой город")
        assert "Другой город" in result
        assert await database.weather_location(PARENT) == PLACE
        assert len(calls) == 2
    finally:
        await engine.close()


def test_timezone_is_preserved_in_new_city_and_invalid_zone_is_rejected():
    assert location(PLACE) == PLACE
    with pytest.raises(KeyError):
        location({**PLACE, "timezone": "Invalid/Zone"})


def test_provider_configuration_and_doctor_without_network(config_file, capsys):
    config_file.write_text(config_file.read_text().replace('provider = "open_meteo"', 'provider = "wttr"')
                           .replace('[weather]\nenabled = false', '[weather]\nenabled = true'))
    config = load_config(config_file)
    assert config.weather == WeatherConfig(True, "wttr")
    doctor(config)
    assert "Источник погоды: wttr" in capsys.readouterr().out
    config_file.write_text(config_file.read_text().replace('provider = "wttr"', 'provider = "https://arbitrary.example.org"'))
    with pytest.raises(ConfigError, match="weather.provider"):
        load_config(config_file)
