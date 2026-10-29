"""Настоящие XML-расширения; шифрование и сеть подставлены, гонки реальны."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import time
import xml.etree.ElementTree as ET

import pytest
import pytest_asyncio
from slixmpp import Message

from goose.config import ConfigError, InteractionConfig, load_config
from goose.dialogue import Dialogues
from goose.events import ordinary_message, owner_stanza
from goose.model import ModelError
from goose.social import (CHATSTATES, LIKE, LIKE_INTERVAL, LIKE_RULE, REACTIONS, MAX_RESOURCES,
                          NEGOTIATION_TTL, Social, answer_metadata, reaction_id)
from goose.transport import GooseClient

from test_takeover import CHILD, OTHER, OWN, drain, event as base_event, idle, running, sent_carbon
from test_archive import archive_server


def event(*args, **kwargs):
    packet = base_event(*args, **kwargs)
    ET.SubElement(packet.xml, f'{{{CHATSTATES}}}active')
    return packet


@pytest_asyncio.fixture
async def social(running, monkeypatch):
    client, model, plugin, sent = running
    client.config = replace(client.config, interaction=InteractionConfig(True, True))
    for name in ('xep_0085', 'xep_0444'):
        client.register_plugin(name)
    now = [1000.0]
    client.social = Social(client, clock=lambda: now[0])
    client.dialogues.social = client.social
    client.dialogues.config = client.config
    original = client.send
    def wire(stanza, **kwargs):
        ET.fromstring(str(stanza))
        return original(stanza, **kwargs)
    monkeypatch.setattr(client, 'send', wire)
    for jid in (CHILD, OTHER):
        await client.dialogues.set_mode(jid, 'auto')
        client.social.observe(jid, event(jid, OWN))
    return client, model, plugin, sent, now


def reactions(sent):
    return [m for m in sent if m.xml.find(f'{{{REACTIONS}}}reactions') is not None]


def states(sent, jid=CHILD):
    return [m['chat_state'] for m in sent if m['chat_state']
            and (str(m['to']) == jid or ('/' not in jid and m['to'].bare == jid))]


def notification(sender, recipient=OWN, name='active'):
    packet = base_event(sender, recipient, '', encrypted=False)
    ET.SubElement(packet.xml, f'{{{CHATSTATES}}}{name}')
    ET.SubElement(packet.xml, '{urn:xmpp:hints}no-store')
    return packet


def standalone_states(sent):
    return [(str(m['to']), m['chat_state']) for m in sent if m['chat_state'] and not m['body']]


async def test_typing_during_api_and_stop_after_encrypted_answer(social):
    client, model, plugin, sent, _ = social
    entered, release = asyncio.Event(), asyncio.Event()
    async def complete(context):
        entered.set()
        await release.wait()
        return LIKE + '\nОтвет по существу.'
    model.complete.side_effect = complete
    await client.process_message(event(CHILD + '/phone', OWN, 'Почему получается так?', ident='wire-id'))
    await asyncio.wait_for(entered.wait(), 1)
    assert states(sent) == ['composing']
    assert standalone_states(sent) == [(CHILD + '/phone', 'composing')]
    assert not reactions(sent)
    release.set()
    await idle(client)
    assert states(sent)[-1] == 'active'
    assert standalone_states(sent)[-1] == (CHILD + '/phone', 'active')
    assert reactions(sent)[0]['reactions']['id'] == 'wire-id'
    assert reactions(sent)[0]['reactions']['values'] == {'👍'}
    assert plugin.encrypt_message.await_args.args[0]['body'] == 'Ответ по существу.'
    assert (await client.database.history(CHILD, 100, 24, 30))[-1]['content'] == 'Ответ по существу.'
    assert len(model.complete.await_args.args[0]) >= 3
    assert LIKE_RULE in model.complete.await_args.args[0][0]['content']
    for msg in sent:
        if msg.xml.find(f'{{{REACTIONS}}}reactions') is not None or msg['chat_state'] == 'composing':
            assert not msg['body']


@pytest.mark.parametrize('action', ['hold', 'manual', 'clear', 'profile', 'archive'])
async def test_cancel_stops_typing_before_api_returns_and_never_likes(social, action):
    client, model, _, sent, _ = social
    entered, release = asyncio.Event(), asyncio.Event()
    async def complete(context):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return LIKE + '\nЗапоздалый ответ.'
    model.complete.side_effect = complete
    await client.process_message(event(CHILD, OWN, 'Вопрос', ident='pending'))
    await asyncio.wait_for(entered.wait(), 1)
    if action == 'hold':
        client.hold(CHILD)
    elif action == 'manual':
        await client.dialogues.set_mode(CHILD, 'manual')
    elif action == 'clear':
        await client.dialogues.clear(CHILD)
    elif action == 'profile':
        await client.dialogues.update_profile(CHILD, 'grade', 5)
    else:
        await client.dialogues.archive_failed(CHILD)
    assert states(sent) == ['composing', 'active']
    release.set()
    await idle(client)
    assert not reactions(sent)
    assert not any(m['eme']['namespace'] for m in sent)
    if action == 'hold':
        client.release(CHILD)


async def test_like_is_optional_limited_per_peer_and_uses_origin_id(social):
    client, model, _, sent, now = social
    model.complete.return_value = LIKE + '\nПонятный ответ.'
    async def ask(jid, number):
        packet = event(jid + '/phone', OWN, 'Интересный вопрос', ident=f'wire-{number}')
        ET.SubElement(packet.xml, '{urn:xmpp:sid:0}origin-id', {'id': f'origin-{number}'})
        await client.process_message(packet)
        await idle(client)
    await ask(CHILD, 1)
    await ask(CHILD, 2)
    assert len(reactions(sent)) == 1
    now[0] += LIKE_INTERVAL
    await ask(CHILD, 3)
    assert len(reactions(sent)) == 1
    await ask(OTHER, 4)
    assert len(reactions(sent)) == 2
    for number in (5, 6, 7):
        await ask(CHILD, number)
    assert len(reactions(sent)) == 3
    assert reactions(sent)[0]['reactions']['id'] == 'origin-1'
    assert str(reactions(sent)[1]['to']) == OTHER
    # Ключевые лимиты не требуют дополнительных обращений к модели.
    assert model.complete.await_count == 7


@pytest.mark.parametrize('failure', ['model', 'empty-marker', 'encryption'])
async def test_failed_answer_never_likes_and_finishes_typing(social, failure):
    client, model, plugin, sent, _ = social
    if failure == 'model':
        model.complete.side_effect = ModelError('provider')
    elif failure == 'empty-marker':
        model.complete.return_value = LIKE
    else:
        model.complete.return_value = LIKE + '\nОтвет'
        plugin.encrypt_message.return_value = (None, frozenset({'failed'}))
        plugin.encrypt_message.side_effect = None
    await client.process_message(event(CHILD, OWN, 'Вопрос', ident='failed'))
    await idle(client)
    assert states(sent)[0] == 'composing' and states(sent)[-1] == 'active'
    assert not reactions(sent)


async def test_commands_duplicates_manual_and_unknown_peer_have_no_social_response(social):
    client, model, _, sent, _ = social
    await client.process_message(event(CHILD, OWN, '!ping', ident='command'))
    assert not reactions(sent) and 'composing' not in states(sent)
    sent.clear()
    model.complete.return_value = LIKE + '\nОтвет'
    packet = event(CHILD, OWN, 'Почему?', ident='unique')
    await client.process_message(packet)
    await idle(client)
    sent.clear()
    await client.process_message(packet)
    await idle(client)
    assert not sent
    await client.dialogues.set_mode(CHILD, 'manual')
    await client.process_message(event(CHILD, OWN, 'Пауза', ident='manual'))
    await client.process_message(event('stranger@example.org', OWN, 'Почему?', ident='stranger'))
    assert not sent and model.complete.await_count == 1


@pytest.mark.parametrize('kind', ['typing', 'reaction', 'reaction-fallback'])
async def test_metadata_direct_carbon_and_archive_never_pause_or_execute(social, kind):
    client, model, _, sent, _ = social
    packet = Message()
    packet['from'], packet['to'], packet['type'] = OWN + '/phone', CHILD, 'chat'
    if kind == 'typing':
        packet['chat_state'] = 'composing'
    else:
        client.plugin['xep_0444'].set_reactions(packet, 'example', {'👍'})
        if kind == 'reaction-fallback':
            packet['body'] = '!admin mode pupil1@example.org manual'
    assert not ordinary_message(packet, delayed=True)
    assert owner_stanza(packet, OWN, client.config.resource, client.config.allowed) is None
    for msg in (packet, sent_carbon(packet)):
        client.admit(msg)
        await drain(client)
    # Входящая реакция с fallback-body также не запускает команду.
    incoming = deepcopy(packet)
    incoming['from'], incoming['to'] = CHILD, OWN
    client.admit(incoming)
    await drain(client)
    assert await client.dialogues.mode(CHILD) == 'auto'
    assert not client.holds and not sent and not model.complete.called


async def test_old_typing_finalizer_cannot_clear_new_generation(social):
    client, _, _, sent, _ = social
    old = client.social.start_typing(CHILD)
    client.social.stop_typing(CHILD)
    new = client.social.start_typing(CHILD)
    before = len(sent)
    client.social.stop_typing(CHILD, old)
    assert len(sent) == before and client.social.states[CHILD].token is new
    client.social.stop_typing(CHILD, new)
    assert states(sent)[-1] == 'active'


async def test_hold_normalizes_full_jid_and_stops_bare_dialogue(social):
    client, _, _, sent, _ = social
    client.social.start_typing(CHILD)
    client.dialogues.hold(CHILD + '/another-device')
    assert states(sent) == ['composing', 'active']
    assert client.dialogues.states[CHILD].holds == 1
    client.dialogues.release(CHILD + '/another-device')
    assert client.dialogues.states[CHILD].holds == 0


async def test_takeover_while_encryption_waits_blocks_answer_and_reaction(social):
    client, model, plugin, sent, _ = social
    entered, release = asyncio.Event(), asyncio.Event()
    original = plugin.encrypt_message.side_effect
    async def encrypt(*args):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return await original(*args)
    plugin.encrypt_message.side_effect = encrypt
    model.complete.return_value = LIKE + '\nОтвет, который не должен уйти.'
    await client.process_message(event(CHILD, OWN, 'Почему?', ident='encrypting'))
    await asyncio.wait_for(entered.wait(), 1)
    client.admit(sent_carbon(event(OWN + '/phone', CHILD, 'Отвечу сам', ident='manual-wins')))
    assert states(sent) == ['composing', 'active']
    await drain(client)
    release.set()
    await idle(client)
    assert not reactions(sent)
    assert not any(m['eme']['namespace'] for m in sent)
    assert await client.dialogues.mode(CHILD) == 'manual'


@pytest.mark.parametrize('carbon_first', [False, True])
async def test_direct_and_received_carbon_create_one_like(social, carbon_message, carbon_first):
    client, model, _, sent, _ = social
    model.complete.return_value = LIKE + '\nОтвет'
    direct = event(CHILD + '/phone', OWN, 'Почему?', ident='same')
    carbon = carbon_message(direct)
    for msg in ((carbon, direct) if carbon_first else (direct, carbon)):
        client.admit(msg)
        await drain(client)
    await idle(client)
    assert model.complete.await_count == 1
    assert len(reactions(sent)) == 1
    assert states(sent).count('composing') == 1


async def test_archived_reactions_do_not_pause_or_enter_history(social, archive_server):
    client, _, model, _, sent = archive_server
    await client.archive.synchronize(startup=True)
    await client.dialogues.set_mode(CHILD, 'auto')
    packet = Message()
    packet['from'], packet['to'], packet['type'], packet['id'] = OWN + '/phone', CHILD, 'chat', 'old-like'
    packet['body'] = '!ping'
    client.plugin['xep_0444'].set_reactions(packet, 'old-question', {'👍'})
    archive_server[1].records[CHILD].append(('reaction', packet, time.time()))
    await client.archive.synchronize()
    assert await client.dialogues.mode(CHILD) == 'auto'
    assert (await client.database.archive_cursor(CHILD))['anchor'] == 'reaction'
    assert not await client.database.history(CHILD, 100, 24, 30)
    assert not model.complete.called and not sent


async def test_untrusted_question_never_starts_typing_or_likes(social):
    client, model, plugin, sent, _ = social
    original = plugin.decrypt_message.side_effect
    async def untrusted(stanza):
        body, device = await original(stanza)
        device.trust_level_name = 'UNDECIDED'
        return body, device
    plugin.decrypt_message.side_effect = untrusted
    await client.process_message(event(CHILD, OWN, 'Почему?', ident='untrusted'))
    assert not sent and not model.complete.called


async def test_typing_is_not_repeated_and_stops_after_disconnect(social):
    client, _, _, sent, _ = social
    client.social.start_typing(CHILD)
    await asyncio.sleep(0.03)
    assert states(sent) == ['composing']
    assert all(len(list(m.xml)) == 1 for m in sent)
    client.ready.clear()
    await client.social.close()
    count = len(sent)
    await asyncio.sleep(0.02)
    assert len(sent) == count and all(s.token is None for s in client.social.states.values())


@pytest.mark.parametrize('scope', ['dialogues', 'client'])
async def test_shutdown_clears_typing_before_slow_cancellation(social, scope):
    client, model, _, sent, _ = social
    entered, release = asyncio.Event(), asyncio.Event()
    async def complete(context):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return LIKE + '\nПоздний ответ'
    model.complete.side_effect = complete
    await client.process_message(event(CHILD, OWN, 'Почему?', ident='shutdown'))
    await asyncio.wait_for(entered.wait(), 1)
    closing = asyncio.create_task(client.close() if scope == 'client' else client.dialogues.close())
    await asyncio.sleep(0)
    assert states(sent)[-1] == 'active'
    assert not reactions(sent)
    release.set()
    await asyncio.wait_for(closing, 1)


@pytest.mark.parametrize('ident', ['', 'x' * 1025, 'bad\nvalue', None])
async def test_no_usable_message_id_means_no_reaction(social, ident):
    client, model, _, sent, _ = social
    model.complete.return_value = LIKE + '\nОтвет'
    await client.dialogues.submit(CHILD, 'Вопрос', message_id=ident)
    await idle(client)
    assert not reactions(sent)
    assert LIKE_RULE not in model.complete.await_args.args[0][0]['content']


def test_duplicate_origin_id_is_not_a_reaction_target():
    packet = event(CHILD, OWN)
    for ident in ('one', 'two'):
        ET.SubElement(packet.xml, '{urn:xmpp:sid:0}origin-id', {'id': ident})
    assert reaction_id(packet) is None


def test_internal_marker_never_leaks_and_must_be_first_line_to_request_like():
    assert answer_metadata('  ' + LIKE + '\nОтвет. ') == ('Ответ.', True)
    assert answer_metadata('Ответ ' + LIKE + ' здесь.') == ('Ответ  здесь.', False)
    with pytest.raises(ModelError):
        answer_metadata(LIKE)


@pytest.mark.parametrize('field', ['typing_enabled', 'reactions_enabled', 'unknown'])
def test_interaction_configuration_is_strict(config_file, field):
    body = config_file.read_text()
    if field == 'unknown':
        body = body.replace('[interaction]', '[interaction]\nunknown = true')
    else:
        body = body.replace(field + ' = false', field + ' = "true"')
    config_file.write_text(body)
    with pytest.raises(ConfigError):
        load_config(config_file)


def test_old_configuration_without_interaction_stays_disabled(config_file):
    config_file.write_text(config_file.read_text().replace(
        '[interaction]\ntyping_enabled = false\nreactions_enabled = false\n', ''))
    assert load_config(config_file).interaction == InteractionConfig(False, False)


@pytest.mark.parametrize('typing,likes', [(False, False), (False, True), (True, False)])
async def test_switches_are_independent_and_marker_is_always_hidden(social, typing, likes):
    client, model, plugin, sent, _ = social
    client.config = replace(client.config, interaction=InteractionConfig(typing, likes))
    client.social = Social(client)
    client.dialogues.social = client.social
    model.complete.return_value = LIKE + '\nОбычный ответ.'
    await client.process_message(event(CHILD, OWN, 'Почему?', ident='switches'))
    await idle(client)
    assert bool(states(sent)) == typing
    assert bool(reactions(sent)) == likes
    assert plugin.encrypt_message.await_args.args[0]['body'] == 'Обычный ответ.'
    assert (LIKE_RULE in model.complete.await_args.args[0][0]['content']) == likes


async def test_peer_can_disable_and_enable_chat_states_without_losing_answers(social):
    client, model, plugin, sent, _ = social
    await client.process_message(base_event(CHILD, OWN, 'Без статусов', ident='no-states'))
    await idle(client)
    assert not states(sent) and not client.social.states[CHILD].chat_supported
    assert plugin.encrypt_message.await_count == 1
    sent.clear()
    await client.process_message(event(CHILD, OWN, 'Со статусами', ident='has-states'))
    await idle(client)
    assert states(sent)[0] == 'composing' and states(sent)[-1] == 'active'
    assert model.complete.await_count == 2


@pytest.mark.parametrize('carbon', [False, True])
async def test_standalone_negotiation_then_omemo_without_state_reaches_phone(social, carbon_message, carbon):
    client, model, _, sent, _ = social
    packet = notification(CHILD + '/phone')
    client.admit(carbon_message(packet) if carbon else packet)
    assert not client.queue.qsize() and not client.holds and not sent
    model.complete.assert_not_called()
    assert await client.database.history(CHILD, 100, 24, 30) == []
    await client.process_message(base_event(CHILD + '/phone', OWN, 'Длинный вопрос', ident='phone-no-active'))
    await idle(client)
    assert standalone_states(sent) == [(CHILD + '/phone', 'composing'), (CHILD + '/phone', 'active')]
    assert model.complete.await_count == 1 and await client.dialogues.mode(CHILD) == 'auto'


@pytest.mark.parametrize('case', ['foreign', 'wrong-recipient', 'delayed', 'archived', 'nested', 'unknown',
                                 'multiple', 'encrypted', 'body', 'bad-carbon', 'sent-carbon'])
async def test_invalid_metadata_never_negotiates_or_starts_commands(social, carbon_message, case):
    client, model, _, sent, _ = social
    packet = notification(CHILD + '/phone')
    if case == 'foreign':
        packet['from'] = 'stranger@example.org/phone'
    elif case == 'wrong-recipient':
        packet['to'] = 'other-bot@example.org'
    elif case == 'delayed':
        ET.SubElement(packet.xml, '{urn:xmpp:delay}delay')
    elif case == 'archived':
        ET.SubElement(packet.xml, '{urn:xmpp:mam:2}result')
    elif case == 'nested':
        ET.SubElement(packet.xml, '{urn:xmpp:forward:0}forwarded')
    elif case == 'unknown':
        packet.xml.find(f'{{{CHATSTATES}}}active').tag = f'{{{CHATSTATES}}}unexpected'
    elif case == 'multiple':
        ET.SubElement(packet.xml, f'{{{CHATSTATES}}}composing')
    elif case == 'encrypted':
        ET.SubElement(packet.xml, '{eu.siacs.conversations.axolotl}encrypted')
    elif case == 'body':
        packet['body'] = '!admin mode pupil1@example.org auto'
    elif case == 'bad-carbon':
        packet = carbon_message(packet, sender='stranger@example.org')
    else:
        packet = carbon_message(packet, direction='sent')
    client.admit(packet)
    await drain(client)
    assert not client.social.states[CHILD].notifications
    assert not client.holds and not sent and not model.complete.called
    assert await client.database.history(CHILD, 100, 24, 30) == []


@pytest.mark.parametrize('reason', ['different-resource', 'expired'])
async def test_negotiation_is_scoped_to_resource_and_expires(social, reason):
    client, model, _, sent, now = social
    client.admit(notification(CHILD + '/phone'))
    source = CHILD + '/desktop'
    if reason == 'expired':
        source = CHILD + '/phone'
        now[0] += NEGOTIATION_TTL + 1
    await client.process_message(base_event(source, OWN, 'Вопрос', ident='no-consent'))
    await idle(client)
    assert not standalone_states(sent) and model.complete.await_count == 1


async def test_metadata_cache_is_bounded_and_is_not_authorization(social):
    client, model, plugin, sent, _ = social
    for i in range(MAX_RESOURCES * 3):
        client.admit(notification(CHILD + f'/device-{i}'))
    assert len(client.social.states[CHILD].notifications) == MAX_RESOURCES
    original = plugin.decrypt_message.side_effect
    async def untrusted(stanza):
        text, device = await original(stanza)
        device.trust_level_name = 'UNDECIDED'
        return text, device
    plugin.decrypt_message.side_effect = untrusted
    await client.process_message(base_event(CHILD + f'/device-{MAX_RESOURCES * 3 - 1}', OWN, 'Вопрос'))
    assert not model.complete.called and not sent


async def test_queued_phone_and_desktop_keep_their_own_typing_targets(social):
    client, model, _, sent, _ = social
    for _ in range(client.config.llm.max_concurrent_requests):
        await client.dialogues.slots.acquire()
    try:
        await client.process_message(event(CHILD + '/phone', OWN, 'Первый вопрос', ident='phone-first'))
        await client.process_message(event(CHILD + '/desktop', OWN, 'Второй вопрос', ident='desktop-second'))
    finally:
        for _ in range(client.config.llm.max_concurrent_requests):
            client.dialogues.slots.release()
    await idle(client)
    assert standalone_states(sent) == [
        (CHILD + '/phone', 'composing'), (CHILD + '/phone', 'active'),
        (CHILD + '/desktop', 'composing'), (CHILD + '/desktop', 'active')]
    assert model.complete.await_count == 2
    assert all(str(m['to']) == CHILD for m in sent if m['body'])


async def test_active_ends_on_original_device_after_other_device_disables_status(social):
    client, _, _, sent, _ = social
    client.social.observe(CHILD, event(CHILD + '/phone', OWN))
    token = client.social.start_typing(CHILD)
    client.social.observe(CHILD, base_event(CHILD + '/desktop', OWN))
    client.social.stop_typing(CHILD, token)
    assert standalone_states(sent) == [(CHILD + '/phone', 'composing'), (CHILD + '/phone', 'active')]


async def test_chatstate_diagnostics_never_log_endpoint_or_question(social, caplog):
    client, _, _, _, _ = social
    with caplog.at_level('INFO', logger='goose'):
        await client.process_message(event(CHILD + '/private-resource', OWN, 'private-question', ident='private-id'))
        await idle(client)
    messages = '\n'.join(r.getMessage() for r in caplog.records if r.name == 'goose')
    assert 'chatstate_negotiated supported=True resource=True' in messages
    assert 'chatstate_sent state=composing' in messages and 'chatstate_sent state=active' in messages
    assert all(value not in messages for value in (CHILD, 'private-resource', 'private-question', 'private-id'))


@pytest.mark.parametrize('mode', ['probe', 'maintenance'])
async def test_diagnostic_clients_do_not_advertise_chat_features(config, mode):
    config = replace(config, interaction=InteractionConfig(True, True))
    client = GooseClient(config, 'synthetic', **{mode: True})
    try:
        assert 'xep_0085' not in set(client.plugin)
        assert 'xep_0444' not in set(client.plugin)
    finally:
        # Управляющий клиент не подключался и не создавал OMEMO-менеджер.
        if not client.probe:
            from unittest.mock import AsyncMock
            client.plugin['xep_0384'].aclose = AsyncMock()
        await client.close()


async def test_expanded_persona_keeps_story_and_followup_in_recommended_context(social):
    client, model, _, _, _ = social
    template = Path(__file__).resolve().parents[1] / 'config/persona.example.md'
    client.dialogues.config = replace(client.config, llm=replace(client.config.llm, persona_file=template))
    model.complete.return_value = 'Подробная воображаемая история и её правильный механизм. ' * 15
    await client.dialogues.submit(CHILD, 'Как ты придумал эту вещь?', message_id='first')
    await idle(client)
    model.complete.return_value = 'Фактическое пояснение авторства.'
    await client.dialogues.submit(CHILD, 'Точно ты?', message_id='second')
    await idle(client)
    history = model.complete.await_args.args[0]
    assert any(m['role'] == 'assistant' and 'воображаемая история' in m['content'] for m in history)
    assert model.complete.await_count == 2
