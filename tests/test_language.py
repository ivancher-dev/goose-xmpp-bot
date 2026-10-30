"""Синтетические языки и данные; качество реальной модели проверяется отдельно."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3

import pytest
import pytest_asyncio

from goose import language as lang
from goose.commands import Commands
from goose.config import ConfigError
from goose.dialogue import Dialogues, estimate_tokens
from goose.model import OpenAICompatible
from goose.model import ModelError
from goose.storage import Database, OmemoStorage

PARENT, CHILD, OTHER = 'parent@example.org', 'pupil1@example.org', 'pupil2@example.org'
PREF = {**lang.DEFAULTS, 'enabled': True}
NOTE = {'source': 'зилёная', 'correction': 'зелёная',
        'rule': 'Проверим словом зе́лень: е под ударением.', 'example': 'Зелёная трава.'}


def packet(answer='Ответ на вопрос.', notes=None, language='ru'):
    return answer + '\n' + lang.MARKER + '\n' + json.dumps(
        {'language': language, 'notes': [dict(NOTE)] if notes is None else notes}, ensure_ascii=False)


class Model:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.result = packet()

    async def complete(self, context):
        self.calls.append(deepcopy(context))
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            # Недоверенный поздний ответ после отмены.
            await self.release.wait()
        return self.result

    async def close(self):
        pass


@pytest_asyncio.fixture
async def system(config, database, tmp_path):
    persona = tmp_path / 'language-persona.md'
    persona.write_text('Добрый семейный Гусь. Отвечай понятно.')
    enabled = replace(config, llm=replace(config.llm, enabled=True, persona_file=persona))
    model, sent = Model(), []
    async def prepare(jid, text):
        def send():
            sent.append((jid, text))
            return True
        return send
    engine = Dialogues(enabled, database, model, prepare)
    await engine.initialize()
    try:
        yield engine, model, sent, Commands(enabled, database, dialogues=engine)
    finally:
        model.release.set()
        await engine.close()


async def idle(engine):
    if engine.tasks:
        await asyncio.wait_for(asyncio.gather(*tuple(engine.tasks), return_exceptions=True), 2)


async def test_independent_settings_owner_only_and_persistent(system, database, config):
    engine, model, sent, commands = system
    assert await database.language_preferences(CHILD) == lang.DEFAULTS
    before = await database.profile(CHILD)
    for body in (f'!admin language {CHILD} on', f'!admin language {OTHER} max_notes=5'):
        assert 'владельцу' in await commands.handle(CHILD + '/phone', body)
    assert await commands.handle('stranger@example.org', f'!admin language {CHILD} on') is None
    assert await database.language_preferences(CHILD) == lang.DEFAULTS
    assert 'включены' in await commands.handle(PARENT + '/pc', f'!admin language {CHILD} on')
    await commands.handle(PARENT, f'!admin language {CHILD} mode=detailed')
    await commands.handle(PARENT, f'!admin language {CHILD} max_notes=3')
    expected = {'enabled': True, 'mode': 'detailed', 'max_notes': 3}
    reopened = Database(config.data_dir, config.jid)
    await reopened.initialize(config.users)
    assert await reopened.language_preferences(CHILD) == expected
    assert await reopened.language_preferences(OTHER) == lang.DEFAULTS
    assert await database.profile(CHILD) == before
    await engine.clear(CHILD)
    assert await database.language_preferences(CHILD) == expected
    await commands.handle(PARENT, f'!admin language {CHILD} off')
    assert await database.language_preferences(CHILD) == {**expected, 'enabled': False}
    assert not model.calls and not sent


@pytest.mark.parametrize('option', ['enabled=true', 'mode=all', 'max_notes=0', 'max_notes=6',
    'max_notes=true', 'max_notes=2;DROP', 'language=ru', 'on extra'])
async def test_invalid_settings_leave_both_dialogues_unchanged(system, database, option):
    _, _, _, commands = system
    before = await database.language_preferences(CHILD)
    reply = await commands.handle(PARENT, f'!admin language {CHILD} {option}')
    assert reply and 'включены' not in reply
    assert await database.language_preferences(CHILD) == before
    assert await database.language_preferences(OTHER) == lang.DEFAULTS


@pytest.mark.parametrize('target', ['missing@example.org', CHILD + '/phone', PARENT])
async def test_invalid_targets_do_not_create_settings(system, database, target):
    _, _, _, commands = system
    assert 'включены' not in await commands.handle(PARENT, f'!admin language {target} on')
    assert await database.call(lambda c: c.execute('SELECT COUNT(*) FROM language_feedback').fetchone()[0]) == 0


async def test_main_answer_then_feedback_one_request_no_rewriting_and_isolation(system, database):
    engine, model, sent, commands = system
    await commands.handle(PARENT, f'!admin language {CHILD} on')
    for jid in (CHILD, OTHER):
        await engine.set_mode(jid, 'auto')
    await engine.submit(CHILD + '/phone', 'Почему трава зилёная?')
    await engine.submit(OTHER, 'Отдельный вопрос без замечаний')
    await idle(engine)
    assert len(model.calls) == 2
    first = next(c for c in model.calls if c[-1]['content'] == 'Почему трава зилёная?')
    second = next(c for c in model.calls if c[-1]['content'] == 'Отдельный вопрос без замечаний')
    assert lang.MARKER in first[0]['content'] and lang.MARKER not in str(second)
    assert all('@example.org' not in str(context) for context in model.calls)
    assert 'Отдельный вопрос' not in str(first) and 'зилёная' not in str(second)
    child = next(text for jid, text in sent if jid == CHILD)
    assert child.startswith('Ответ на вопрос.') and '«зилёная» → «зелёная»' in child
    assert 'зе́лень' in child and lang.PREFIX not in child and '"notes"' not in child
    assert next(text for jid, text in sent if jid == OTHER) == 'Ответ на вопрос.'
    history = await database.history(CHILD, 100, 24, 30)
    assert history[0] == {'role': 'user', 'content': 'Почему трава зилёная?'}
    assert history[1]['content'] == child


async def test_current_word_table_and_index_reply_ignore_similar_old_questions(system, database):
    engine, model, sent, _ = system
    await database.add_user_message(CHILD, 'Другая старая ошибка в вопросе')
    await engine.update_language(CHILD, 'enabled', True)
    await engine.set_mode(CHILD, 'auto')
    note = {'start': 3, 'end': 3, 'correction': 'белый',
            'rule': 'Проверим: бе́ленький.', 'example': 'Белый снег.'}
    model.result = packet(notes=[note])
    await engine.submit(CHILD, 'Как растёт билый гриб?')
    await idle(engine)
    assert len(model.calls) == 1
    table = json.loads(model.calls[0][0]['content'].rsplit('\n', 1)[-1])
    assert table == {'1': 'Как', '2': 'растёт', '3': 'билый', '4': 'гриб?'}
    assert '«билый» → «белый»' in sent[0][1]
    history = await database.history(CHILD, 100, 24, 30)
    assert history[-2] == {'role': 'user', 'content': 'Как растёт билый гриб?'}


@pytest.mark.parametrize('body', ['Почему трава зилёная? Сейчас без исправлений.',
    'Не исправляй ошибки, почему трава зилёная?', 'No corrections: why is it green?',
    "Don't correct my writing. Why is it green?"])
async def test_one_turn_opt_out_preserves_setting_and_answer(system, database, body):
    engine, model, sent, _ = system
    await engine.update_language(CHILD, 'enabled', True)
    await engine.set_mode(CHILD, 'auto')
    await engine.submit(CHILD, body)
    await idle(engine)
    assert lang.MARKER not in str(model.calls[0])
    assert sent == [(CHILD, 'Ответ на вопрос.')]
    assert (await database.language_preferences(CHILD))['enabled']


def test_opt_out_inside_quoted_text_does_not_disable_feedback():
    assert not lang.opted_out('Фраза «без исправлений» написана верно?')


@pytest.mark.parametrize('tail', ['{', 'null', '[]', '{"language":"ru"}',
    '{"language":"ru","notes":[]}', '{"language":"und","notes":[]}',
    '{"language":"ru","language":"en","notes":[]}', '```json\n{}\n```', 'x' * 13000])
def test_broken_optional_metadata_does_not_lose_main_answer(tail):
    assert lang.render_feedback('Полный основной ответ.\n' + lang.MARKER + tail,
                                'Почему трава зилёная?', PREF) == 'Полный основной ответ.'


def test_truncated_protocol_or_output_keeps_main_answer():
    for marker in [lang.PREFIX, lang.PREFIX + ']', lang.MARKER]:
        assert lang.render_feedback('Ответ.\n' + marker, 'зилёная', PREF) == 'Ответ.'
    raw = packet('Ответ.', [{**NOTE, 'rule': 'я' * 5000}])
    clipped = OpenAICompatible.parse(json.dumps({'choices': [{'message': {'content': raw}}]}))
    assert lang.render_feedback(clipped, 'зилёная', PREF) == 'Ответ.'
    long_answer = 'а' * 4050
    assert lang.render_feedback(packet(long_answer), 'зилёная', PREF) == long_answer
    assert lang.render_feedback(lang.MARKER + '{}', 'зилёная', PREF) == ''
    assert lang.render_feedback(packet(language='und'), 'зилёная', PREF) == 'Ответ на вопрос.'


def test_invalid_xml_only_in_metadata_cannot_destroy_valid_main_answer():
    text = 'Основной ответ.\n' + lang.MARKER + '\x00'
    parsed = OpenAICompatible.parse(json.dumps({'choices': [{'message': {'content': text}}]}))
    assert parsed == 'Основной ответ.'
    with pytest.raises(ModelError, match='invalid_response'):
        OpenAICompatible.parse(json.dumps({'choices': [{'message': {'content': lang.MARKER + '\x00'}}]}))


def test_detailed_mode_and_global_limit_never_shorten_main_answer():
    note = {**NOTE, 'rule': 'Правило. ' * 45}
    assert lang.render_feedback(packet(notes=[note]), 'зилёная', PREF) == 'Ответ на вопрос.'
    full = lang.render_feedback(packet(notes=[note]), 'зилёная', {**PREF, 'mode': 'detailed'})
    assert note['rule'] in full and full.startswith('Ответ на вопрос.')
    notes = [{**NOTE, 'source': f'ошибк{i}', 'correction': f'ошибка{i}'} for i in range(5)]
    output = lang.render_feedback(packet('А' * 3900, notes), ' '.join(n['source'] for n in notes),
                                  {**PREF, 'max_notes': 5})
    assert output.startswith('А' * 3900) and len(output) <= 4096


@pytest.mark.parametrize('body', ['«зилёная»', '"зилёная"', '“зилёная”', "'зилёная'",
    '`зилёная`', '```\nзилёная\n```', '> зилёная', '$зилёная$', '$$зилёная$$',
    'https://example.org/зилёная', 'www.example.org/зилёная', 'Привет', 'оченьзилёная',
    'Условие задачи: зилёная', 'Цитата: зилёная', 'Код: зилёная'])
def test_foreign_fragments_urls_code_math_and_hallucinated_sources_are_not_child_errors(body):
    assert lang.render_feedback(packet(), body, PREF) == 'Ответ на вопрос.'


@pytest.mark.parametrize('reported', ['ежык', '«ежык»', '"ежык"', 'ЕЖЫК'])
def test_source_representation_variants_use_the_actual_word_from_message(reported):
    note = {'source': reported, 'correction': 'ёжик',
            'rule': 'После ж пишем и: жи-ши.', 'example': 'Маленький ёжик.'}
    output = lang.render_feedback(packet(notes=[note]), 'Где ёжык?', PREF)
    assert '«ёжык» → «ёжик»' in output
    assert lang.MARKER not in output


@pytest.mark.parametrize('reported', ['I is', 'I     is'])
def test_source_representation_whitespace_is_preserved_in_visible_original(reported):
    note = {'source': reported, 'correction': 'I am',
            'rule': 'Use am with I.', 'example': 'I am here.'}
    output = lang.render_feedback(packet(notes=[note], language='en'), 'Why I  is here?', PREF)
    assert '«I  is» → «I am»' in output


@pytest.mark.parametrize('body,reported,correction', [
    ('«ёжык»', 'ежык', 'ёжик'), ('`ёжык`', 'ежык', 'ёжик'),
    ('https://example.org/ёжык', 'ежык', 'ёжик'),
    ('Ежык Ёжык', 'ЕЖЫК', 'ёжик'),
    ('там собака', 'сабака', 'собака'),
    ('Ёлкина пришла', 'елкина', 'Петрова'),
    ('не «слово из цитаты» был', 'не был', 'не была')])
def test_source_representation_matching_cannot_guess_spelling_or_change_protected_text(body, reported, correction):
    note = {**NOTE, 'source': reported, 'correction': correction}
    assert lang.render_feedback(packet(notes=[note]), body, PREF) == 'Ответ на вопрос.'


@pytest.mark.parametrize('extra', [{}, {'source': 'другое написание из ответа модели'}])
def test_word_indices_ground_original_even_when_model_cannot_copy_typo(extra):
    note = {'start': 3, 'end': 3, 'correction': 'белый',
            'rule': 'Проверочное слово: бе́ленький.', 'example': 'Белый снег.', **extra}
    output = lang.render_feedback(packet(notes=[note]), 'Как растёт билый гриб?', PREF)
    assert '«билый» → «белый»' in output
    assert 'другое написание' not in output


def test_word_index_ranges_keep_actual_spacing_and_accept_numeric_json_strings():
    note = {'start': '2', 'end': '3', 'correction': 'I am',
            'rule': 'Use am with I.', 'example': 'I am here.'}
    assert '«I   is» → «I am»' in lang.render_feedback(packet(notes=[note], language='en'), 'Why I   is here?', PREF)


@pytest.mark.parametrize('start,end', [(0, 1), (1, 99), (2, 1), (-1, 2),
    (True, 1), (1, False), (1.0, 2), ('1;DROP', 2), (1, None)])
def test_invalid_word_indices_cannot_fall_back_to_an_unchecked_source(start, end):
    note = {**NOTE, 'start': start, 'end': end}
    assert lang.render_feedback(packet(notes=[note]), 'Почему трава зилёная?', PREF) == 'Ответ на вопрос.'


@pytest.mark.parametrize('body,start,end', [('Пример «билый»', 2, 2), ('Код `билый`', 2, 2),
    ('Пример https://example.org/билый', 2, 2), ('не «чужой текст» был', 1, 5)])
def test_word_indices_cannot_review_quotes_code_urls_or_cross_protected_fragments(body, start, end):
    note = {'start': start, 'end': end, 'correction': 'белый', 'rule': 'Правило.', 'example': 'Пример.'}
    assert lang.render_feedback(packet(notes=[note]), body, PREF) == 'Ответ на вопрос.'


@pytest.mark.parametrize('source,correction', [('2 яблака', '3 яблока'), ('не пришол', 'пришёл'),
    ('Петя пришол', 'Вася пришёл'), ('I do not know', 'I know'), ('3,5 метра', '3,6 метра'),
    ('-2 градуса', '2 градуса'), ('- 2 градуса', '+2 градуса'), ('7 + 8', '7 - 8')])
def test_optional_correction_cannot_change_numbers_names_or_negation(source, correction):
    note = {**NOTE, 'source': source, 'correction': correction}
    assert lang.render_feedback(packet(notes=[note]), source, PREF) == 'Ответ на вопрос.'


@pytest.mark.parametrize('bad', ['\ud800', '\ufffe', '\x00'])
def test_escaped_invalid_xml_in_json_fields_cannot_block_answer(bad):
    assert lang.render_feedback(packet(notes=[{**NOTE, 'rule': bad}]), 'зилёная', PREF) == 'Ответ на вопрос.'


def test_note_limit_dedup_and_other_language_explanations():
    notes = [NOTE, NOTE, {'source': 'I is', 'correction': 'I am',
                         'rule': 'Use am with I.', 'example': 'I am happy.'},
             {'source': 'сабака', 'correction': 'собака',
              'rule': 'Это словарное слово: безударную о нужно запомнить.', 'example': 'Собака играет.'}]
    output = lang.render_feedback(packet(notes=notes, language='mixed'), 'зилёная, I is, сабака', PREF)
    assert output.count('→') == 2 and 'сабака' not in output
    english = lang.render_feedback(packet(notes=[notes[2]], language='en'), 'Why I is here?', PREF)
    assert 'Use am with I.' in english and 'Ga-ga-ga' in english
    french = {**notes[2], 'source': 'je suis aller', 'correction': 'je suis allé',
              'rule': 'Avec être, utilisez le participe passé.', 'example': 'Je suis allé au parc.'}
    assert 'je suis allé' in lang.render_feedback(packet(notes=[french], language='fr'), 'je suis aller', PREF)
    assert lang.render_feedback(packet(notes=[]), 'Привет!', PREF) == 'Ответ на вопрос.'


async def test_context_space_is_reserved_for_main_question(system):
    engine, model, sent, _ = system
    await engine.update_language(CHILD, 'enabled', True)
    engine.config = replace(engine.config, memory=replace(engine.config.memory, context_token_budget=2000))
    await engine.set_mode(CHILD, 'auto')
    await engine.submit(CHILD, 'Почему трава зилёная?')
    await idle(engine)
    assert len(model.calls) == 1 and estimate_tokens(model.calls[0]) <= 2000
    assert lang.MARKER not in str(model.calls[0]) and sent == [(CHILD, 'Ответ на вопрос.')]


async def test_feedback_fits_with_real_persona_and_nearby_context(system):
    from pathlib import Path
    engine, model, sent, _ = system
    engine.config.llm.persona_file.write_text((Path(__file__).resolve().parents[1] / 'config/persona.example.md').read_text())
    await engine.set_mode(CHILD, 'auto')
    for i in range(10):
        await engine.submit(CHILD, f'Пример {i}: ' + 'я' * 160)
        await idle(engine)
    await engine.update_language(CHILD, 'enabled', True)
    await engine.submit(CHILD, 'Почему трава зилёная?')
    await idle(engine)
    assert lang.MARKER in str(model.calls[-1])
    assert estimate_tokens(model.calls[-1]) <= engine.config.memory.context_token_budget
    assert any('Пример 9:' in m['content'] for m in model.calls[-1])
    assert '→' in sent[-1][1]


async def test_feedback_fits_with_full_persona_like_rules_and_long_recent_answers(system, database):
    from pathlib import Path
    from types import SimpleNamespace
    from goose.dialogue import Job
    from goose.social import LIKE_RULE
    engine, _, _, _ = system
    engine.config.llm.persona_file.write_text((Path(__file__).resolve().parents[1] / 'config/persona.example.md').read_text())
    await engine.update_language(CHILD, 'enabled', True)
    await engine.set_mode(CHILD, 'auto')
    engine.social = SimpleNamespace(can_like=lambda *args: True, stop_typing=lambda *args: None)
    turn = await database.add_user_message(CHILD, 'Первый учебный вопрос')
    await database.add_assistant_message(CHILD, turn, 'Объяснение учебной темы. ' * 20)
    turn = await database.add_user_message(CHILD, 'Объясни новый пример.')
    await database.add_assistant_message(CHILD, turn, 'Простой ответ на следующий вопрос. ' * 10)
    turn = await database.add_user_message(CHILD, 'Почему листья жёлтые?')
    row = await database.dialogue(CHILD)
    context, body, preference = await engine._context(CHILD, Job(turn, row['revision'], engine.clock()+60))
    assert preference and preference['enabled']
    assert lang.MARKER in context[0]['content'] and LIKE_RULE in context[0]['content']
    assert any('Объяснение учебной темы.' in m['content'] for m in context)
    assert any('Простой ответ на следующий вопрос.' in m['content'] for m in context)
    assert body == 'Почему листья жёлтые?' and estimate_tokens(context) <= 9000


async def test_feedback_drops_only_older_answer_when_latest_exchange_needs_room(system, database):
    from pathlib import Path
    from types import SimpleNamespace
    from goose.dialogue import Job
    engine, _, _, _ = system
    engine.config.llm.persona_file.write_text((Path(__file__).resolve().parents[1] / 'config/persona.example.md').read_text())
    await engine.update_language(CHILD, 'enabled', True)
    await engine.set_mode(CHILD, 'auto')
    engine.social = SimpleNamespace(can_like=lambda *args: True, stop_typing=lambda *args: None)
    old = await database.add_user_message(CHILD, 'Старый вопрос')
    await database.add_assistant_message(CHILD, old, 'Старое длинное объяснение. ' * 35)
    latest = await database.add_user_message(CHILD, 'Вопрос из ближайшего обмена')
    await database.add_assistant_message(CHILD, latest, 'Ответ из ближайшего обмена. ' * 10)
    current = await database.add_user_message(CHILD, 'Новый вопрос с опечаткой')
    revision = (await database.dialogue(CHILD))['revision']
    context, _, preference = await engine._context(CHILD, Job(current, revision, engine.clock()+60))
    assert preference and preference['enabled']
    assert estimate_tokens(context) <= 9000
    assert [m['content'] for m in context if m['role'] == 'user'] == ['Вопрос из ближайшего обмена', 'Новый вопрос с опечаткой']
    assert any(m['role'] == 'assistant' and m['content'].startswith('Ответ из ближайшего обмена.') for m in context)


@pytest.mark.parametrize('case,reason', [
    ('missing', 'missing_metadata'), ('broken', 'invalid_metadata'),
    ('empty', 'no_notes'), ('uncertain', 'uncertain_language'),
    ('filtered', 'filtered_notes'), ('budget', 'output_budget'), ('added', None)])
def test_feedback_diagnostics_explain_outcome_without_private_values(caplog, case, reason):
    private = 'fixture-private-rule-and-example'
    note = {**NOTE, 'rule': private, 'example': private}
    body = 'зилёная ' + private
    result = packet(notes=[note])
    if case == 'missing':
        result = 'Ответ. ' + private
    elif case == 'broken':
        result = 'Ответ.\n' + lang.MARKER + '{' + private
    elif case == 'empty':
        result = packet(notes=[])
    elif case == 'uncertain':
        result = packet(language='und')
    elif case == 'filtered':
        result = packet(notes=[{**note, 'source': 'несуществующий фрагмент'}])
    elif case == 'budget':
        result = packet('а'*4090, [note])
    with caplog.at_level('INFO', logger='goose'):
        lang.render_feedback(result, body, PREF)
    expected = 'language_feedback_added notes=1' if reason is None else 'language_feedback_skipped reason=' + reason
    assert expected in caplog.text
    for value in (private, NOTE['source'], NOTE['correction'], CHILD, lang.MARKER):
        assert value not in caplog.text


@pytest.mark.parametrize('action', ['manual', 'clear', 'profile', 'language', 'hold'])
async def test_late_feedback_after_cancel_is_not_delivered_and_other_dialogue_continues(system, action):
    engine, model, sent, _ = system
    await engine.update_language(CHILD, 'enabled', True)
    await engine.set_mode(CHILD, 'auto')
    await engine.set_mode(OTHER, 'auto')
    model.release.clear()
    await engine.submit(CHILD, 'Почему трава зилёная?')
    await asyncio.wait_for(model.entered.wait(), 1)
    if action == 'manual':
        await engine.set_mode(CHILD, 'manual')
    elif action == 'clear':
        await engine.clear(CHILD)
    elif action == 'profile':
        await engine.update_profile(CHILD, 'grade', 3)
    elif action == 'language':
        await engine.update_language(CHILD, 'enabled', False)
    else:
        engine.hold(CHILD)
    model.release.set()
    await engine.submit(OTHER, 'Независимый вопрос')
    await idle(engine)
    assert sent == [(OTHER, 'Ответ на вопрос.')]


async def test_manual_archive_duplicates_and_commands_do_not_start_language_review(system):
    engine, model, sent, commands = system
    await engine.update_language(CHILD, 'enabled', True)
    await engine.submit(CHILD, 'Сохрани без генерации')
    assert not model.calls
    await engine.restore(CHILD, ('archive',), 'incoming', 'Архивная зилёная', 1)
    await commands.handle(CHILD, '!ping')
    assert not model.calls
    await engine.set_mode(CHILD, 'auto')
    await engine.submit(CHILD, 'Почему трава зилёная?', keys=('unique',))
    await idle(engine)
    await engine.submit(CHILD, 'Почему трава зилёная?', keys=('unique',))
    await idle(engine)
    assert len(model.calls) == len(sent) == 1


async def test_migration_four_to_five_preserves_data_and_failure_rolls_back(config, database):
    await database.update_profile(CHILD, 'grade', 3)
    await database.set_mode(CHILD, 'auto')
    await database.add_user_message(CHILD, 'Синтетическая память')
    await database.set_weather_location(CHILD, {'name': 'Fixture'})
    await OmemoStorage(database)._store('fixture', {'persistent': True})
    await database.call(lambda c: c.execute('DROP TABLE language_feedback'))
    await database.call(lambda c: c.execute('PRAGMA user_version=4'))
    before = await database.call(lambda c: list(c.execute('SELECT key,value FROM omemo')))
    reopened = Database(config.data_dir, config.jid)
    await reopened.initialize(config.users)
    assert await reopened.call(lambda c: c.execute('PRAGMA user_version').fetchone()[0]) == 5
    assert (await reopened.profile(CHILD))['grade'] == 3
    assert (await reopened.dialogue(CHILD))['mode'] == 'auto'
    assert (await reopened.history(CHILD, 100, 24, 30))[0]['content'] == 'Синтетическая память'
    assert (await reopened.weather_location(CHILD))['name'] == 'Fixture'
    assert await reopened.call(lambda c: list(c.execute('SELECT key,value FROM omemo'))) == before
    assert await reopened.language_preferences(CHILD) == lang.DEFAULTS
    await database.call(lambda c: c.execute('DROP TABLE language_feedback'))
    await database.call(lambda c: c.execute('PRAGMA user_version=4'))
    await database.call(lambda c: c.execute("CREATE TRIGGER fail_seed BEFORE INSERT ON profiles BEGIN SELECT RAISE(ABORT,'fixture'); END"))
    with pytest.raises(sqlite3.IntegrityError):
        await reopened.initialize(config.users)
    assert await reopened.call(lambda c: c.execute('PRAGMA user_version').fetchone()[0]) == 4
    assert await reopened.call(lambda c: c.execute("SELECT 1 FROM sqlite_master WHERE name='language_feedback'").fetchone()) is None
