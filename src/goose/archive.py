"""Ограниченная MAM-синхронизация; архив никогда не исполняется как входящая команда."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import xml.etree.ElementTree as ET

from slixmpp import JID, Message
from slixmpp.exceptions import IqError, IqTimeout

from .config import bare_jid
from .events import DELAY, FORWARD, ordinary_message

log = logging.getLogger('goose')
MAM = 'urn:xmpp:mam:2'
RSM = 'http://jabber.org/protocol/rsm'
PAGE_SIZE = 50
MAX_PAGES = 5
SYNC_TIMEOUT = 20
POLL_INTERVAL = 60


class ArchiveError(Exception):
    pass


@dataclass
class Record:
    stanza: Message
    stamp: float
    ident: str


@dataclass
class Page:
    query_id: str
    peer: str
    end: float
    limit: int
    records: list = field(default_factory=list)
    size: int = 0
    invalid: bool = False


def record_from(stanza, page, own_jid):
    """Проверяется активный queryid, адрес архива и обе стороны вложенного сообщения."""
    results = stanza.xml.findall(f'{{{MAM}}}result')
    if len(results) != 1 or results[0].get('queryid') != page.query_id:
        raise ArchiveError()
    ident = results[0].get('id', '')
    if not ident or len(ident) > 1024:
        raise ArchiveError()
    children = list(results[0])
    if len(children) != 1 or children[0].tag != f'{{{FORWARD}}}forwarded':
        raise ArchiveError()
    forwarded = list(children[0])
    messages = [e for e in forwarded if e.tag == '{jabber:client}message']
    delays = [e for e in forwarded if e.tag == f'{{{DELAY}}}delay']
    if len(forwarded) != 2 or len(messages) != 1 or len(delays) != 1:
        raise ArchiveError()
    stamp = datetime.fromisoformat(delays[0].get('stamp', '').replace('Z', '+00:00'))
    if stamp.tzinfo is None or stamp.timestamp() > page.end + 1:
        raise ArchiveError()
    inner = Message(xml=deepcopy(messages[0]), stream=stanza.stream)
    sender, recipient = bare_jid(str(inner['from'])), bare_jid(str(inner['to']))
    if (sender, recipient) not in {(page.peer, own_jid), (own_jid, page.peer)}:
        raise ArchiveError()
    if any(e.tag.startswith(('{urn:xmpp:carbons:', '{urn:xmpp:mam:', '{urn:xmpp:forward:'))
           for e in inner.xml.iter()):
        raise ArchiveError()
    return Record(inner, stamp.timestamp(), ident)


class ArchiveSync:
    def __init__(self, client):
        self.client = client
        self.database = client.database
        self.active = None
        self.states = {jid: 'pending' for jid in client.config.allowed}
        self.task = None

    def collect(self, stanza):
        page = self.active
        if page is None:
            return
        # Чужие результаты не могут завершить или испортить активную выборку.
        if str(stanza['from']) not in {'', self.client.config.jid}:
            return
        if str(stanza['to']) not in {'', self.client.config.jid, str(self.client.boundjid)}:
            return
        if not any(r.get('queryid') == page.query_id for r in stanza.xml.findall(f'{{{MAM}}}result')):
            return
        if page.invalid:
            return
        size = len(ET.tostring(stanza.xml))
        page.size += size
        if (stanza['type'] not in {'normal', 'chat'} or size > 65536
                or page.size > 2 * 1024 * 1024 or len(page.records) >= page.limit):
            page.invalid = True
            return
        try:
            page.records.append(record_from(stanza, page, self.client.config.jid))
        except (ValueError, KeyError, TypeError, OverflowError, ArchiveError):
            page.invalid = True

    async def query(self, peer, end, *, after=None, baseline=False, start=None):
        iq = self.client.make_iq_set(ito=self.client.config.jid)
        iq['mam']['queryid'] = iq['id']
        iq['mam']['with'] = JID(peer)
        iq['mam']['end'] = datetime.fromtimestamp(end, timezone.utc)
        if start is not None:
            iq['mam']['start'] = datetime.fromtimestamp(start, timezone.utc)
        # RSM Set передаёт значение в XML.text без приведения типа.
        iq['mam']['rsm']['max'] = str(1 if baseline else PAGE_SIZE)
        if baseline:
            # Slixmpp 1.17: именно True создаёт пустой <before/>, '' удаляет его.
            iq['mam']['rsm']['before'] = True
        elif after:
            iq['mam']['rsm']['after'] = after
        page = Page(iq['id'], peer, end, 1 if baseline else PAGE_SIZE)
        self.active = page
        try:
            result = await iq.send(timeout=5)
        finally:
            self.active = None
        if (page.invalid or result['type'] != 'result'
                or str(result['from']) not in {'', self.client.config.jid}
                or str(result['to']) not in {'', self.client.config.jid, str(self.client.boundjid)}):
            raise ArchiveError()
        fins = result.xml.findall(f'{{{MAM}}}fin')
        if len(fins) != 1 or fins[0].get('stable', 'true') not in {'true', '1'}:
            raise ArchiveError()
        complete = fins[0].get('complete', 'false') in {'true', '1'}
        last = fins[0].findtext(f'{{{RSM}}}set/{{{RSM}}}last')
        if page.records:
            if last != page.records[-1].ident:
                raise ArchiveError()
        elif not complete or last:
            raise ArchiveError()
        return page.records, last, complete

    async def baseline(self, peer, end):
        _, last, _ = await self.query(peer, end, baseline=True)
        await self.database.save_archive_cursor(peer, last, end, confirmed=False)

    async def recover(self, peer, cursor, end):
        if cursor['stamp'] > end:
            raise ArchiveError()
        anchor = cursor['anchor']
        seen = set()
        for _ in range(MAX_PAGES):
            records, last, complete = await self.query(
                peer, end, after=anchor, start=cursor['stamp'] if anchor is None else None)
            for record in records:
                if record.ident in seen or record.ident == anchor:
                    raise ArchiveError()
                seen.add(record.ident)
                async with self.client.message_lock:
                    # Служебные события не являются историей разговора.
                    if ordinary_message(record.stanza, delayed=True):
                        await self.client.restore_message(record.stanza, record.stamp)
            if last:
                anchor = last
            if complete:
                await self.database.save_archive_cursor(peer, anchor, end)
                return
        raise ArchiveError()

    async def synchronize(self, *, startup=False):
        if not startup and self.client.features['mam'] not in (True, False):
            try:
                info = await self.client.plugin['xep_0030'].get_info(
                    jid=self.client.config.jid, local=False, cached=False, timeout=5)
            except (IqError, IqTimeout, TimeoutError, ConnectionError, OSError):
                log.warning('xmpp_discovery_incomplete feature=mam')
            else:
                self.client.features['mam'] = MAM in info['disco_info']['features']
        end = datetime.now(timezone.utc).timestamp()
        deadline = asyncio.get_running_loop().time() + SYNC_TIMEOUT
        peers = sorted(self.states)
        for index, peer in enumerate(peers):
            if self.client.features['mam'] is not True:
                self.states[peer] = 'unavailable'
                continue
            remaining = max(0, deadline - asyncio.get_running_loop().time())
            peer_deadline = asyncio.get_running_loop().time() + min(10, remaining / (len(peers) - index))
            try:
                async with asyncio.timeout(max(0, peer_deadline - asyncio.get_running_loop().time())):
                    before = await self.database.dialogue(peer)
                    cursor = await self.database.archive_cursor(peer)
                    if cursor is None:
                        await self.baseline(peer, end)
                        self.states[peer] = 'confirmation_required'
                    else:
                        await self.recover(peer, cursor, end)
                        self.states[peer] = 'ready' if cursor['confirmed'] else 'confirmation_required'
                        if cursor['confirmed'] and self.client.features['carbons']:
                            await self.database.resume_auto(peer, before['revision'])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.states[peer] = 'incomplete'
                temporary = isinstance(exc, (TimeoutError, IqTimeout, ConnectionError, OSError))
                if isinstance(exc, IqError):
                    temporary = exc.iq['error']['condition'] in {
                        'service-unavailable', 'remote-server-timeout', 'resource-constraint', 'internal-server-error'}
                await self.client.dialogues.archive_failed(peer, transient=temporary)
                if temporary:
                    # Подтверждённый anchor не теряем из-за краткого сбоя сети.
                    log.warning('archive_sync_pending reason=temporary')
                    continue
                # Новая точка не даёт auto без явного подтверждения владельца.
                try:
                    async with asyncio.timeout(max(0, peer_deadline - asyncio.get_running_loop().time())):
                        await self.baseline(peer, end)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                log.warning('archive_sync_incomplete')
        log.info('archive_sync_finished ready=%d pending=%d',
                 sum(s == 'ready' for s in self.states.values()),
                 sum(s != 'ready' for s in self.states.values()))

    async def poll(self):
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            await self.synchronize()

    def start(self):
        self.task = asyncio.create_task(self.poll())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
