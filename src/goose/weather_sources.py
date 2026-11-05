"""Нормализация публичных контрактов MET Norway, 7Timer и Яндекс Погоды."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import math
import re
from zoneinfo import ZoneInfo

from .weather_view import Forecast, Observation, WeatherReport

MET_NO = 'https://api.met.no/weatherapi/locationforecast/2.0/compact'
SEVEN_TIMER = 'https://www.7timer.info/bin/api.pl'
YANDEX = 'https://api.weather.yandex.ru/graphql/query'
WEATHER_PROVIDERS = ('yandex', 'wttr', 'open_meteo', 'met_no', 'seven_timer')
PROVIDER_HOSTS = {'yandex': 'api.weather.yandex.ru', 'wttr': 'wttr.in',
                  'open_meteo': 'api.open-meteo.com', 'met_no': 'api.met.no',
                  'seven_timer': 'www.7timer.info'}
MAX_SOURCE_AGE = 12 * 3600


def value(number, low, high):
    if type(number) not in (int, float) or not math.isfinite(number) or not low <= number <= high:
        raise ValueError('Invalid forecast number')
    return number


def instant(text):
    if not isinstance(text, str) or len(text) > 80:
        raise ValueError('Invalid timestamp')
    stamp = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Missing timezone')
    return stamp


def fresh(stamp, now, max_age=MAX_SOURCE_AGE):
    if not -300 <= (now - stamp).total_seconds() <= max_age:
        raise ValueError('Stale forecast')
    return stamp


def label(place):
    return ', '.join(dict.fromkeys(p for p in (place['name'], place.get('admin1'), place.get('country')) if p))


def aggregate(samples, place, tomorrow, now):
    """Минимум/максимум только доступных часов, а не выдуманные целые сутки."""
    zone = ZoneInfo(place['timezone'])
    day = (now.astimezone(zone) + timedelta(days=int(tomorrow))).date()
    chosen = sorted((s for s in samples if s['time'].astimezone(zone).date() == day), key=lambda s: s['time'])
    if not chosen or len({s['time'] for s in chosen}) != len(chosen):
        raise ValueError('Missing or duplicate forecast points')
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    end = start + timedelta(days=1)
    needed = start if tomorrow else max(start, now)
    if chosen[0]['time'] > needed + timedelta(hours=3) or chosen[-1]['time'] < end - timedelta(hours=3):
        raise ValueError('Incomplete forecast period')
    if any((b['time'] - a['time']).total_seconds() > 3 * 3600 for a, b in zip(chosen, chosen[1:])):
        raise ValueError('Forecast gap')
    representative = min(chosen, key=lambda s: abs((s['time'].astimezone(zone) - (start + timedelta(hours=12))).total_seconds()))
    temps = [value(s['temp'], -100, 70) for s in chosen]
    winds = [s['wind'] for s in chosen]
    wind = max(value(w, 0, 140) for w in winds) if all(w is not None for w in winds) else None
    probabilities = [s.get('probability') for s in chosen]
    probability = max(value(p, 0, 100) for p in probabilities) if all(p is not None for p in probabilities) else None
    first, last = (chosen[i]['time'].astimezone(zone) for i in (0, -1))
    forecast = Forecast(representative['condition'], min(temps), max(temps), probability,
                        'осадков', wind, representative['time'].astimezone(zone).hour * 60
                        + representative['time'].astimezone(zone).minute,
                        any(s['rain'] for s in chosen),
                        f'По доступным интервалам {first:%H:%M}–{last:%H:%M}')
    return day.isoformat(), zone, forecast, chosen


_MET_CLEAR = {'clearsky': 'ясно', 'fair': 'преимущественно ясно',
              'partlycloudy': 'переменная облачность', 'cloudy': 'пасмурно', 'fog': 'туман'}
_MET_PRECIP = {}
for _kind, _text in (('rain', 'дождь'), ('sleet', 'мокрый снег'), ('snow', 'снег')):
    for _strength, _adjective in (('', ''), ('light', 'слабые осадки: '), ('heavy', 'сильные осадки: ')):
        for _showers in ('', 'showers'):
            _name = _strength + _kind + _showers
            _MET_PRECIP[_name] = (_adjective + _text, _kind in {'rain', 'sleet'})
            _MET_PRECIP[_name + 'andthunder'] = ('гроза, ' + _adjective + _text, _kind in {'rain', 'sleet'})


def met_condition(code):
    if not isinstance(code, str):
        raise ValueError('Invalid weather symbol')
    base = re.sub(r'_(day|night|polartwilight)$', '', code)
    if base in _MET_CLEAR:
        return _MET_CLEAR[base], False
    return _MET_PRECIP.get(base, ('описание погоды недоступно', False))


def met_no_report(data, place, tomorrow, now):
    if data['type'] != 'Feature' or data['geometry']['type'] != 'Point':
        raise ValueError('Invalid geometry')
    lon, lat, *_ = data['geometry']['coordinates']
    if abs(value(lat, -90, 90) - place['latitude']) > .05 or abs(
            (value(lon, -180, 180) - place['longitude'] + 180) % 360 - 180) > .05:
        raise ValueError('Wrong forecast location')
    props = data['properties']
    issued = fresh(instant(props['meta']['updated_at']), now)
    units = props['meta']['units']
    if units.get('air_temperature') != 'celsius' or units.get('wind_speed') != 'm/s':
        raise ValueError('Invalid units')
    entries = props['timeseries']
    if not isinstance(entries, list) or not 1 <= len(entries) <= 400:
        raise ValueError('Invalid forecast points')
    samples = []
    for entry in entries:
        stamp = instant(entry['time'])
        point = entry['data']['instant']['details']
        period = next((entry['data'][key] for key in ('next_1_hours', 'next_6_hours', 'next_12_hours') if key in entry['data']), None)
        if period is None:
            continue
        condition, rain = met_condition(period['summary']['symbol_code'])
        probability = period.get('details', {}).get('probability_of_precipitation')
        if probability is not None and units.get('probability_of_precipitation') != '%':
            raise ValueError('Invalid probability unit')
        samples.append({'time': stamp, 'temp': point['air_temperature'], 'wind': point['wind_speed'],
                        'condition': condition, 'rain': rain, 'probability': probability})
    day, zone, forecast, _ = aggregate(samples, place, tomorrow, now)
    return WeatherReport(label(place), day, zone, forecast, 'MET Norway',
                         'https://api.met.no/ · CC BY 4.0 https://creativecommons.org/licenses/by/4.0/',
                         tomorrow, issued_at=issued)


_SEVEN_CONDITIONS = {'clear': 'ясно', 'pcloudy': 'переменная облачность',
                     'mcloudy': 'облачно', 'cloudy': 'пасмурно', 'humid': 'дымка',
                     'lightrain': 'небольшой дождь', 'oshower': 'временами ливень',
                     'ishower': 'местами ливень', 'lightsnow': 'небольшой снег',
                     'rain': 'дождь', 'snow': 'снег', 'rainsnow': 'дождь со снегом',
                     'ts': 'гроза', 'tsrain': 'гроза с дождём'}
_WIND_RANGES = {1: 'слабый, менее 0,3 м/с.', 2: '0,3–3,4 м/с.', 3: '3,4–8 м/с.',
                4: '8–10,8 м/с.', 5: '10,8–17,2 м/с.', 6: '17,2–24,5 м/с.',
                7: '24,5–32,6 м/с.', 8: 'более 32,6 м/с.'}


def seven_timer_report(data, place, tomorrow, now):
    if data['product'] != 'civil' or not isinstance(data['init'], str) or not re.fullmatch(r'[0-9]{10}', data['init']):
        raise ValueError('Invalid 7Timer product')
    issued = fresh(datetime.strptime(data['init'], '%Y%m%d%H').replace(tzinfo=timezone.utc), now)
    entries = data['dataseries']
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise ValueError('Invalid forecast points')
    samples = []
    for entry in entries:
        hours = entry['timepoint']
        if type(hours) is not int or not 0 <= hours <= 240:
            raise ValueError('Invalid forecast offset')
        code = entry['weather']
        if not isinstance(code, str):
            raise ValueError('Invalid condition')
        code = re.sub(r'(day|night)$', '', code)
        speed = entry.get('wind10m', {}).get('speed')
        if speed is not None and (type(speed) is not int or speed not in _WIND_RANGES):
            speed = None
        samples.append({'time': issued + timedelta(hours=hours), 'temp': entry['temp2m'], 'wind': None,
                        'condition': _SEVEN_CONDITIONS.get(code, 'описание погоды недоступно'),
                        'rain': entry.get('prec_type') in {'rain', 'frzr', 'icep'}
                                or code in {'lightrain', 'oshower', 'ishower', 'rain', 'rainsnow', 'tsrain'},
                        'wind_category': speed})
    day, zone, forecast, chosen = aggregate(samples, place, tomorrow, now)
    bins = [s['wind_category'] for s in chosen]
    if all(b is not None for b in bins):
        forecast = replace(forecast, wind_description='максимальная категория ' + _WIND_RANGES[max(bins)])
    return WeatherReport(label(place), day, zone, forecast, '7Timer! (GFS)',
                         'https://www.7timer.info/', tomorrow, issued_at=issued)


_YA_CONDITIONS = dict(zip(
    ('CLEAR', 'PARTLY_CLOUDY', 'CLOUDY', 'OVERCAST', 'LIGHT_RAIN', 'RAIN', 'HEAVY_RAIN', 'SHOWERS',
     'SLEET', 'LIGHT_SNOW', 'SNOW', 'SNOWFALL', 'HAIL', 'THUNDERSTORM', 'THUNDERSTORM_WITH_RAIN', 'THUNDERSTORM_WITH_HAIL'),
    ('ясно', 'переменная облачность', 'облачно', 'пасмурно', 'небольшой дождь', 'дождь', 'сильный дождь', 'ливень',
     'мокрый снег', 'небольшой снег', 'снег', 'снегопад', 'град', 'гроза', 'гроза с дождём', 'гроза с градом')))
_YA_RAIN = {'LIGHT_RAIN', 'RAIN', 'HEAVY_RAIN', 'SHOWERS', 'SLEET', 'THUNDERSTORM_WITH_RAIN', 'THUNDERSTORM_WITH_HAIL'}


def yandex_query(place):
    lat, lon = value(place['latitude'], -90, 90), value(place['longitude'], -180, 180)
    return ('{ serverTime weatherByPoint(request: { lat: %s, lon: %s }) '
            '{ now { temperature feelsLike windSpeed condition } '
            'forecast { days { time hours { time temperature windSpeed condition } } } } }') % (lat, lon)


def yandex_report(data, place, tomorrow, now):
    if data.get('errors'):
        raise ValueError('GraphQL error')
    root = data['data']
    issued = fresh(instant(root['serverTime']), now, max_age=900)
    point = root['weatherByPoint']
    days = point['forecast']['days']
    if not isinstance(days, list) or not 1 <= len(days) <= 16:
        raise ValueError('Invalid forecast days')
    samples = []
    for day in days:
        hours = day['hours']
        if not isinstance(hours, list) or len(hours) > 26:
            raise ValueError('Invalid forecast hours')
        for hour in hours:
            code = hour['condition']
            if not isinstance(code, str):
                raise ValueError('Invalid condition')
            samples.append({'time': instant(hour['time']), 'temp': hour['temperature'], 'wind': hour['windSpeed'],
                            'condition': _YA_CONDITIONS.get(code, 'описание погоды недоступно'), 'rain': code in _YA_RAIN})
    day, zone, forecast, _ = aggregate(samples, place, tomorrow, now)
    observation = None
    if not tomorrow:
        current = point['now']
        observation = Observation(issued, _YA_CONDITIONS.get(current['condition'], 'описание погоды недоступно'),
                                  value(current['temperature'], -100, 70), value(current['feelsLike'], -120, 90),
                                  value(current['windSpeed'], 0, 140))
    return WeatherReport(label(place), day, zone, forecast, 'Яндекс Погода',
                         'https://yandex.ru/pogoda/', tomorrow, observation)
