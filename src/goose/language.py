"""Необязательный языковой блок: основной ответ отделён от проверяемых метаданных."""

import hashlib
import json
import logging
import re

from .config import ConfigError

log = logging.getLogger('goose')
MARKER = '[[GOOSE_LANGUAGE]]'
PREFIX = '[[GOOSE_LANGUAGE'
DEFAULTS = {'enabled': False, 'mode': 'brief', 'max_notes': 2}
PROTECTED = re.compile(
    r'```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)|`[^`\n]+`|'
    r'\$\$.*?(?:\$\$|\Z)|\$[^$\n]+\$|https?://\S+|www\.\S+|'
    r'«[^»]*»|“[^”]*”|"[^"\n]*"|(?<!\w)\x27[^\x27\n]+\x27(?!\w)|^>[^\n]*|'
    r'^(?:Условие(?: задачи)?|Цитата|Код|Given|Quote|Task|Problem)\s*:[^\n]*',
    re.DOTALL | re.MULTILINE | re.I)
OPT_OUT = re.compile(
    r'\bбез\s+(?:исправлений|поправок|разбора\s+ошибок)|'
    r'\bне\s+(?:исправляй|поправляй|проверяй\s+(?:мои\s+)?ошибки)|'
    r'\b(?:no\s+corrections|without\s+corrections|don[’\x27]t\s+correct|do\s+not\s+correct)\b', re.I)
NEGATIONS = re.compile(r'\b(?:не|ни|нет|not|no|never|cannot|don[’\x27]t|can[’\x27]t)\b', re.I)
WORD = re.compile(r'[^\W\d_][\w’\x27-]*', re.UNICODE)


def preference_value(name, value):
    valid = ((name == 'enabled' and type(value) is bool)
             or (name == 'mode' and value in ('brief', 'detailed'))
             or (name == 'max_notes' and type(value) is int and 1 <= value <= 5))
    if not valid:
        raise ConfigError('Языковые комментарии: on/off, mode=brief|detailed, max_notes=1..5.')
    return value


def opted_out(body):
    return bool(OPT_OUT.search(PROTECTED.sub(' ', body)))


def instructions(preferences):
    return (
        'Проверь ошибки ТОЛЬКО в последнем сообщении пользователя, на языке этого текста. '
        'Дай полный ответ на вопрос, затем новую строку ' + MARKER + ' и JSON без Markdown: '
        '{"language":"ru","notes":[{"source":"зилёная","correction":"зелёная",'
        '"rule":"Проверим: зе́лень.","example":"Зелёная трава."}]}. '
        'Уверенную ошибку исправь. Нет уверенности или места — notes:[], но основной ответ сохрани. '
        'language: ISO-код, mixed или und. source — дословный фрагмент; correction — исправление; '
        'rule — верное правило, example — пример. Не выдумывай проверочных слов. '
        'Поясняй по классу, на языке сообщения или по явной просьбе. '
        'Цитаты, чужое условие, код, формулы и URL не личные ошибки; не трогай стиль/сокращения, '
        'имена, числа и смысл. Без оценок и жалоб Папану. «Без исправлений» — notes:[]. '
        f'Не более {preferences["max_notes"]} заметок. '
        + ('Кратко: rule до 240 символов, example до 180.' if preferences['mode'] == 'brief'
           else 'Подробно: rule до 600 символов, example до 300.')
    )


def _skipped(reason, answer):
    # Только закрытый код причины; никаких фрагментов, JID или ответов API.
    log.info('language_feedback_skipped reason=%s', reason)
    return answer


def _text(value, maximum):
    return (isinstance(value, str) and 0 < len(value.strip()) <= maximum
            and all(0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD
                    or 0x10000 <= ord(c) <= 0x10FFFF for c in value) and PREFIX not in value)


