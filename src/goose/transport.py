"""Один asyncio-цикл, ограниченная очередь и только зашифрованные ответы."""

import asyncio
from contextlib import suppress
from copy import deepcopy
import hashlib
import logging
import secrets
import ssl
import time
import xml.etree.ElementTree as ET

from slixmpp import ClientXMPP, JID, Message
from slixmpp.exceptions import IqError, IqTimeout
from slixmpp.xmlstream.handler import Callback
from slixmpp.xmlstream.matcher import MatchXPath

from .commands import Commands
from .archive import ArchiveError, ArchiveSync
from .config import ConfigError, bare_jid
from .dialogue import Dialogues
from .facts import Facts
from .facts import intent as facts_intent
from .events import SID, message_keys, ordinary_message, owner_stanza
from .model import OpenAICompatible
from .omemo import LEGACY_NS, ManualTrustRequired, register
from .storage import OmemoStorage

log = logging.getLogger("goose")
CARBONS = "urn:xmpp:carbons:2"
MAM = "urn:xmpp:mam:2"
FORWARD = "urn:xmpp:forward:0"
DELAY = "urn:xmpp:delay"


def classify(stanza, own_jid):
    """Классифицировать внешнюю stanza до допуска к обработке команд."""
    xml = stanza.xml
    try:
        sender = bare_jid(str(stanza["from"]))
    except ConfigError:
        return "invalid-sender"
    if any(e.tag.startswith("{urn:xmpp:mam:") for e in xml.iter()):
        return "archive"
    if xml.find(f".//{{{DELAY}}}delay") is not None or xml.find(".//{jabber:x:delay}x") is not None:
        return "delayed"
    if xml.find(f"{{{CARBONS}}}sent") is not None or xml.find(f"{{{CARBONS}}}received") is not None:
        return "carbon" if sender == own_jid else "invalid-carbon"
    if xml.find(f".//{{{FORWARD}}}forwarded") is not None:
        return "forwarded"
    if sender == own_jid:
        return "own"
    if stanza["type"] not in {"chat", "normal"}:
        return "service"
    if xml.find("{urn:xmpp:receipts}received") is not None or xml.find("{urn:xmpp:chat-markers:0}displayed") is not None:
        return "receipt"
    return "direct"


def incoming_stanza(stanza, own_jid):
    """Принять только прямую доставку или проверенную живую received-копию.

    Авторизация выполняется по внутреннему from, после расшифровки дополнительно
    проверяется устройство. Внешний from Carbons обязан быть точным bare JID бота.
    """
    reason = classify(stanza, own_jid)
    if reason != "carbon":
        return (stanza, reason) if reason == "direct" else (None, reason)
    if str(stanza["from"]) != own_jid or stanza["type"] not in {"chat", "normal"}:
        return None, "invalid-carbon"
    received = stanza.xml.findall(f"{{{CARBONS}}}received")
    sent = stanza.xml.findall(f"{{{CARBONS}}}sent")
    if len(sent) == 1 and not received:
        return None, "sent-carbon"
    if len(received) != 1 or sent:
        return None, "invalid-carbon"
    children = list(received[0])
    if len(children) != 1 or children[0].tag != f"{{{FORWARD}}}forwarded":
        return None, "invalid-carbon"
    forwarded = list(children[0])
    if len(forwarded) != 1 or forwarded[0].tag != "{jabber:client}message":
        return None, "invalid-carbon"
    inner = Message(xml=deepcopy(forwarded[0]), stream=stanza.stream)
    try:
        if (bare_jid(str(stanza["to"])) != own_jid
                or bare_jid(str(inner["to"])) != own_jid):
            return None, "invalid-carbon"
    except ConfigError:
        return None, "invalid-carbon"
    inner_reason = classify(inner, own_jid)
    if inner_reason != "direct":
        return None, inner_reason
    return inner, "received-carbon"


def delivery_key(stanza):
    sender = bare_jid(str(stanza["from"]))
    origin = stanza.xml.find("{urn:xmpp:sid:0}origin-id")
    identifier = origin.get("id") if origin is not None else None
    identifier = identifier or stanza["id"]
    ttl = 7 * 86400 if identifier else 60
    payload = identifier or ET.tostring(stanza.xml, encoding="unicode")
    return hashlib.sha256(f"{sender}\0{payload}".encode()).hexdigest(), ttl


