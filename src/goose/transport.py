"""Один asyncio-цикл, ограниченная очередь и только зашифрованные ответы."""

import asyncio
from contextlib import suppress
import hashlib
import logging
import secrets
import ssl
import xml.etree.ElementTree as ET

from slixmpp import ClientXMPP, JID
from slixmpp.xmlstream.handler import Callback
from slixmpp.xmlstream.matcher import MatchXPath

from .commands import Commands
from .config import ConfigError, bare_jid
from .omemo import LEGACY_NS, ManualTrustRequired, register
from .storage import OmemoStorage

log = logging.getLogger("goose")
CARBONS = "urn:xmpp:carbons:2"
MAM = "urn:xmpp:mam:2"
FORWARD = "urn:xmpp:forward:0"
DELAY = "urn:xmpp:delay"


def classify(stanza, own_jid):
    """Оболочки никогда не исполняются как команды, включая поддельные Carbons."""
    xml = stanza.xml
    try:
        sender = bare_jid(str(stanza["from"]))
    except ConfigError:
        return "invalid-sender"
    if xml.find(f"{{{CARBONS}}}sent") is not None or xml.find(f"{{{CARBONS}}}received") is not None:
        return "carbon" if sender == own_jid else "invalid-carbon"
    if any(e.tag.startswith("{urn:xmpp:mam:") for e in xml.iter()):
        return "archive"
    if xml.find(f".//{{{FORWARD}}}forwarded") is not None:
        return "forwarded"
    if xml.find(f".//{{{DELAY}}}delay") is not None or xml.find(".//{jabber:x:delay}x") is not None:
        return "delayed"
    if sender == own_jid:
        return "own"
    if stanza["type"] not in {"chat", "normal"}:
        return "service"
    if xml.find("{urn:xmpp:receipts}received") is not None or xml.find("{urn:xmpp:chat-markers:0}displayed") is not None:
        return "receipt"
    return "direct"


def delivery_key(stanza):
    sender = bare_jid(str(stanza["from"]))
    origin = stanza.xml.find("{urn:xmpp:sid:0}origin-id")
    identifier = origin.get("id") if origin is not None else None
    identifier = identifier or stanza["id"]
    ttl = 7 * 86400 if identifier else 60
    payload = identifier or ET.tostring(stanza.xml, encoding="unicode")
    return hashlib.sha256(f"{sender}\0{payload}".encode()).hexdigest(), ttl


class GooseClient(ClientXMPP):
    def __init__(self, config, password, database=None, *, probe=False):
        suffix = f"doctor-{secrets.token_hex(4)}" if probe else config.resource
        super().__init__(f"{config.jid}/{suffix}", password)
        self.config = config
        self.database = database
        self.probe = probe
        self.ready = asyncio.Event()
        self.ended = asyncio.Event()
        self.failed = False
        self.fatal = False
        self.failure_reason = None
        self.manager = None
        self.start_task = None
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
            self.commands = Commands(config, database, self.status)
            self.register_handler(Callback("Goose messages", MatchXPath(f"{{{self.default_ns}}}message"), self.admit))
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
            # Список контактов не даёт прав; принятие подписок выключено.
            await self.get_roster(timeout=10)
            self.send_presence(ppriority=-1)
            info = await self.plugin["xep_0030"].get_info(jid=self.boundjid.domain, timeout=10)
            supported = set(info["disco_info"]["features"])
            self.features = {"carbons": CARBONS in supported, "mam": MAM in supported}
            if self.features["carbons"]:
                await self.plugin["xep_0280"].enable(timeout=10)
            if not self.probe:
                try:
                    self.manager = await self.plugin["xep_0384"].get_session_manager()
                except Exception:
                    # Ошибка библиотеки после частичной инициализации завершается
                    # выходом процесса: asyncio.run отменит и её внутренние задачи.
                    self.on_initialization_failure(None)
                    raise
                self.worker = asyncio.create_task(self.process_queue())
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
        return (f"Этап 1; соединение={self.ready.is_set()}; OMEMO=legacy, manual trust; "
                f"Carbons={self.features['carbons']}; MAM={self.features['mam']}; "
                "архив не запускает команды; модель и расписание отключены.")

    def admit(self, stanza):
        reason = classify(stanza, self.config.jid)
        if reason != "direct":
            log.info("message_ignored reason=%s", reason)
            return
        if (not self.ready.is_set() or bare_jid(str(stanza["from"])) not in self.config.allowed
                or len(ET.tostring(stanza.xml)) > 65536):
            return
        try:
            self.queue.put_nowait(stanza)
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
        plugin = self.plugin["xep_0384"]
        # Никакой реакции на открытый текст и неподдерживаемое шифрование.
        if LEGACY_NS not in plugin.is_encrypted(stanza):
            log.info("message_ignored reason=encryption_required_or_unsupported")
            return
        key, ttl = delivery_key(stanza)
        if not await self.database.claim(key, ttl):
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
            reply = await self.commands.handle(sender, body)
            if reply is None:
                return
            outgoing = self.make_message(mto=sender, mtype="chat", mbody=reply)
            outgoing["from"] = self.boundjid
            encrypted, errors = await plugin.encrypt_message(outgoing, {JID(sender)})
            if errors or encrypted is None or LEGACY_NS not in plugin.is_encrypted(encrypted):
                log.warning("reply_blocked reason=encryption_failed")
                return
            # EME соответствует реально поддержанной версии протокола.
            encrypted["eme"]["namespace"] = LEGACY_NS
            encrypted["eme"]["name"] = "OMEMO"
            if self.ready.is_set():
                encrypted.send()
        except ManualTrustRequired:
            log.warning("reply_blocked reason=manual_trust_required")
        except Exception:
            log.warning("reply_blocked reason=omemo_or_command_failure")

    async def close(self):
        self.ready.clear()
        self.cancel_connection_attempt()
        for task in (self.worker, self.start_task):
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
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
        client = client_factory(config, password, database, probe=probe)
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