def _safe_change(source, correction):
    # Консервативная защита: изменения слов с заглавной буквы тоже пропускаются.
    numbers = lambda text: [re.sub(r'\s+', '', number) for number in
                            re.findall(r'[+−-]?\s*\d+(?:[.,]\d+)*', text)]
    if numbers(source) != numbers(correction):
        return False
    if [s.casefold() for s in NEGATIONS.findall(source)] != [s.casefold() for s in NEGATIONS.findall(correction)]:
        return False
    capitals = lambda text: [word for word in WORD.findall(text) if word[0].isupper()]
    return capitals(source) == capitals(correction)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def render_feedback(text, body, preferences=None):
    """Сломанный/обрезанный необязательный блок отбрасывается, ответ не сокращается."""
    index = text.find(PREFIX)
    if index < 0:
        if preferences and preferences['enabled']:
            return _skipped('missing_metadata', text.strip())
        return text.strip()
    answer, tail = text[:index].rstrip(), text[index:]
    if not preferences or not preferences['enabled']:
        return answer
    if not answer:
        return _skipped('no_answer', answer)
    if opted_out(body):
        return _skipped('opt_out', answer)
    if not tail.startswith(MARKER) or len(tail) > 12000:
        return _skipped('invalid_metadata', answer)
    try:
        value = json.loads(tail[len(MARKER):], object_pairs_hook=_unique_object)
        if (not isinstance(value, dict) or set(value) != {'language', 'notes'}
                or not isinstance(value['language'], str)
                or not re.fullmatch(r'(?:[a-z]{2,3}|mixed)', value['language'])
                or not isinstance(value['notes'], list) or len(value['notes']) > 16):
            return _skipped('invalid_metadata', answer)
        if value['language'] in {'und', 'mul', 'zxx'}:
            return _skipped('uncertain_language', answer)
        if not value['notes']:
            return _skipped('no_notes', answer)
        editable = PROTECTED.sub(lambda match: ' ' * len(match[0]), body)
        notes, used = [], set()
        rejected = {'format': 0, 'change': 0, 'source': 0, 'duplicate': 0}
        brief = preferences['mode'] == 'brief'
        for note in value['notes']:
            if (not isinstance(note, dict) or set(note) != {'source', 'correction', 'rule', 'example'}
                    or not all(_text(note.get(k), size) for k, size in
                               [('source', 160), ('correction', 160),
                                ('rule', 240 if brief else 600), ('example', 180 if brief else 300)])):
                rejected['format'] += 1
                continue
            source, correction = note['source'], note['correction']
            if len(source.strip()) < 2 or source == correction or not _safe_change(source, correction):
                rejected['change'] += 1
                continue
            if source not in editable or not re.search(r'(?<!\w)' + re.escape(source) + r'(?!\w)', editable):
                rejected['source'] += 1
                continue
            if source in used:
                rejected['duplicate'] += 1
                continue
            used.add(source)
            notes.append(note)
            if len(notes) == preferences['max_notes']:
                break
        if any(rejected.values()):
            log.info('language_feedback_filtered format=%d change=%d source=%d duplicate=%d',
                     rejected['format'], rejected['change'], rejected['source'], rejected['duplicate'])
        if not notes:
            return _skipped('filtered_notes', answer)
        rules = ' '.join(note['rule'] for note in notes)
        if re.search('[А-Яа-яЁё]', rules):
            intros = ('Га-Га-Га! На минутку включаю гусиного редактора:',
                      'Гусик заметил кое-что полезное для письма:',
                      'Маленькая заметка от императорского редактора:')
        elif value['language'] == 'en':
            intros = ('Ga-ga-ga! A quick note from your goose editor:',)
        else:
            intros = ('✎',)
        intro = intros[hashlib.sha256(body.encode()).digest()[0] % len(intros)]
        block = intro
        count = 0
        for i, note in enumerate(notes, 1):
            part = (f'\n{i}. «{note["source"]}» → «{note["correction"]}».\n'
                    f'{note["rule"]}\n{note["example"]}')
            if len(block + part) > (1100 if brief else 1800) or len(answer + '\n\n' + block + part) > 4096:
                break
            block += part
            count += 1
        if not count:
            return _skipped('output_budget', answer)
        log.info('language_feedback_added notes=%d', count)
        return answer + '\n\n' + block
    except (ValueError, TypeError, KeyError, RecursionError):
        return _skipped('invalid_metadata', answer)
