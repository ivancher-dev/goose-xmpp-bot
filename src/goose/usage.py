"""Учёт API без текстов сообщений; область диалога изолирована по asyncio-задачам."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, time as day_time, timedelta
from decimal import Decimal, ROUND_HALF_UP
import asyncio
import json
import logging
import re
import time
import uuid
from zoneinfo import ZoneInfo

from .config import ConfigError

log = logging.getLogger('goose')
_scope = ContextVar('goose_api_usage', default=None)
OUTCOMES = frozenset({'success', 'authentication', 'provider', 'invalid_response',
                     'network', 'timeout', 'cancelled', 'interrupted', 'not_sent',
                     'http_error', 'unavailable', 'rate_limited'})
MAX_ROWS = 100000
RETENTION_DAYS = 400


@contextmanager
def usage_scope(database, jid):
    token = _scope.set((database, jid))
    try:
        yield
    finally:
        _scope.reset(token)


def token_usage(payload):
    """Недостоверный usage не отнимает обычный ответ и не превращается в ноль."""
    empty = (None, None, None)
    try:
        data = json.loads(payload)
        value = data.get('usage') if isinstance(data, dict) else None
        if not isinstance(value, dict):
            return empty
        counts = [value.get(k) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')]
        if any(v is not None and (type(v) is not int or not 0 <= v <= (2*10**9 if i == 2 else 10**9))
               for i, v in enumerate(counts)):
            return empty
        prompt, completion, total = counts
        if prompt is not None and completion is not None:
            if total is not None and total != prompt + completion:
                return empty
            total = prompt + completion
        elif total is not None and any(v is not None and v > total for v in (prompt, completion)):
            return empty
        return prompt, completion, total
    except (ValueError, TypeError, RecursionError):
        return empty


class RecordingError(Exception):
    pass


class Attempt:
    def __init__(self, config, call_id, number, clock=time.time):
        self.scope = _scope.get()
        self.config, self.call_id, self.number, self.clock = config, call_id, number, clock
        self.ident = uuid.uuid4().hex

    async def start(self):
        if self.scope is None:
            return
        database, jid = self.scope
        try:
            await database.start_api_usage(self.ident, self.call_id, jid, self.config.base_url,
                                           self.config.model, self.number, self.clock())
        except asyncio.CancelledError:
            # Database.call завершает транзакцию перед повторным выбросом отмены.
            await self.finish('not_sent')
            raise
        except Exception:
            log.error('api_usage_failed phase=start')
            raise RecordingError() from None

    async def finish(self, outcome, counts=(None, None, None)):
        if self.scope is None:
            return
        database, _ = self.scope
        try:
            await database.finish_api_usage(self.ident, outcome, counts, self.clock())
        except asyncio.CancelledError:
            raise
        except Exception:
            # Записанная попытка останется с неизвестным расходом; готовый ответ сохраняется.
            log.error('api_usage_failed phase=finish')


def period_bounds(value, timezone, now=None):
    zone = ZoneInfo(timezone)
    today = datetime.fromtimestamp(time.time() if now is None else now, zone).date()
    if value == 'today':
        first, monthly = today, False
    elif value == 'month':
        first, monthly = today.replace(day=1), True
    elif re.fullmatch(r'[0-9]{4}-[0-9]{2}(?:-[0-9]{2})?', value):
        monthly = len(value) == 7
        try:
            first = date.fromisoformat(value + '-01' if monthly else value)
        except ValueError:
            raise ConfigError('Период: today, month, YYYY-MM-DD или YYYY-MM.') from None
    else:
        raise ConfigError('Период: today, month, YYYY-MM-DD или YYYY-MM.')
    try:
        last = (date(first.year + 1, 1, 1) if first.month == 12 else date(first.year, first.month + 1, 1)) if monthly else first + timedelta(days=1)
    except (ValueError, OverflowError):
        raise ConfigError('Период вне поддерживаемого календаря.') from None
    label = first.strftime('%m.%Y' if monthly else '%d.%m.%Y')
    return (datetime.combine(first, day_time.min, zone).timestamp(),
            datetime.combine(last, day_time.min, zone).timestamp(), label)


def money(value, currency):
    if 0 < value < Decimal('0.000001'):
        return '<0,000001 ' + currency
    amount = format(value.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP), 'f').rstrip('0').rstrip('.')
    return (amount or '0').replace('.', ',') + ' ' + currency


async def usage_report(config, database, target, period='today', now=None):
    start, end, label = period_bounds(period, config.timezone, now)
    records, meta = await database.api_usage(start, end, target)
    keys = ('attempts', 'calls', 'retries', 'success', 'pending', 'prompt', 'completion',
            'total', 'prompt_known', 'completion_known', 'total_known', 'complete')
    totals = {key: sum(row[key] for row in records) for key in keys}
    count = totals['attempts']
    def tokens(key):
        known = totals[key + '_known']
        if not known:
            return 'нет данных'
        return str(totals[key]) + (' известных' if known < count else '')
    lines = [f'Статистика API — {label} ({config.timezone}).',
             'Диалог: ' + (target or 'все аккаунты') + '.',
             f'Обращений к модели: {totals["calls"]}; попыток API: {count}; повторных: {totals["retries"]}.',
             f'С текстовым ответом API: {totals["success"]}; прочих: {count-totals["success"]}.',
             f'Токены: вход — {tokens("prompt")}; выход — {tokens("completion")}; всего — {tokens("total")}.',
             f'Полные данные о токенах: {totals["complete"]} из {count} попыток.']
    pricing = config.usage
    if pricing.input_per_million is None:
        lines.append('Стоимость: тариф не задан.')
    else:
        priced = [r for r in records if r['model'] == pricing.price_model and r['base_url'] == config.llm.base_url]
        covered = sum(r['complete'] for r in priced)
        amount = sum((Decimal(r['priced_prompt']) * pricing.input_per_million +
                      Decimal(r['priced_completion']) * pricing.output_per_million for r in priced), Decimal(0)) / 10**6
        lines.append(f'Оценка по текущему тарифу за известные токены: ≈ {money(amount, pricing.currency)}.'
                     if covered else 'Оценка стоимости: нет полных данных по настроенному тарифу.')
        lines.append(f'Без оценки: {count-covered} попыток. Оценка без скидок и иных начислений; это не счёт провайдера.')
    if meta['started'] is None:
        lines.append('Учёт ещё не содержит API-попыток.')
    else:
        since = datetime.fromtimestamp(meta['started'], ZoneInfo(config.timezone)).strftime('%d.%m.%Y %H:%M')
        lines.append(f'Учёт начат: {since}.')
        if start < meta['started'] or (meta['cutoff'] is not None and start <= meta['cutoff']):
            lines.append('Период покрыт не полностью: показаны только сохранённые данные.')
    if totals['pending']:
        lines.append('Часть попыток ещё не завершена; расход по ним пока неизвестен.')
    if target is None and records:
        per_user = {}
        for row in records:
            per_user[row['jid']] = per_user.get(row['jid'], 0) + row['attempts']
        lines.append('По аккаунтам — попытки API:')
        for jid, attempts in sorted(per_user.items()):
            part = f'{jid}: {attempts}.'
            if len('\n'.join(lines + [part])) > 3600:
                lines.append('Список сокращён; подробности доступны запросом по конкретному аккаунту.')
                break
            lines.append(part)
    lines.append('Учтены автоматические обращения Гуся; отдельные doctor --api и внешние программы сюда не входят.')
    return '\n'.join(lines)
