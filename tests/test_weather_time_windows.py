from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from goose.weather_sources import met_no_report, seven_timer_report, yandex_report
from test_weather_pool import met_data, seven_data, yandex_data
from test_wttr import PLACE


NOW = datetime(2026, 11, 5, 10, 46, tzinfo=timezone.utc)
PARSERS = {'met_no': met_no_report, 'seven_timer': seven_timer_report, 'yandex': yandex_report}


def raw_forecast(provider, rain=lambda stamp, duration: False, cold=lambda stamp: False, now=NOW):
    zone = ZoneInfo(PLACE['timezone'])
    if provider == 'met_no':
        payload = met_data()
        payload['properties']['meta']['updated_at'] = now.isoformat()
        for entry in payload['properties']['timeseries']:
            stamp = datetime.fromisoformat(entry['time']).astimezone(zone)
            entry['data']['instant']['details'].update(
                air_temperature=-15 if cold(stamp) else 15, wind_speed=10 if cold(stamp) else 1)
            entry['data']['next_1_hours']['summary']['symbol_code'] = (
                'rain_day' if rain(stamp, timedelta(hours=1)) else 'clearsky_day')
    elif provider == 'seven_timer':
        payload = seven_data()
        for entry in payload['dataseries']:
            stamp = (datetime(2026, 11, 5, tzinfo=timezone.utc)
                     + timedelta(hours=entry['timepoint'])).astimezone(zone)
            entry['temp2m'] = -15 if cold(stamp) else 15
            entry['wind10m']['speed'] = 4 if cold(stamp) else 2
            entry['weather'] = 'rainday' if rain(stamp, timedelta(hours=3)) else 'clearday'
            entry['prec_type'] = 'rain' if rain(stamp, timedelta(hours=3)) else 'none'
    else:
        payload = yandex_data()
        payload['data']['serverTime'] = now.isoformat()
        point = payload['data']['weatherByPoint']
        point['now'].update(temperature=15, feelsLike=15, windSpeed=1, condition='CLEAR')
        for day in point['forecast']['days']:
            for entry in day['hours']:
                stamp = datetime.fromisoformat(entry['time']).astimezone(zone)
                entry.update(temperature=-15 if cold(stamp) else 15, windSpeed=10 if cold(stamp) else 1,
                             condition='RAIN' if rain(stamp, timedelta(hours=1)) else 'CLEAR')
    return payload


@pytest.mark.parametrize('provider', PARSERS)
def test_ended_morning_rain_cold_and_wind_do_not_produce_afternoon_advice(provider):
    def past(stamp):
        return stamp.date() == NOW.date() and stamp.hour < 10
    data = raw_forecast(provider, rain=lambda stamp, duration: past(stamp), cold=past)
    report = PARSERS[provider](data, PLACE, False, NOW)
    text = report.render(advice=True)
    assert 'На оставшуюся часть дня' in text and report.forecast.minimum == 15
    assert not report.forecast.rain_expected and report.forecast.rain_windows is None
    assert all(word not in text for word in ('зонт', 'шапку', 'потеплее', 'Дождь в прогнозных интервалах'))


@pytest.mark.parametrize('provider', PARSERS)
def test_clear_weather_and_later_rain_are_explained_by_forecast_windows(provider):
    data = raw_forecast(provider, rain=lambda stamp, duration: stamp.date() == NOW.date() and 18 <= stamp.hour < 21)
    report = PARSERS[provider](data, PLACE, False, NOW)
    text = report.render(advice=True)
    assert 'ясно' in text and 'зонт' in text
    assert 'Дождь в прогнозных интервалах: 18:00–21:00.' in text
    if provider == 'yandex':
        assert 'По данным на 13:46: ясно.' in text and 'В 14:00: ясно.' in text