class GooseClient(ClientXMPP):
    def __init__(self, config, password, database=None, *, probe=False, maintenance=False):
        suffix = f"doctor-{secrets.token_hex(4)}" if probe else config.resource
        super().__init__(f"{config.jid}/{suffix}", password)
        self.config = config
        self.database = database
        self.probe = probe
        self.serving_commands = not (probe or maintenance)
        self.ready = asyncio.Event()
        self.ended = asyncio.Event()
        self.failed = False
        self.fatal = False
        self.failure_reason = None
        self.manager = None
        self.dialogues = None
        self.facts = None
        self.start_task = None
        self.archive = None
        self.own_device_id = None
        self.accepting = False
        self.holds = {}
        self.own_events = {}
        self.message_lock = asyncio.Lock()
        self.features = {"carbons": "не проверено", "mam": "не проверено"}
        self.queue = asyncio.Queue(maxsize=64)
        self.worker = None
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.enable_plaintext = False
        self.auto_authorize = False
        self.auto_subscribe = False
        for name in ("xep_0030", "xep_0199", "xep_0280", "xep_0313", "xep_0380"):
            self.register_plugin(name)
        if not probe:
            register(self, OmemoStorage(database))
        if self.serving_commands:
            self.commands = Commands(config, database, self.status)
            self.register_handler(Callback("Goose messages", MatchXPath(f"{{{self.default_ns}}}message"), self.admit))
            self.register_handler(Callback('Goose archive',
                MatchXPath(f'{{{self.default_ns}}}message/{{{MAM}}}result'), self.archive_message))
        self.add_event_handler("session_start", self.on_start)
        self.add_event_handler("failed_auth", self.on_auth_failure)
        self.add_event_handler("failed_all_auth", self.on_auth_failure)
        self.add_event_handler("goose_omemo_failed", self.on_initialization_failure)
        self.add_event_handler("disconnected", self.on_disconnected)

    def reschedule_connection_attempt(self):
        # Публичная точка расширения XMLStream: повторами владеет run_sessions.
        self.failed = True
        self.ended.set()
        return None

    def reconnect(self, wait=2, reason="Reconnecting"):
        # В том числе see-other-host: новая сессия создаётся контроллером с исходным
        # доменом TLS и лимитом попыток, а не скрытым reconnect библиотеки.
        self.failed = True
        self.ended.set()

    def on_auth_failure(self, _):
        self.failed = self.fatal = True
        self.failure_reason = "Ошибка авторизации XMPP; проверьте учётную запись."
        log.error("xmpp_auth_failed")
        self.cancel_connection_attempt()
        self.ended.set()

    def on_disconnected(self, _):
        self.ready.clear()
        self.ended.set()

    def on_initialization_failure(self, _):
        self.failed = self.fatal = True
        self.failure_reason = "Не удалось инициализировать OMEMO; проверьте PEP, сеть и хранилище."
        log.error("omemo_initialization_failed")
        self.ended.set()

    async def on_start(self, _):
        self.start_task = asyncio.current_task()
        try:
            if self.boundjid.bare != self.config.jid:
                raise ConfigError("Сервер привязал непредусмотренный аккаунт.")
            self.accepting = self.serving_commands
            # Список контактов не даёт прав; принятие подписок выключено.
            await self.get_roster(timeout=10)
            # Отрицательный приоритет исключает bare-доставку по RFC 6121.
            # Сервис принимает её, а временные проверки/управление — только IQ/PEP.
            self.send_presence(ppriority=0 if self.serving_commands else -1)
            info = await self.plugin["xep_0030"].get_info(jid=self.boundjid.domain, timeout=10)
            supported = set(info["disco_info"]["features"])
            self.features = {"carbons": CARBONS in supported, "mam": "не проверено"}
            try:
                # Архив пользователя объявляется на bare JID аккаунта, отдельно
                # от возможностей домена. Требуется свежий сетевой disco#info.
                account_info = await self.plugin["xep_0030"].get_info(
                    jid=self.boundjid.bare, local=False, cached=False, timeout=10)
            except (IqError, IqTimeout):
                # Отказ/таймаут не доказывает отсутствие MAM и не запрещает чат.
                log.warning("xmpp_discovery_incomplete feature=mam")
            else:
                self.features["mam"] = MAM in account_info["disco_info"]["features"]
            if self.features["carbons"]:
                await self.plugin["xep_0280"].enable(timeout=10)
            if not self.probe:
                try:
                    self.manager = await self.plugin["xep_0384"].get_session_manager()
                    self.own_device_id = (await self.manager.get_own_device_information())[0].device_id
                except Exception:
                    # Ошибка библиотеки после частичной инициализации завершается
                    # выходом процесса: asyncio.run отменит и её внутренние задачи.
                    self.on_initialization_failure(None)
                    raise
                if self.serving_commands:
                    self.own_events = dict.fromkeys(await self.database.owner_event_keys())
                    model = OpenAICompatible(self.config.llm) if self.config.llm.enabled else None
                    self.dialogues = Dialogues(self.config, self.database, model, self.prepare_reply)
                    await self.dialogues.initialize()
                    self.commands.dialogues = self.dialogues
                    self.facts = Facts(self.config, self.database, self.prepare_reply)
                    self.commands.facts = self.facts
                    for jid, count in self.holds.items():
                        for _ in range(count):
                            self.dialogues.hold(jid)
                    self.archive = ArchiveSync(self)
                    self.manager.before_history_sync()
                    try:
                        await self.archive.synchronize(startup=True)
                    finally:
                        await self.manager.after_history_sync()
                    self.worker = asyncio.create_task(self.process_queue())
                    self.archive.start()
            self.ready.set()
            log.info("xmpp_ready carbons=%s mam=%s", *self.features.values())
        except asyncio.CancelledError:
            raise
        except Exception:
            # Исключения библиотек могут содержать JID/XML; в журнал идут только коды.
            self.failed = True
            log.error("xmpp_initialization_failed")
            self.ended.set()

    def status(self):
        archive = ('не запускалась' if self.archive is None else
                   f"готово {sum(s == 'ready' for s in self.archive.states.values())}/{len(self.archive.states)}")
        return (f"Этап 3; соединение={self.ready.is_set()}; OMEMO=legacy, manual trust; "
                f"Carbons={self.features['carbons']}; MAM={self.features['mam']}; "
                f"восстановление архива: {archive}; "
                f"модель={'включена' if self.config.llm.enabled else 'отключена'}; "
                f"погода={self.config.weather.enabled}; новости={self.config.news.enabled}; "
                "архив не запускает команды; расписание отключено; ручная отправка включает manual.")

    def archive_message(self, stanza):
        if self.archive:
            self.archive.collect(stanza)

    def hold(self, jid):
        self.holds[jid] = self.holds.get(jid, 0) + 1
        if self.dialogues:
            self.dialogues.hold(jid)
        if self.facts:
            self.facts.cancel(jid)

    def release(self, jid):
        if self.holds.get(jid, 0) <= 1:
            self.holds.pop(jid, None)
        else:
            self.holds[jid] -= 1
        if self.dialogues:
            self.dialogues.release(jid)

    def remember_own_keys(self, keys):
        self.own_events.update(dict.fromkeys(keys))
        while len(self.own_events) > 10000:
            self.own_events.pop(next(iter(self.own_events)))

    def admit(self, stanza):
        if not (self.ready.is_set() or self.accepting) or len(ET.tostring(stanza.xml)) > 65536:
            return
        owner = owner_stanza(stanza, self.config.jid, self.config.resource, self.config.allowed)
        if owner:
            incoming, peer = owner
            if any(key in self.own_events for key in message_keys(incoming, self.config.jid)):
                return
            self.hold(peer)
            incoming._goose_hold_peer = peer
            try:
                self.queue.put_nowait(incoming)
            except asyncio.QueueFull:
                self.release(peer)
                # Потеря ручного вмешательства требует нового восстановления, не auto.
                self.ready.clear()
                self.failed = True
                self.ended.set()
                log.warning('manual_message_overflow')
            return
        incoming, reason = incoming_stanza(stanza, self.config.jid)
        if incoming is None:
            log.info("message_ignored reason=%s", reason)
            return
        if bare_jid(str(incoming["from"])) not in self.config.allowed:
            return
        try:
            self.queue.put_nowait(incoming)
            log.info("message_received source=%s", reason)
        except asyncio.QueueFull:
            log.warning("message_ignored reason=queue_full")

    async def process_queue(self):
        while True:
            stanza = await self.queue.get()
            try:
                async with asyncio.timeout(30):
                    await self.process_message(stanza)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("message_processing_failed")
            finally:
                self.queue.task_done()

    async def process_message(self, stanza):
        async with self.message_lock:
            held = getattr(stanza, '_goose_hold_peer', None)
            try:
                owner = owner_stanza(stanza, self.config.jid, self.config.resource, self.config.allowed)
                if owner:
                    inner, peer = owner
                    if held is None:
                        self.hold(peer)
                        held = peer
                    await self.process_owner(inner, peer, time.time())
                    return
                incoming, _ = incoming_stanza(stanza, self.config.jid)
                if incoming is None or bare_jid(str(incoming['from'])) not in self.config.allowed:
                    return
                if bare_jid(str(incoming['to'])) != self.config.jid:
                    return
                await self.process_incoming(incoming)
            finally:
                if held:
                    self.release(held)

    async def process_incoming(self, stanza):
        plugin = self.plugin["xep_0384"]
        # Никакой реакции на открытый текст и неподдерживаемое шифрование.
        if LEGACY_NS not in plugin.is_encrypted(stanza):
            log.info("message_ignored reason=encryption_required_or_unsupported")
            return
        keys = message_keys(stanza, self.config.jid)
        key, _ = delivery_key(stanza)
        known = await self.database.event_kind(keys)
        if known or await self.database.was_seen(key):
            await self.database.remember_event(bare_jid(str(stanza['from'])), keys, known or 'ignored')
            log.info("message_ignored reason=duplicate")
            return
        try:
            decrypted, device = await plugin.decrypt_message(stanza)
            sender = bare_jid(str(stanza["from"]))
            if device.bare_jid != sender or device.trust_level_name != "TRUSTED":
                log.warning("message_ignored reason=untrusted_device")
                return
            body = decrypted["body"]
            if not body or len(body) > 2048:
                return
            if not body.lstrip().startswith('!') and facts_intent(body) is None and self.dialogues and self.config.llm.enabled:
                await self.dialogues.submit(sender, body, keys=keys)
                return
            if not await self.database.remember_event(sender, keys, 'incoming'):
                return
            if self.facts and self.facts.submit(sender, body):
                return
            reply = await self.commands.handle(sender, body)
            if reply is None:
                if self.dialogues:
                    await self.dialogues.submit(sender, body)
                return
            send = await self.prepare_reply(sender, reply)
            if send:
                send()
        except ManualTrustRequired:
            log.warning("reply_blocked reason=manual_trust_required")
        except Exception:
            log.warning("reply_blocked reason=omemo_or_command_failure")

    async def process_owner(self, stanza, peer, stamp, *, archived=False):
        keys = message_keys(stanza, self.config.jid)
        known = await self.database.event_kind(keys)
        if known:
            await self.database.remember_event(peer, keys, known)
            self.remember_own_keys(keys)
            return
        encrypted = stanza.xml.find(f'{{{LEGACY_NS}}}encrypted')
        header = encrypted.find(f'{{{LEGACY_NS}}}header') if encrypted is not None else None
        if (archived and header is not None and self.own_device_id is not None
                and header.get('sid') == str(self.own_device_id)
                and JID(str(stanza['from'])).resource == self.config.resource):
            await self.database.remember_event(peer, keys, 'bot')
            self.remember_own_keys(keys)
            return
        body = None
        failed = False
        if LEGACY_NS in self.plugin['xep_0384'].is_encrypted(stanza):
            try:
                decoded, device = await self.plugin['xep_0384'].decrypt_message(stanza)
                if (device.bare_jid != self.config.jid or device.trust_level_name != 'TRUSTED'
                        or device.device_id == self.own_device_id):
                    raise ValueError()
                body = decoded['body']
                if not body:
                    await self.database.remember_event(peer, keys, 'ignored')
                    self.remember_own_keys(keys)
                    return
                if len(body) > 2048:
                    raise ValueError()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Оболочка собственного сервера подтверждает отправку, но не её текст.
                body = None
                failed = True
        else:
            # Ручная открытая/неподдерживаемая отправка также уступает диалог;
            # её текст не импортируется и не становится открытым ответом.
            failed = True
        if self.dialogues:
            await self.dialogues.restore(peer, keys, 'manual', body, stamp, live=not archived)
        else:
            await self.database.remember_event(peer, keys, 'manual', stamp=stamp, live=not archived)
        if self.facts:
            self.facts.cancel(peer)
        self.remember_own_keys(keys)
        log.info('manual_message_seen text_restored=%s', body is not None)
        if archived and failed:
            raise ArchiveError()

    async def restore_message(self, stanza, stamp):
        sender = bare_jid(str(stanza['from']))
        if sender == self.config.jid:
            peer = bare_jid(str(stanza['to']))
            hold = not any(k in self.own_events for k in message_keys(stanza, self.config.jid))
            if hold:
                self.hold(peer)
            try:
                await self.process_owner(stanza, peer, stamp, archived=True)
            finally:
                if hold:
                    self.release(peer)
            return
        keys = message_keys(stanza, self.config.jid)
        known = await self.database.event_kind(keys)
        key, _ = delivery_key(stanza)
        if known or await self.database.was_seen(key):
            await self.database.remember_event(sender, keys, known or 'ignored')
            return
        plugin = self.plugin['xep_0384']
        if LEGACY_NS not in plugin.is_encrypted(stanza):
            await self.database.remember_event(sender, keys, 'ignored')
            return
        try:
            decoded, device = await plugin.decrypt_message(stanza)
            if device.bare_jid != sender or device.trust_level_name != 'TRUSTED':
                raise ValueError()
            body = decoded['body']
            if len(body) > 2048:
                raise ValueError()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ArchiveError() from None
        if not body or body.lstrip().startswith('!') or facts_intent(body) is not None:
            await self.database.remember_event(sender, keys, 'ignored')
        elif self.dialogues:
            await self.dialogues.restore(sender, keys, 'incoming', body, stamp)

    async def prepare_reply(self, sender, reply):
        guard = self.dialogues.states[sender].guard if self.dialogues else None
        plugin = self.plugin["xep_0384"]
        outgoing = self.make_message(mto=sender, mtype="chat", mbody=reply)
        outgoing["from"] = self.boundjid
        try:
            encrypted, errors = await plugin.encrypt_message(outgoing, {JID(sender)})
        except ManualTrustRequired:
            log.warning("reply_blocked reason=manual_trust_required")
            return None
        if errors or encrypted is None or LEGACY_NS not in plugin.is_encrypted(encrypted):
            log.warning("reply_blocked reason=encryption_failed")
            return None
        encrypted["eme"]["namespace"] = LEGACY_NS
        encrypted["eme"]["name"] = "OMEMO"
        encrypted['from'] = self.boundjid
        encrypted['to'] = sender
        # Шифровальщик сохраняет id, но удаляет остальные sid-теги исходной stanza.
        ET.SubElement(encrypted.xml, f'{{{SID}}}origin-id', {'id': encrypted['id']})
        keys = message_keys(encrypted, self.config.jid)
        await self.database.remember_event(sender, keys, 'bot')
        self.remember_own_keys(keys)
        def send():
            if (not self.ready.is_set() or self.holds.get(sender)
                    or (self.dialogues and self.dialogues.states[sender].guard != guard)):
                return False
            encrypted.send()
            log.info("reply_queued")
            return True
        return send

    async def close(self):
        self.ready.clear()
        self.accepting = False
        self.cancel_connection_attempt()
        for task in (self.worker, self.start_task):
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if self.archive:
            await self.archive.close()
        if self.dialogues:
            await self.dialogues.close()
        if self.facts:
            await self.facts.close()
        if not self.probe:
            await self.plugin["xep_0384"].aclose()
        with suppress(Exception):
            await self.disconnect(wait=2, ignore_send_queue=True)


