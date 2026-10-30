"""Необязательный языковой блок: основной ответ отделён от проверяемых метаданных."""

import hashlib
import json
import re

from .config import ConfigError

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
        '\n\nЯзыковые комментарии включены только для последнего сообщения пользователя. '
        'Сначала дай ПОЛНЫЙ обычный ответ на его вопрос простым текстом; он важнее разбора. '
        'Только если осталось место и есть уверенные полезные исправления, после ответа '
        'на отдельной строке напиши ' + MARKER + ', затем JSON без Markdown: '
        '{"language":"ru","notes":[{"source":"зилёная","correction":"зелёная",'
        '"rule":"Проверим словом зе́лень: е под ударением.","example":"Зелёная трава."}]}. '
        'language — язык проверяемого текста (ISO-код, mixed для смешанного); '
        'если язык или правило неясны, пропусти весь блок. Анализируй язык сообщения, '
        'не переводи его для проверки. Поясняй на языке сообщения или по явной просьбе. '
        'source — точный короткий фрагмент последнего сообщения, correction — исправление; '
        'rule и example обязательны, проверочное слово/правило не выдумывай. '
        'Учитывай текущий класс. Не меняй числа, имена, отрицания и смысл. '
        'Цитаты, условие задачи, код, URL, формулы, чужой текст и чатовые сокращения '
        'не выдавай за личные ошибки. Стилистический вкус не ошибка. '
        'Проверять явно указанный чужой фрагмент можно в основном ответе без приписывания ошибок ребёнку. '
        'Не оценивай грамотность, не считай все ошибки, не жалуйся Папану. '
        'Просьба без исправлений отменяет блок. Если замечаний нет, не добавляй даже метку. '
        f'Режим {preferences["mode"]}; максимум {preferences["max_notes"]} замечаний. '
        + ('Каждое правило до 240 символов, пример до 180.' if preferences['mode'] == 'brief'
           else 'Поясняй подробнее: правило до 600 символов, пример до 300.')
    )


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
        return text.strip()
    answer, tail = text[:index].rstrip(), text[index:]
    if (not answer or not preferences or not preferences['enabled'] or opted_out(body)
            or not tail.startswith(MARKER) or len(tail) > 12000):
        return answer
    try:
        value = json.loads(tail[len(MARKER):], object_pairs_hook=_unique_object)
        if (not isinstance(value, dict) or set(value) != {'language', 'notes'}
                or not isinstance(value['language'], str)
                or not re.fullmatch(r'(?:[a-z]{2,3}|mixed)', value['language'])
                or value['language'] in {'und', 'mul', 'zxx'}
                or not isinstance(value['notes'], list) or len(value['notes']) > 16):
            return answer
        editable = PROTECTED.sub(lambda match: ' ' * len(match[0]), body)
        notes, used = [], set()
        brief = preferences['mode'] == 'brief'
        for note in value['notes']:
            if (not isinstance(note, dict) or set(note) != {'source', 'correction', 'rule', 'example'}
                    or not all(_text(note.get(k), size) for k, size in
                               [('source', 160), ('correction', 160),
                                ('rule', 240 if brief else 600), ('example', 180 if brief else 300)])):
                continue
            source, correction = note['source'], note['correction']
            if (len(source.strip()) < 2 or source == correction or not _safe_change(source, correction)
                    or source not in editable or source in used):
                continue
            if not re.search(r'(?<!\w)' + re.escape(source) + r'(?!\w)', editable):
                continue
            used.add(source)
            notes.append(note)
            if len(notes) == preferences['max_notes']:
                break
        if not notes:
            return answer
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
        for i, note in enumerate(notes, 1):
            part = (f'\n{i}. «{note["source"]}» → «{note["correction"]}».\n'
                    f'{note["rule"]}\n{note["example"]}')
            if len(block + part) > (1100 if brief else 1800) or len(answer + '\n\n' + block + part) > 4096:
                break
            block += part
        return answer + '\n\n' + block if block != intro else answer
    except (ValueError, TypeError, KeyError, RecursionError):
        return answer
