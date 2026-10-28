"""Ручной перехват: подставлен только криптотранспорт, гонки выполняются реально."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import xml.etree.ElementTree as ET

import pytest
import pytest_asyncio
from slixmpp import Message

from goose.dialogue import Dialogues
from goose.events import message_keys, owner_stanza
from goose.omemo import LEGACY_NS
from goose.transport import CARBONS, FORWARD, GooseClient

OWN = 'goose-test@example.org'
CHILD = 'pupil1@example.org'
OTHER = 'pupil2@example.org'


def event(sender, recipient, body='Пример', *, ident='event-1', encrypted=True, device=22):
    stanza = Message()
    stanza['from'], stanza['to'], stanza['type'], stanza['id'], stanza['body'] = sender, recipient, 'chat', ident, body
    if encrypted:
        cipher = ET.SubElement(stanza.xml, f'{{{LEGACY_NS}}}encrypted')
        ET.SubElement(cipher, f'{{{LEGACY_NS}}}header', {'sid': str(device)})
        ET.SubElement(cipher, f'{{{LEGACY_NS}}}payload').text = ident
    return stanza


def sent_carbon(inner):
    outer = Message()
    outer['from'], outer['to'], outer['type'] = OWN, OWN + '/goose-bot', 'chat'
    sent = ET.SubElement(outer.xml, f'{{{CARBONS}}}sent')
    ET.SubElement(sent, f'{{{FORWARD}}}forwarded').append(deepcopy(inner.xml))
    return outer


@pytest_asyncio.fixture
async def running(config, database, tmp_path, monkeypatch):
    persona = tmp_path / 'persona.md'
    persona.write_text('Учебный Гусь.')
    config = replace(config, llm=replace(config.llm, enabled=True, persona_file=persona))
    client = GooseClient(config, 'fixture', database)
    client.init_plugins()
    client.ready.set()
    client.own_device_id = 11
    async def decrypt(stanza):
        sender = str(stanza['from']).split('/')[0]
        return deepcopy(stanza), SimpleNamespace(bare_jid=sender, trust_level_name='TRUSTED', device_id=22)
    async def encrypt(stanza, _peers):
        outgoing = event(str(stanza['from']), str(stanza['to']), 'ciphertext', ident=stanza['id'], device=11)
        return Message(xml=outgoing.xml, stream=client), frozenset()
    plugin = SimpleNamespace(
        is_encrypted=lambda m: {LEGACY_NS} if m.xml.find(f'{{{LEGACY_NS}}}encrypted') is not None else set(),
        decrypt_message=AsyncMock(side_effect=decrypt), encrypt_message=AsyncMock(side_effect=encrypt), aclose=AsyncMock())
    monkeypatch.setitem(client.plugin._plugins, 'xep_0384', plugin)
    sent = []
    monkeypatch.setattr(client, 'send', lambda stanza, **kwargs: sent.append(stanza))
    model = SimpleNamespace(complete=AsyncMock(return_value='Ответ модели'), close=AsyncMock())
    client.dialogues = Dialogues(config, database, model, client.prepare_reply)
    client.commands.dialogues = client.dialogues
    await client.dialogues.initialize()
    try:
        yield client, model, plugin, sent
    finally:
        await client.close()


async def drain(client):
    while not client.queue.empty():
        await client.process_message(client.queue.get_nowait())


async def idle(client):
    await asyncio.wait_for(asyncio.gather(*tuple(client.dialogues.tasks), return_exceptions=True), 2)


@pytest.mark.parametrize('carbon_first', [True, False])
async def test_manual_sent_direct_and_carbon_pause_once_and_only_one_dialogue(running, database, carbon_first):
    client, model, _, sent = running
    for jid in (CHILD, OTHER):
        await client.dialogues.set_mode(jid, 'auto')
    manual = event(OWN + '/phone', CHILD, 'Ручная подсказка')
    deliveries = (sent_carbon(manual), manual) if carbon_first else (manual, sent_carbon(manual))
    for stanza in deliveries:
        client.admit(stanza)
        await drain(client)
    assert await client.dialogues.mode(CHILD) == 'manual'
    assert await client.dialogues.mode(OTHER) == 'auto'
    assert await database.history(CHILD, 100, 24, 30) == [{'role': 'assistant', 'content': 'Ручная подсказка'}]
    assert not sent and not model.complete.called
    client.admit(event(OTHER + '/phone', OWN, 'Независимый вопрос', ident='other'))
    await drain(client)
    await idle(client)
    model.complete.assert_awaited_once()
    assert len(sent) == 1 and str(sent[0]['to']) == OTHER


@pytest.mark.parametrize('phase', ['model', 'encryption'])
async def test_manual_admission_blocks_late_answer_even_before_decrypt(running, phase):
    client, model, plugin, sent = running
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(*args):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return 'Запоздалый ответ' if phase == 'model' else (event(OWN + '/goose-bot', CHILD, device=11), frozenset())
    if phase == 'model':
        model.complete.side_effect = blocked
    else:
        plugin.encrypt_message.side_effect = blocked
    await client.dialogues.set_mode(CHILD, 'auto')
    await client.dialogues.submit(CHILD, 'Вопрос')
    await asyncio.wait_for(entered.wait(), 1)
    manual = event(OWN + '/desktop', CHILD, 'Отвечу сам', ident='manual')
    client.admit(sent_carbon(manual))  # Очередь намеренно ещё не обрабатывается.
    assert client.holds[CHILD] == 1
    release.set()
    await idle(client)
    assert not sent
    await drain(client)
    assert await client.dialogues.mode(CHILD) == 'manual'
    assert not client.holds


@pytest.mark.parametrize('failure', ['untrusted', 'decrypt', 'unsupported', 'plaintext'])
async def test_unreadable_own_send_pauses_without_importing_unverified_text(running, database, failure):
    client, _, plugin, sent = running
    await client.dialogues.set_mode(CHILD, 'auto')
    manual = event(OWN + '/phone', CHILD, 'Не добавлять в контекст', encrypted=failure not in {'plaintext', 'unsupported'})
    if failure == 'unsupported':
        ET.SubElement(manual.xml, '{urn:xmpp:omemo:2}encrypted')
    if failure == 'decrypt':
        plugin.decrypt_message.side_effect = ValueError('private')
    elif failure == 'untrusted':
        plugin.decrypt_message.side_effect = None
        plugin.decrypt_message.return_value = (manual, SimpleNamespace(
            bare_jid=OWN, trust_level_name='UNDECIDED', device_id=22))
    client.admit(sent_carbon(manual))
    await drain(client)
    assert await client.dialogues.mode(CHILD) == 'manual'
    assert await database.history(CHILD, 100, 24, 30) == []
    assert not sent


async def test_service_reply_is_recorded_before_send_and_not_manual_after_restart(running, database):
    client, _, plugin, sent = running
    await client.dialogues.set_mode(CHILD, 'auto')
    send = await client.prepare_reply(CHILD, 'Ответ сервиса')
    assert not sent
    send()
    own = sent[0]
    assert await database.event_kind(message_keys(own, OWN)) == 'bot'
    # Resource мог измениться при обновлении: постоянные ID всё равно узнаются.
    reflected = Message(xml=deepcopy(own.xml))
    reflected['from'] = OWN + '/previous-service-resource'
    await client.restore_message(reflected, time.time())
    assert await client.dialogues.mode(CHILD) == 'auto'
    plugin.decrypt_message.assert_not_awaited()


async def test_manual_text_is_context_for_followup_but_never_a_command(running, database):
    client, model, _, _ = running
    await client.dialogues.set_mode(CHILD, 'auto')
    await client.dialogues.submit(CHILD, 'Объясни пример')
    await idle(client)
    await client.process_message(event(OWN + '/phone', CHILD, 'Подсказка владельца', ident='hint'))
    await client.process_message(event(OWN + '/phone', CHILD, '!context clear', ident='not-a-command'))
    assert any(m['content'] == 'Подсказка владельца' for m in await database.history(CHILD, 100, 24, 30))
    await client.dialogues.set_mode(CHILD, 'auto')
    await client.dialogues.submit(CHILD, 'А дальше?')
    await idle(client)
    assert any(m['content'] == 'Подсказка владельца' for m in model.complete.call_args.args[0])


@pytest.mark.parametrize('tamper', ['outer-from', 'outer-to', 'inner-from', 'inner-to', 'mixed',
                                   'duplicate', 'forwarded', 'extra-forwarded', 'nested-carbon',
                                   'delay', 'archive', 'receipt', 'service', 'self'])
async def test_forged_sent_envelopes_never_pause(running, tamper):
    client, _, plugin, _ = running
    await client.dialogues.set_mode(CHILD, 'auto')
    inner = event(OWN + '/phone', CHILD)
    outer = sent_carbon(inner)
    forwarded = outer.xml.find(f'{{{CARBONS}}}sent/{{{FORWARD}}}forwarded')
    raw = forwarded[0]
    if tamper == 'outer-from':
        outer['from'] = CHILD
    elif tamper == 'outer-to':
        outer['to'] = CHILD
    elif tamper == 'inner-from':
        raw.set('from', CHILD)
    elif tamper == 'inner-to':
        raw.set('to', 'unknown@example.org')
    elif tamper == 'mixed':
        ET.SubElement(outer.xml, f'{{{CARBONS}}}received')
    elif tamper == 'duplicate':
        forwarded.append(deepcopy(raw))
    elif tamper == 'forwarded':
        ET.SubElement(raw, f'{{{FORWARD}}}forwarded')
    elif tamper == 'extra-forwarded':
        ET.SubElement(outer.xml, f'{{{FORWARD}}}forwarded')
    elif tamper == 'nested-carbon':
        ET.SubElement(ET.SubElement(outer.xml, 'x'), f'{{{CARBONS}}}sent')
    elif tamper == 'delay':
        ET.SubElement(forwarded, '{urn:xmpp:delay}delay')
    elif tamper == 'archive':
        ET.SubElement(raw, '{urn:xmpp:mam:2}result')
    elif tamper == 'receipt':
        ET.SubElement(raw, '{urn:xmpp:receipts}received')
    elif tamper == 'service':
        raw.set('type', 'groupchat')
    elif tamper == 'self':
        raw.set('from', OWN + '/goose-bot')
    assert owner_stanza(outer, OWN, 'goose-bot', client.config.allowed) is None
    client.admit(outer)
    await drain(client)
    assert await client.dialogues.mode(CHILD) == 'auto' and not client.holds
    plugin.decrypt_message.assert_not_awaited()


async def test_manual_queue_overflow_closes_session_without_a_send_window(running):
    client, _, _, sent = running
    await client.dialogues.set_mode(CHILD, 'auto')
    for index in range(64):
        client.admit(event(CHILD + '/phone', OWN, ident=str(index)))
    client.admit(sent_carbon(event(OWN + '/phone', CHILD, ident='overflow')))
    assert client.ended.is_set() and not client.ready.is_set()
    send = await client.prepare_reply(CHILD, 'Не отправлять')
    assert send() is False and not sent


def test_delivery_aliases_cover_ids_ciphertext_and_reject_foreign_stanza_id():
    first = event(CHILD + '/phone', OWN, ident='first')
    second = deepcopy(first)
    second['id'] = 'rewritten-by-server'
    assert set(message_keys(first, OWN)) & set(message_keys(second, OWN))
    ET.SubElement(first.xml, '{urn:xmpp:sid:0}stanza-id', {'by': 'foreign.example.org', 'id': 'bogus'})
    assert message_keys(first, OWN) == message_keys(event(CHILD + '/phone', OWN, ident='first'), OWN)


async def test_replayed_manual_after_explicit_auto_does_not_cancel_a_new_answer(running):
    client, model, _, sent = running
    manual = event(OWN + '/phone', CHILD, 'Подсказка', ident='manual-once')
    client.admit(sent_carbon(manual))
    await drain(client)
    await client.dialogues.set_mode(CHILD, 'auto')
    entered, release = asyncio.Event(), asyncio.Event()
    async def complete(messages):
        entered.set()
        await release.wait()
        return 'Свежий ответ'
    model.complete.side_effect = complete
    await client.dialogues.submit(CHILD, 'Продолжим')
    await asyncio.wait_for(entered.wait(), 1)
    client.admit(manual)
    assert not client.holds and client.queue.empty()
    release.set()
    await idle(client)
    assert await client.dialogues.mode(CHILD) == 'auto' and len(sent) == 1


async def test_first_manual_answer_is_kept_even_without_an_earlier_saved_question(running):
    client, model, _, _ = running
    await client.process_message(event(OWN + '/phone', CHILD, 'Первая ручная подсказка', ident='first-manual'))
    await client.dialogues.set_mode(CHILD, 'auto')
    await client.process_message(event(CHILD + '/phone', OWN, 'Объясни эту подсказку', ident='followup'))
    await idle(client)
    context = model.complete.call_args.args[0]
    assert context[2]['role'] == 'user'  # Chat Completions не начинается с assistant.
    assert any(m['role'] == 'assistant' and m['content'] == 'Первая ручная подсказка' for m in context)
    assert context[-1] == {'role': 'user', 'content': 'Объясни эту подсказку'}


async def test_live_manual_and_new_question_are_not_lost_after_a_clock_rollback(running, database):
    client, model, _, _ = running
    await client.dialogues.set_mode(CHILD, 'auto')
    await database.call(lambda db: db.execute(
        'UPDATE dialogues SET mode_changed_at=?, cleared_at=? WHERE jid=?',
        (time.time() + 3600, time.time() + 3600, CHILD)))
    await client.process_message(event(OWN + '/phone', CHILD, 'Живая подсказка', ident='clock-manual'))
    assert await client.dialogues.mode(CHILD) == 'manual'
    assert any(m['content'] == 'Живая подсказка' for m in await database.history(CHILD, 100, 24, 30))
    await client.dialogues.set_mode(CHILD, 'auto')
    await client.process_message(event(CHILD + '/phone', OWN, 'Новый вопрос', ident='clock-question'))
    await idle(client)
    assert model.complete.call_args.args[0][-1]['content'] == 'Новый вопрос'