@pytest.mark.parametrize('provider', PARSERS)
def test_already_started_but_unfinished_rain_interval_is_retained(provider):
    data = raw_forecast(provider, rain=lambda stamp, duration: stamp <= NOW < stamp + duration)
    report = PARSERS[provider](data, PLACE, False, NOW)
    assert report.forecast.rain_expected and 'зонт' in report.render(advice=True)
    assert report.forecast.rain_windows == ('12:00–15:00' if provider == 'seven_timer' else '13:00–14:00')


@pytest.mark.parametrize('provider', ['met_no', 'yandex'])
def test_rain_interval_ending_exactly_now_no_longer_causes_advice(provider):
    now = NOW.replace(minute=0)
    data = raw_forecast(provider, rain=lambda stamp, duration: stamp + duration == now, now=now)
    report = PARSERS[provider](data, PLACE, False, now)
    assert not report.forecast.rain_expected and 'зонт' not in report.render(advice=True)


@pytest.mark.parametrize('provider', PARSERS)
def test_tomorrow_keeps_the_whole_day_and_bounds_rain_at_local_midnight(provider):
    data = raw_forecast(provider, rain=lambda stamp, duration: stamp.date().isoformat() == '2026-11-06' and stamp.hour >= 21)
    report = PARSERS[provider](data, PLACE, True, NOW)
    assert report.day == '2026-11-06' and 'На завтра' in report.render()
    assert report.forecast.rain_windows == '21:00–24:00'
    assert 'зонт' in report.render(advice=True)


def test_separate_rain_periods_do_not_imply_rain_during_dry_hours():
    data = raw_forecast('yandex', rain=lambda stamp, duration: stamp.date() == NOW.date() and stamp.hour in (15, 16, 20))
    report = yandex_report(data, PLACE, False, NOW)
    assert report.forecast.rain_windows == '15:00–17:00, 20:00–21:00'


def test_last_hour_of_today_is_kept_without_tomorrow_rain():
    now = NOW.replace(hour=20, minute=59)
    data = raw_forecast('yandex', rain=lambda stamp, duration: stamp.date() > now.date(), now=now)
    report = yandex_report(data, PLACE, False, now)
    assert report.forecast.condition_minutes == 23 * 60
    assert not report.forecast.rain_expected and 'зонт' not in report.render(advice=True)


def test_repeated_dst_hour_does_not_revive_rain_from_first_occurrence():
    zone = ZoneInfo('America/New_York')
    now = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)  # второе 01:30, UTC−05
    start = datetime(2026, 11, 1, 4, tzinfo=timezone.utc)
    data = {'data': {'serverTime': now.isoformat(), 'weatherByPoint': {
        'now': {'temperature': 15, 'feelsLike': 15, 'windSpeed': 1, 'condition': 'CLEAR'},
        'forecast': {'days': [{'hours': [
            {'time': (start + timedelta(hours=h)).astimezone(zone).isoformat(),
             'temperature': -15 if h < 2 else 15, 'windSpeed': 10 if h < 2 else 1,
             'condition': 'RAIN' if h == 1 else 'CLEAR'} for h in range(25)]}]}}}}
    report = yandex_report(data, {**PLACE, 'timezone': zone.key}, False, now)
    assert report.day == '2026-11-01' and report.forecast.minimum == 15
    assert not report.forecast.rain_expected and 'зонт' not in report.render(advice=True)


@pytest.mark.parametrize('city_zone,clock,offset', [
    ('Europe/Moscow', '13:46', 'UTC+03:00'),
    ('Asia/Kolkata', '16:16', 'UTC+05:30'),
    ('America/New_York', '05:46', 'UTC−05:00'),
])
def test_yandex_observation_time_is_city_time_even_when_server_time_is_utc(city_zone, clock, offset):
    place = {**PLACE, 'timezone': city_zone}
    data = yandex_data(place)
    data['data']['serverTime'] = NOW.isoformat()
    text = yandex_report(data, place, False, NOW).render()
    assert f'По данным на {clock}:' in text and f'Время местное: {offset}.' in text