async def wait_ready(client, stop, timeout):
    tasks = [asyncio.create_task(client.ready.wait()), asyncio.create_task(client.ended.wait()),
             asyncio.create_task(stop.wait())]
    try:
        done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        return bool(done) and client.ready.is_set() and not client.ended.is_set() and not stop.is_set()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_sessions(config, database, stop, action=None, *, probe=False, client_factory=GooseClient):
    password = config.password()  # Отсутствующий секрет не запускает повторов.
    for attempt in range(config.max_attempts):
        client = client_factory(config, password, database, probe=probe,
                                maintenance=action is not None)
        try:
            client.connect()
            if await wait_ready(client, stop, config.connect_timeout):
                if action:
                    async with asyncio.timeout(60):
                        await action(client)
                    return
                tasks = [asyncio.create_task(client.ended.wait()), asyncio.create_task(stop.wait())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            if stop.is_set():
                return
            if client.fatal:
                raise ConfigError(getattr(client, "failure_reason", None)
                                  or "Ошибка авторизации XMPP; проверьте учётную запись.")
        finally:
            await client.close()
        if attempt + 1 < config.max_attempts:
            log.warning("xmpp_retry attempt=%d", attempt + 2)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), min(2 ** attempt, 8))
            if stop.is_set():
                return
    raise ConfigError("Исчерпаны ограниченные попытки XMPP; проверьте TLS, сеть и настройки.")
