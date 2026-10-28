"""Настоящие XML-формы MAM/RSM, подставной сервер и постоянные позиции архива."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import xml.etree.ElementTree as ET

import pytest
import pytest_asyncio
from slixmpp import Iq, Message
from slixmpp.exceptions import IqError

from goose.archive import ArchiveError, ArchiveSync, MAM, MAX_PAGES, PAGE_SIZE, RSM
from goose.dialogue import Dialogues
from goose.events import DELAY, FORWARD, message_keys
from goose.storage import Database

# Общая синтетическая сессия проверяет тот же транспорт, что live-перехват.
from test_takeover import CHILD, OTHER, OWN, event, running


def archived(inner, ident, query_id, stamp):
    outer = Message()
    outer['from'], outer['to'] = OWN, OWN + '/goose-bot'
    result = ET.SubElement(outer.xml, f'{{{MAM}}}result', {'id': ident, 'queryid': query_id})
    forwarded = ET.SubElement(result, f'{{{FORWARD}}}forwarded')
    ET.SubElement(forwarded, f'{{{DELAY}}}delay', {
        'stamp': datetime.fromtimestamp(stamp, timezone.utc).isoformat()})
    forwarded.append(deepcopy(inner.xml))
    return outer


@pytest_asyncio.fixture
async def archive_server(running, monkeypatch):
    client, model, plugin, sent = running
    client.features = {'mam': True, 'carbons': True}
    client.archive = ArchiveSync(client)
    records = {jid: [] for jid in client.config.allowed}
    calls = []
    server = SimpleNamespace(records=records, calls=calls, mutate=None, finish=None)
    async def send(iq, **kwargs):
        ET.fromstring(str(iq))
        assert kwargs['timeout'] == 5
        assert str(iq['to']) == OWN
        peer = str(iq['mam']['with'])
        assert peer in client.config.allowed
        before = iq['mam']['rsm']['before']
        after = iq['mam']['rsm']['after']
        limit = int(iq['mam']['rsm']['max'])
        baseline = iq.xml.find(f'{{{MAM}}}query/{{{RSM}}}set/{{{RSM}}}before') is not None
        end, start = iq['mam']['end'].timestamp(), iq['mam']['start']
        calls.append((peer, after, baseline, limit))
        available = [r for r in records[peer] if r[2] <= end and (not start or r[2] >= start.timestamp())]
        if after:
            ids = [r[0] for r in available]
            if after not in ids:
                error = Iq()
                error['type'] = 'error'
                error['error']['condition'] = 'item-not-found'
                raise IqError(error)
            available = available[ids.index(after) + 1:]
        selected = available[-limit:] if baseline else available[:limit]
        for ident, stanza, stamp in selected:
            wrapped = archived(stanza, ident, iq['id'], stamp)
            if server.mutate:
                server.mutate(wrapped)
            client.archive.collect(wrapped)
        response = Iq()
        response['id'], response['type'], response['from'] = iq['id'], 'result', OWN
        fin = ET.SubElement(response.xml, f'{{{MAM}}}fin', {
            'complete': 'true' if len(available) <= limit else 'false'})
        if selected:
            ET.SubElement(ET.SubElement(fin, f'{{{RSM}}}set'), f'{{{RSM}}}last').text = selected[-1][0]
        if server.finish:
            server.finish(response)
        return response
    monkeypatch.setattr(Iq, 'send', send)
    try:
        yield client, server, model, plugin, sent
    finally:
        await client.archive.close()


async def baseline_and_enable(client, jids=(CHILD, OTHER)):
    await client.archive.synchronize(startup=True)
    for jid in jids:
        await client.dialogues.set_mode(jid, 'auto')


async def restart(client):
    await client.dialogues.close()
    model = SimpleNamespace(complete=AsyncMock(return_value='Новый ответ'), close=AsyncMock())
    client.dialogues = Dialogues(client.config, client.database, model, client.prepare_reply)
    client.commands.dialogues = client.dialogues
    await client.dialogues.initialize()
    client.archive = ArchiveSync(client)
    return model


@pytest.mark.parametrize('baseline', [True, False])
async def test_mam_query_serializes_through_real_iq_send(running, monkeypatch, baseline):
    client, model, _, sent = running
    client.archive = ArchiveSync(client)
    client.session_bind_event.set()
    packets = []

    def send(stanza, **kwargs):
        # Настоящий Iq.send и сериализатор Slixmpp; подставлена только сеть.
        packet = ET.fromstring(str(stanza))
        packets.append(packet)
        response = Iq(stream=client)
        response['id'], response['type'], response['from'] = packet.get('id'), 'result', OWN
        response['to'] = str(client.boundjid)
        ET.SubElement(response.xml, f'{{{MAM}}}fin', {'complete': 'true'})
        client.recv_stanza(response)

    monkeypatch.setattr(client, 'send', send)
    assert await client.archive.query(CHILD, time.time(), baseline=baseline) == ([], None, True)
    query = packets[0].find(f'{{{MAM}}}query')
    assert query.findtext(f'{{{RSM}}}set/{{{RSM}}}max') == str(1 if baseline else PAGE_SIZE)
    assert (query.find(f'{{{RSM}}}set/{{{RSM}}}before') is not None) == baseline
    assert not sent and not model.complete.called


async def test_first_baseline_never_imports_old_history_or_enables_old_auto(archive_server, database):
    client, server, model, plugin, sent = archive_server
    server.records[CHILD].append(('older', event(OWN + '/phone', CHILD, 'Давняя переписка', ident='older'), time.time() - 60))
    server.records[CHILD].append(('old', event(OWN + '/phone', CHILD, 'Старая переписка'), time.time() - 30))
    await client.dialogues.set_mode(CHILD, 'auto')
    await database.suspend_auto()  # Состояние, оставшееся от старой версии при запуске.
    await client.archive.synchronize(startup=True)
    assert 'пауза' in await client.dialogues.mode(CHILD)
    cursor = await database.archive_cursor(CHILD)
    assert cursor['anchor'] == 'old' and cursor['confirmed'] == 0
    assert await database.history(CHILD, 100, 24, 30) == []
    assert not sent and not model.complete.called
    plugin.decrypt_message.assert_not_awaited()
    await client.dialogues.set_mode(CHILD, 'auto')
    assert (await database.archive_cursor(CHILD))['confirmed'] == 1


async def test_restart_recovers_manual_only_for_its_peer_and_never_replies_to_archive(archive_server, database):
    client, server, _, _, sent = archive_server
    await baseline_and_enable(client)
    server.records[CHILD].append(('manual', event(OWN + '/phone', CHILD, 'Ручная подсказка', ident='manual'), time.time()))
    old = event(OTHER + '/phone', OWN, 'Вопрос во время отключения', ident='offline')
    server.records[OTHER].append(('offline', old, time.time()))
    server.records[OTHER].append(('command', event(OTHER + '/phone', OWN, '!context clear', ident='command'), time.time()))
    model = await restart(client)
    await client.archive.synchronize(startup=True)
    assert await client.dialogues.mode(CHILD) == 'manual'
    assert await client.dialogues.mode(OTHER) == 'auto'
    assert await database.history(CHILD, 100, 24, 30) == [{'role': 'assistant', 'content': 'Ручная подсказка'}]
    assert await database.history(OTHER, 100, 24, 30) == [{'role': 'user', 'content': 'Вопрос во время отключения'}]
    assert not model.complete.called and not sent
    # Live-копия уже восстановленного сообщения не вызывает запоздалого ответа.
    await client.process_message(old)
    assert not model.complete.called
    await client.process_message(event(OTHER + '/phone', OWN, 'Новый вопрос', ident='new'))
    await asyncio.wait_for(asyncio.gather(*tuple(client.dialogues.tasks)), 1)
    assert len(sent) == 1
    assert any(m['content'] == 'Вопрос во время отключения' for m in model.complete.call_args.args[0])


async def test_checkpoint_and_own_delivery_aliases_survive_database_reopen(archive_server, database):
    client, server, _, plugin, _ = archive_server
    server.records[CHILD].append(('anchor', event(CHILD, OWN, ident='anchor'), time.time() - 2))
    await baseline_and_enable(client)
    send = await client.prepare_reply(CHILD, 'Ответ сервиса')
    captured = plugin.encrypt_message.call_args.args[0]
    own = event(OWN + '/goose-bot', CHILD, 'ciphertext', ident=captured['id'], device=11)
    server.records[CHILD].append(('bot', own, time.time()))
    reopened = Database(database.path.parent, database.account)
    await reopened.initialize({})
    assert (await reopened.archive_cursor(CHILD))['anchor'] == 'anchor'
    assert await reopened.event_kind(message_keys(own, OWN)) == 'bot'
    client.database = reopened
    model = await restart(client)
    await client.archive.synchronize(startup=True)
    assert await client.dialogues.mode(CHILD) == 'auto'
    plugin.decrypt_message.assert_not_awaited()
    assert not model.complete.called


async def test_context_clear_and_newer_explicit_auto_are_not_undone_by_archive(archive_server, database):
    client, server, _, _, sent = archive_server
    await baseline_and_enable(client)
    stamp = time.time()
    server.records[CHILD].append(('old-user', event(CHILD, OWN, 'Удалённая память', ident='old-user'), stamp))
    server.records[CHILD].append(('old-manual', event(OWN + '/phone', CHILD, 'Удалённый ответ', ident='old-manual'), stamp))
    await client.dialogues.clear(CHILD)
    await client.dialogues.set_mode(CHILD, 'auto')
    await client.archive.synchronize()
    assert await database.history(CHILD, 100, 24, 30) == []
    assert await client.dialogues.mode(CHILD) == 'auto' and not sent


async def test_unreadable_peer_does_not_prevent_other_peer_resuming(archive_server):
    client, server, _, plugin, sent = archive_server
    await baseline_and_enable(client)
    server.records[CHILD].append(('bad', event(CHILD, OWN, ident='bad'), time.time()))
    original = plugin.decrypt_message.side_effect
    async def decrypt(stanza):
        if stanza['id'] == 'bad':
            raise ValueError('private')
        return await original(stanza)
    plugin.decrypt_message.side_effect = decrypt
    model = await restart(client)
    await client.archive.synchronize(startup=True)
    assert 'пауза' in await client.dialogues.mode(CHILD)
    assert await client.dialogues.mode(OTHER) == 'auto'
    assert client.archive.states[CHILD] == 'incomplete'
    assert not model.complete.called and not sent


async def test_deleted_anchor_requires_new_confirmation_and_can_recover_after_it(archive_server, database):
    client, server, _, _, _ = archive_server
    server.records[CHILD].append(('gone', event(CHILD, OWN, ident='gone'), time.time()))
    await baseline_and_enable(client)
    server.records[CHILD] = [('recent', event(CHILD, OWN, ident='recent'), time.time())]
    await restart(client)
    await client.archive.synchronize(startup=True)
    assert 'пауза' in await client.dialogues.mode(CHILD)
    assert (await database.archive_cursor(CHILD))['confirmed'] == 0
    await client.dialogues.set_mode(CHILD, 'auto')
    await restart(client)
    await client.archive.synchronize(startup=True)
    assert await client.dialogues.mode(CHILD) == 'auto'


@pytest.mark.parametrize('feature', ['mam', 'carbons'])
async def test_missing_capability_never_automatically_rearms(archive_server, feature):
    client, _, _, _, _ = archive_server
    await baseline_and_enable(client)
    await restart(client)
    client.features[feature] = False
    await client.archive.synchronize(startup=True)
    assert 'пауза' in await client.dialogues.mode(CHILD)


@pytest.mark.parametrize('tamper', ['missing-message', 'extra-message', 'missing-delay', 'bad-clock',
                                   'naive-clock', 'future-clock', 'wrong-peer', 'nested', 'empty-id', 'wrong-last', 'unstable'])
async def test_malformed_archive_cannot_rearm_or_execute(archive_server, tamper):
    client, server, model, _, sent = archive_server
    server.records[CHILD].append(('one', event(CHILD, OWN, '!ping'), time.time() - 5))
    def mutate(stanza):
        result = stanza.xml.find(f'{{{MAM}}}result')
        forwarded = result[0]
        delay, inner = forwarded
        if tamper == 'missing-message':
            forwarded.remove(inner)
        elif tamper == 'extra-message':
            forwarded.append(deepcopy(inner))
        elif tamper == 'missing-delay':
            forwarded.remove(delay)
        elif tamper == 'bad-clock':
            delay.set('stamp', 'broken')
        elif tamper == 'naive-clock':
            delay.set('stamp', '2026-10-27T00:00:00')
        elif tamper == 'future-clock':
            delay.set('stamp', '2099-01-01T00:00:00Z')
        elif tamper == 'wrong-peer':
            inner.set('from', OTHER)
        elif tamper == 'nested':
            ET.SubElement(inner, f'{{{MAM}}}result')
        elif tamper == 'empty-id':
            result.set('id', '')
    server.mutate = mutate
    if tamper == 'wrong-last':
        server.finish = lambda iq: setattr(iq.xml.find(f'{{{MAM}}}fin/{{{RSM}}}set/{{{RSM}}}last'), 'text', 'other')
    if tamper == 'unstable':
        server.finish = lambda iq: iq.xml.find(f'{{{MAM}}}fin').set('stable', 'false')
    with pytest.raises(ArchiveError):
        await client.archive.query(CHILD, time.time())
    assert not sent and not model.complete.called


@pytest.mark.parametrize('tamper', ['from', 'to', 'query'])
async def test_unsolicited_or_foreign_archive_result_is_not_collected(archive_server, tamper):
    client, server, _, plugin, sent = archive_server
    server.records[CHILD].append(('one', event(CHILD, OWN, '!ping'), time.time() - 1))
    def mutate(stanza):
        if tamper == 'from':
            stanza['from'] = CHILD
        elif tamper == 'to':
            stanza['to'] = OTHER
        else:
            stanza.xml.find(f'{{{MAM}}}result').set('queryid', 'other-query')
    server.mutate = mutate
    # fin с чужим last не согласуется с пустой полученной страницей.
    with pytest.raises(ArchiveError):
        await client.archive.query(CHILD, time.time())
    plugin.decrypt_message.assert_not_awaited()
    assert not sent


async def test_page_limit_stops_incomplete_history_and_keeps_auto_paused(archive_server, database):
    client, server, model, _, sent = archive_server
    await baseline_and_enable(client)
    for index in range(PAGE_SIZE * MAX_PAGES + 1):
        server.records[CHILD].append((str(index), event(CHILD, OWN, '!ping', ident=str(index)), time.time()))
    await restart(client)
    await client.archive.synchronize(startup=True)
    assert 'пауза' in await client.dialogues.mode(CHILD)
    assert (await database.archive_cursor(CHILD))['confirmed'] == 0
    queries = [call for call in server.calls if call[0] == CHILD and not call[2]]
    assert len(queries) == MAX_PAGES
    assert not sent and not model.complete.called


async def test_sync_timeout_is_bounded_and_does_not_advance_a_confirmed_cursor(archive_server, monkeypatch, database):
    client, server, _, _, sent = archive_server
    await baseline_and_enable(client)
    before = await database.archive_cursor(CHILD)
    original = Iq.send
    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(Iq, 'send', blocked)
    monkeypatch.setattr('goose.archive.SYNC_TIMEOUT', 0.02)
    await restart(client)
    await asyncio.wait_for(client.archive.synchronize(startup=True), 0.5)
    assert 'пауза' in await client.dialogues.mode(CHILD)
    after = await database.archive_cursor(CHILD)
    assert before['anchor'] == after['anchor'] and after['confirmed'] == 1
    assert 'возобновится' in await client.dialogues.mode(CHILD)
    assert not sent
    # Временный сбой закончился: следующий poll возвращает прежний auto.
    await client.dialogues.set_mode(OTHER, 'manual')
    monkeypatch.setattr(Iq, 'send', original)
    monkeypatch.setattr('goose.archive.SYNC_TIMEOUT', 20)
    await client.archive.synchronize()
    assert await client.dialogues.mode(CHILD) == 'auto'
    assert await client.dialogues.mode(OTHER) == 'manual'
    assert not sent


async def test_retry_unknown_mam_discovery_resumes_only_confirmed_auto(archive_server, monkeypatch):
    client, _, model, _, sent = archive_server
    await baseline_and_enable(client, (CHILD,))
    await restart(client)
    client.features['mam'] = 'не проверено'
    await client.archive.synchronize(startup=True)
    assert 'пауза' in await client.dialogues.mode(CHILD)
    discover = AsyncMock(return_value={'disco_info': {'features': [MAM]}})
    monkeypatch.setattr(client.plugin['xep_0030'], 'get_info', discover)
    await client.archive.synchronize()
    discover.assert_awaited_once_with(jid=OWN, local=False, cached=False, timeout=5)
    assert await client.dialogues.mode(CHILD) == 'auto'
    assert await client.dialogues.mode(OTHER) == 'manual'
    assert not sent and not model.complete.called


async def test_startup_buffers_live_messages_and_manual_hold_precedes_auto_restore(archive_server, monkeypatch):
    client, server, _, plugin, sent = archive_server
    await baseline_and_enable(client)
    await client.dialogues.close()
    client.dialogues = None
    client.ready.clear()
    entered, release = asyncio.Event(), asyncio.Event()
    original = Iq.send
    async def delayed(iq, **kwargs):
        entered.set()
        await release.wait()
        return await original(iq, **kwargs)
    monkeypatch.setattr(Iq, 'send', delayed)
    monkeypatch.setattr(client, 'get_roster', AsyncMock())
    monkeypatch.setattr(client, 'send_presence', lambda **kwargs: None)
    monkeypatch.setattr(client.plugin['xep_0030'], 'get_info',
                        AsyncMock(return_value={'disco_info': {'features': [MAM, 'urn:xmpp:carbons:2']}}))
    monkeypatch.setattr(client.plugin['xep_0280'], 'enable', AsyncMock())
    plugin.get_session_manager = AsyncMock(return_value=SimpleNamespace(
        get_own_device_information=AsyncMock(return_value=(SimpleNamespace(device_id=11), ())),
        before_history_sync=lambda: None, after_history_sync=AsyncMock()))
    monkeypatch.setattr('goose.transport.OpenAICompatible', lambda config: SimpleNamespace(
        complete=AsyncMock(return_value='Ответ'), close=AsyncMock()))
    from test_takeover import sent_carbon
    starting = asyncio.create_task(client.on_start(None))
    await asyncio.wait_for(entered.wait(), 1)
    assert not client.ready.is_set()
    client.admit(event(CHILD + '/phone', OWN, 'Новый вопрос', ident='during-start'))
    client.admit(sent_carbon(event(OWN + '/phone', CHILD, 'Отвечу сам', ident='takeover-during-start')))
    assert client.queue.qsize() == 2 and client.holds[CHILD] == 1
    release.set()
    await asyncio.wait_for(starting, 2)
    await asyncio.wait_for(client.queue.join(), 1)
    assert client.ready.is_set() and await client.dialogues.mode(CHILD) == 'manual'
    assert not sent


async def test_polling_stops_and_cleans_active_query_on_close(archive_server, monkeypatch):
    client, _, _, _, _ = archive_server
    entered = asyncio.Event()
    async def wait(iq, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(Iq, 'send', wait)
    monkeypatch.setattr('goose.archive.POLL_INTERVAL', 0)
    client.archive.start()
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(client.archive.close(), 1)
    assert client.archive.active is None and client.archive.task.done()


async def test_slow_archive_of_one_peer_leaves_time_for_other_peers(archive_server, monkeypatch):
    client, _, _, _, _ = archive_server
    await baseline_and_enable(client)
    original = Iq.send
    async def slow(iq, **kwargs):
        if str(iq['mam']['with']) == 'parent@example.org':
            await asyncio.Event().wait()
        return await original(iq, **kwargs)
    monkeypatch.setattr(Iq, 'send', slow)
    monkeypatch.setattr('goose.archive.SYNC_TIMEOUT', 0.6)
    await restart(client)
    await asyncio.wait_for(client.archive.synchronize(startup=True), 1.5)
    assert client.archive.states['parent@example.org'] == 'incomplete'
    assert await client.dialogues.mode(CHILD) == await client.dialogues.mode(OTHER) == 'auto'


@pytest.mark.parametrize('limit', ['message', 'bytes', 'count'])
async def test_collector_bounds_memory_even_if_server_ignores_requested_limit(archive_server, limit):
    from goose.archive import Page
    client, _, _, plugin, sent = archive_server
    page = Page('active-query', CHILD, time.time(), PAGE_SIZE)
    client.archive.active = page
    try:
        for index in range(60):
            body = 'x' * (70000 if limit == 'message' else 60000 if limit == 'bytes' else 10)
            packet = archived(event(CHILD, OWN, body, ident=str(index)), str(index), page.query_id, page.end - 1)
            client.archive.collect(packet)
        assert page.invalid and len(page.records) <= PAGE_SIZE
        assert sum(len(ET.tostring(r.stanza.xml)) for r in page.records) <= 2 * 1024 * 1024
        plugin.decrypt_message.assert_not_awaited()
        assert not sent
    finally:
        client.archive.active = None


async def test_periodic_manual_restore_blocks_answer_while_decryption_is_pending(archive_server):
    client, server, model, plugin, sent = archive_server
    await baseline_and_enable(client)
    model_started, release_model = asyncio.Event(), asyncio.Event()
    decrypt_started, release_decrypt = asyncio.Event(), asyncio.Event()
    original = plugin.decrypt_message.side_effect
    async def complete(messages):
        model_started.set()
        try:
            await release_model.wait()
        except asyncio.CancelledError:
            await release_model.wait()
        return 'Поздний ответ'
    async def decrypt(stanza):
        decrypt_started.set()
        await release_decrypt.wait()
        return await original(stanza)
    model.complete.side_effect = complete
    plugin.decrypt_message.side_effect = decrypt
    await client.dialogues.submit(CHILD, 'Вопрос')
    await asyncio.wait_for(model_started.wait(), 1)
    server.records[CHILD].append(('manual', event(OWN + '/phone', CHILD, ident='manual-poll'), time.time()))
    task = asyncio.create_task(client.archive.synchronize())
    try:
        await asyncio.wait_for(decrypt_started.wait(), 1)
        assert client.holds[CHILD] == 1
        release_model.set()
        await asyncio.wait_for(asyncio.gather(*tuple(client.dialogues.tasks)), 1)
        assert not sent
        release_decrypt.set()
        await asyncio.wait_for(task, 1)
        assert await client.dialogues.mode(CHILD) == 'manual'
    finally:
        release_model.set()
        release_decrypt.set()
        await asyncio.gather(task, return_exceptions=True)
