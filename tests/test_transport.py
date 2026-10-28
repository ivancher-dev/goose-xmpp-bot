import asyncio
from dataclasses import replace
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import xml.etree.ElementTree as ET

import pytest
from slixmpp import Iq, JID, Message
from slixmpp.exceptions import IqError, IqTimeout

from goose.cli import configure_logging, doctor, main
from goose.config import ConfigError
from goose.dialogue import Dialogues
from goose.omemo import LEGACY_NS, GooseOmemo, ManualTrustRequired
from goose.transport import CARBONS, DELAY, FORWARD, MAM, GooseClient, classify, delivery_key, incoming_stanza, run_sessions


def message(sender="parent@example.org/mobile", body="!ping", extra="", id="msg1", kind="chat"):
    return Message(xml=ET.fromstring(
        f'<message xmlns="jabber:client" from="{sender}" to="goose-test@example.org" '
        f'id="{id}" type="{kind}"><body>{body}</body>{extra}</message>'))


@pytest.mark.parametrize("sender,extra,kind,reason", [
    ("goose-test@example.org/phone", "", "chat", "own"),
    ("goose-test@example.org", '<received xmlns="urn:xmpp:carbons:2"/>', "chat", "carbon"),
    ("parent@example.org", '<sent xmlns="urn:xmpp:carbons:2"/>', "chat", "invalid-carbon"),
    ("parent@example.org", '<result xmlns="urn:xmpp:mam:2"/>', "chat", "archive"),
    ("parent@example.org", '<result xmlns="urn:xmpp:mam:1"/>', "chat", "archive"),
    ("parent@example.org", '<forwarded xmlns="urn:xmpp:forward:0"/>', "chat", "forwarded"),
    ("parent@example.org", '<delay xmlns="urn:xmpp:delay"/>', "chat", "delayed"),
    ("parent@example.org", '<x xmlns="jabber:x:delay"/>', "chat", "delayed"),
    ("parent@example.org", '<received xmlns="urn:xmpp:receipts"/>', "chat", "receipt"),
    ("parent@example.org", "", "error", "service"),
    ("parent@example.org", "", "groupchat", "service"),
])
def test_envelopes_not_commands(sender, extra, kind, reason, config):
    assert classify(message(sender, extra=extra, kind=kind), config.jid) == reason


def test_received_carbon_preserves_original_identity_and_ciphertext(config, carbon_message):
    original = message(extra=f'<encrypted xmlns="{LEGACY_NS}"><header sid="42"/></encrypted>')
    carbon = carbon_message(original)
    inner, source = incoming_stanza(carbon, config.jid)
    assert source == "received-carbon"
    assert str(inner["from"]) == str(original["from"])
    assert str(inner["to"]) == str(original["to"])
    assert inner["id"] == original["id"]
    assert ET.tostring(inner.xml) == ET.tostring(original.xml)
    # Расшифровка не должна менять XML внешней оболочки.
    inner["body"] = "changed"
    assert carbon.xml.find(f".//{{jabber:client}}body").text == "!ping"


@pytest.mark.parametrize("case,reason", [
    ("foreign-sender", "invalid-carbon"),
    ("own-full-sender", "invalid-carbon"),
    ("missing-sender", "invalid-sender"),
    ("foreign-recipient", "invalid-carbon"),
    ("missing-recipient", "invalid-carbon"),
    ("foreign-inner-recipient", "invalid-carbon"),
    ("missing-inner-recipient", "invalid-carbon"),
    ("missing-inner-sender", "invalid-sender"),
    ("own-inner-sender", "own"),
    ("error-outer", "invalid-carbon"),
    ("error-inner", "service"),
    ("receipt-inner", "receipt"),
    ("sent", "sent-carbon"),
    ("mixed", "invalid-carbon"),
    ("duplicate-received", "invalid-carbon"),
    ("empty-carbon", "invalid-carbon"),
    ("empty-forwarded", "invalid-carbon"),
    ("duplicate-forwarded", "invalid-carbon"),
    ("duplicate-inner", "invalid-carbon"),
    ("iq-inner", "invalid-carbon"),
    ("nested-carbon", "carbon"),
    ("nested-forwarded", "forwarded"),
    ("mam", "archive"),
    ("delay-outer", "delayed"),
    ("delay-forwarded", "delayed"),
    ("delay-inner", "delayed"),
    ("legacy-delay", "delayed"),
])
def test_carbon_cannot_bypass_envelope_checks(config, carbon_message, case, reason):
    carbon = carbon_message(message())
    received = carbon.xml.find(f"{{{CARBONS}}}received")
    forwarded = received.find(f"{{{FORWARD}}}forwarded")
    inner = forwarded[0]
    if case == "foreign-sender":
        carbon["from"] = "stranger@example.org"
    elif case == "own-full-sender":
        carbon["from"] = f"{config.jid}/other-resource"
    elif case == "missing-sender":
        del carbon["from"]
    elif case == "foreign-recipient":
        carbon["to"] = "stranger@example.org"
    elif case == "missing-recipient":
        del carbon["to"]
    elif case == "foreign-inner-recipient":
        inner.set("to", "stranger@example.org")
    elif case == "missing-inner-recipient":
        del inner.attrib["to"]
    elif case == "missing-inner-sender":
        del inner.attrib["from"]
    elif case == "own-inner-sender":
        inner.set("from", f"{config.jid}/phone")
    elif case == "error-outer":
        carbon["type"] = "error"
    elif case == "error-inner":
        inner.set("type", "error")
    elif case == "receipt-inner":
        ET.SubElement(inner, "{urn:xmpp:receipts}received")
    elif case == "sent":
        received.tag = f"{{{CARBONS}}}sent"
    elif case == "mixed":
        ET.SubElement(carbon.xml, f"{{{CARBONS}}}sent")
    elif case == "duplicate-received":
        ET.SubElement(carbon.xml, f"{{{CARBONS}}}received")
    elif case == "empty-carbon":
        received.remove(forwarded)
    elif case == "empty-forwarded":
        forwarded.remove(inner)
    elif case == "duplicate-forwarded":
        ET.SubElement(received, f"{{{FORWARD}}}forwarded")
    elif case == "duplicate-inner":
        forwarded.append(message().xml)
    elif case == "iq-inner":
        inner.tag = "{jabber:client}iq"
    elif case == "nested-carbon":
        ET.SubElement(inner, f"{{{CARBONS}}}received")
        inner.set("from", config.jid)
    elif case == "nested-forwarded":
        ET.SubElement(inner, f"{{{FORWARD}}}forwarded")
    elif case == "mam":
        ET.SubElement(inner, "{urn:xmpp:mam:2}result")
    elif case == "delay-outer":
        ET.SubElement(carbon.xml, f"{{{DELAY}}}delay")
    elif case == "delay-forwarded":
        ET.SubElement(forwarded, f"{{{DELAY}}}delay")
    elif case == "delay-inner":
        ET.SubElement(inner, f"{{{DELAY}}}delay")
    elif case == "legacy-delay":
        ET.SubElement(inner, "{jabber:x:delay}x")
    assert incoming_stanza(carbon, config.jid) == (None, reason)


def test_delivery_id_scoped_by_sender_and_origin_id():
    first = message(extra='<origin-id xmlns="urn:xmpp:sid:0" id="origin1"/>', id="different1")
    second = message(extra='<origin-id xmlns="urn:xmpp:sid:0" id="origin1"/>', id="different2")
    assert delivery_key(first) == delivery_key(second)
    assert delivery_key(message(sender="pupil1@example.org")) != delivery_key(message())
    assert delivery_key(message(id=""))[1] == 60
    assert delivery_key(message())[1] == 7 * 86400


async def test_reconnect_delegates_to_controller(config):
    client = GooseClient(config, "test-only", probe=True)
    client.connect = Mock()
    client.reconnect()
    assert client.ended.is_set()
    client.connect.assert_not_called()
    await client.close()


@pytest.fixture
def plugin():
    return SimpleNamespace(
        is_encrypted=lambda m: {LEGACY_NS} if m.xml.find(f"{{{LEGACY_NS}}}encrypted") is not None else set(),
        decrypt_message=AsyncMock(return_value=(message(), SimpleNamespace(bare_jid="parent@example.org", trust_level_name="TRUSTED"))),
        encrypt_message=AsyncMock(),
    )


async def test_encrypted_command_and_duplicate(config, database, plugin, monkeypatch):
    client = GooseClient(config, "test-only", database)
    client.init_plugins()
    monkeypatch.setitem(client.plugin._plugins, "xep_0384", plugin)
    client.ready.set()
    encrypted = message(extra=f'<encrypted xmlns="{LEGACY_NS}"/>')
    outgoing = message(extra=f'<encrypted xmlns="{LEGACY_NS}"/>')
    monkeypatch.setattr(outgoing, "send", Mock())
    plugin.encrypt_message.return_value = (outgoing, frozenset())
    await client.process_message(encrypted)
    await client.process_message(encrypted)
    assert plugin.decrypt_message.await_count == 1
    outgoing.send.assert_called_once()
    sent_plaintext = plugin.encrypt_message.call_args.args[0]
    assert sent_plaintext["body"] == "Га-Га-Га!"
    assert str(sent_plaintext["to"]) == "parent@example.org"
    # Сам клиент не подключался к сети; plugin_end требуется только реальному плагину.


async def test_pause_command_passes_transport_while_model_is_waiting(config, database, plugin, monkeypatch, tmp_path):
    persona = tmp_path / "persona.md"
    persona.write_text("Учебный Гусь.")
    config = replace(config, llm=replace(config.llm, enabled=True, persona_file=persona))
    client = GooseClient(config, "fixture", database)
    client.init_plugins()
    client.ready.set()
    monkeypatch.setitem(client.plugin._plugins, "xep_0384", plugin)
    started, finish = asyncio.Event(), asyncio.Event()
    async def complete(_messages):
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            await finish.wait()
        return "Запоздалый ответ"
    engine = Dialogues(config, database, SimpleNamespace(complete=complete, close=AsyncMock()), client.prepare_reply)
    await engine.initialize()
    client.dialogues = client.commands.dialogues = engine
    async def decrypt(stanza):
        sender = "pupil1@example.org" if stanza["id"] == "question" else "parent@example.org"
        text = "Вопрос" if stanza["id"] == "question" else "!admin mode pupil1@example.org manual"
        return message(sender=sender, body=text), SimpleNamespace(bare_jid=sender, trust_level_name="TRUSTED")
    plugin.decrypt_message.side_effect = decrypt
    outgoing = message(extra=f'<encrypted xmlns="{LEGACY_NS}"/>')
    monkeypatch.setattr(outgoing, "send", Mock())
    plugin.encrypt_message.return_value = (outgoing, frozenset())
    try:
        await engine.set_mode("pupil1@example.org", "auto")
        question = message(sender="pupil1@example.org", id="question", extra=f'<encrypted xmlns="{LEGACY_NS}"/>')
        await asyncio.wait_for(client.process_message(question), 0.5)
        await asyncio.wait_for(started.wait(), 1)
        pause = message(id="pause", extra=f'<encrypted xmlns="{LEGACY_NS}"/>')
        await asyncio.wait_for(client.process_message(pause), 0.5)
        assert plugin.encrypt_message.call_args.args[0]["body"] == "Режим: manual"
        finish.set()
        await asyncio.wait_for(asyncio.gather(*tuple(engine.tasks), return_exceptions=True), 1)
        outgoing.send.assert_called_once()
        assert await engine.mode("pupil1@example.org") == "manual"
    finally:
        finish.set()
        await engine.close()


async def test_close_cancels_initialization_before_closing_dialogues(config, database, monkeypatch):
    client = GooseClient(config, "fixture", database)
    client.init_plugins()
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def initialize():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    async def close():
        assert cancelled.is_set()
    dialogues = SimpleNamespace(initialize=initialize, close=AsyncMock(side_effect=close))
    monkeypatch.setattr("goose.transport.Dialogues", lambda *args, **kwargs: dialogues)
    monkeypatch.setattr(client, "get_roster", AsyncMock())
    monkeypatch.setattr(client, "send_presence", Mock())
    monkeypatch.setattr(client.plugin["xep_0030"], "get_info", AsyncMock(return_value={"disco_info": {"features": []}}))
    manager = SimpleNamespace(get_own_device_information=AsyncMock(return_value=(SimpleNamespace(device_id=11), ())))
    monkeypatch.setattr(client.plugin["xep_0384"], "get_session_manager", AsyncMock(return_value=manager))
    monkeypatch.setattr(client.plugin["xep_0384"], "aclose", AsyncMock())
    starting = asyncio.create_task(client.on_start(None))
    await asyncio.wait_for(entered.wait(), 1)
    await client.close()
    assert starting.cancelled()
    assert not client.ready.is_set()
    dialogues.close.assert_awaited_once()


@pytest.mark.parametrize("failure", ["decrypt", "encrypt", "errors", "untrusted", "empty", "plaintext", "unsupported"])
async def test_encryption_failure_never_sends_cleartext(config, database, plugin, monkeypatch, failure):
    client = GooseClient(config, "test-only", database)
    client.init_plugins()
    monkeypatch.setitem(client.plugin._plugins, "xep_0384", plugin)
    client.ready.set()
    outgoing = Mock()
    if failure == "decrypt":
        plugin.decrypt_message.side_effect = RuntimeError("sensitive payload")
    elif failure == "untrusted":
        plugin.decrypt_message.return_value[1].trust_level_name = "UNDECIDED"
    elif failure == "empty":
        plugin.decrypt_message.return_value[0]["body"] = ""
    elif failure == "encrypt":
        plugin.encrypt_message.side_effect = RuntimeError("sensitive payload")
    else:
        plugin.encrypt_message.return_value = (None, frozenset({"failure"}))
    if failure == "plaintext":
        incoming = message()
    elif failure == "unsupported":
        incoming = message(extra='<encrypted xmlns="urn:xmpp:omemo:2"/>')
    else:
        incoming = message(extra=f'<encrypted xmlns="{LEGACY_NS}"/>')
    monkeypatch.setattr(client, "send_message", outgoing)
    await client.process_message(incoming)
    outgoing.assert_not_called()
    if failure in {"decrypt", "untrusted", "empty", "plaintext", "unsupported"}:
        plugin.encrypt_message.assert_not_called()


async def test_admission_queue_is_bounded_and_checks_identity(config, database):
    client = GooseClient(config, "test-only", database)
    client.init_plugins()
    client.ready.set()
    client.admit(message(sender="stranger@example.org"))
    client.admit(message(sender=config.jid))
    client.admit(message(extra='<received xmlns="urn:xmpp:carbons:2"/>'))
    assert client.queue.empty()
    for _ in range(100):
        client.admit(message())
    assert client.queue.qsize() == 64
    await client.close()


async def test_received_carbon_uses_inner_allowlist(config, database, carbon_message):
    client = GooseClient(config, "test-only", database)
    client.init_plugins()
    client.ready.set()
    client.admit(carbon_message(message(sender="stranger@example.org")))
    client.admit(carbon_message(message(), direction="sent"))
    assert client.queue.empty()
    client.admit(carbon_message(message()))
    assert str(client.queue.get_nowait()["from"]) == "parent@example.org/mobile"
    await client.close()


@pytest.mark.parametrize("mode,priority", [("service", 0), ("probe", -1), ("maintenance", -1)])
async def test_only_service_accepts_bare_delivery_and_runs_commands(config, database, monkeypatch, mode, priority):
    client = GooseClient(config, "test-only", database,
                         probe=mode == "probe", maintenance=mode == "maintenance")
    client.init_plugins()
    monkeypatch.setattr(client, "get_roster", AsyncMock())
    monkeypatch.setattr(client, "send_presence", Mock())
    monkeypatch.setattr(client.plugin["xep_0030"], "get_info",
                        AsyncMock(return_value={"disco_info": {"features": [CARBONS]}}))
    monkeypatch.setattr(client.plugin["xep_0280"], "enable", AsyncMock())
    if mode != "probe":
        manager = SimpleNamespace(get_own_device_information=AsyncMock(return_value=(SimpleNamespace(device_id=11), ())),
                                  before_history_sync=Mock(), after_history_sync=AsyncMock())
        monkeypatch.setattr(client.plugin["xep_0384"], "get_session_manager",
                            AsyncMock(return_value=manager))
    await client.on_start(None)
    assert client.ready.is_set()
    client.send_presence.assert_called_once_with(ppriority=priority)
    assert hasattr(client, "commands") == (mode == "service")
    assert (client.worker is not None) == (mode == "service")
    await client.close()


async def test_management_action_never_starts_command_service(config, monkeypatch):
    monkeypatch.setenv(config.password_env, "test-only")
    client = SimpleNamespace(ready=asyncio.Event(), ended=asyncio.Event(), close=AsyncMock())
    client.connect = client.ready.set
    factory = Mock(return_value=client)
    action = AsyncMock()
    await run_sessions(config, None, asyncio.Event(), action=action, client_factory=factory)
    assert factory.call_args.kwargs["maintenance"] is True
    action.assert_awaited_once_with(client)
    client.close.assert_awaited_once()


@pytest.mark.parametrize("domain_features,account_features,carbons,mam", [
    ([CARBONS], [MAM], True, True),
    ([CARBONS, MAM], [], True, False),
    ([], [MAM], False, True),
    ([], [], False, False),
])
async def test_mam_discovery_uses_account_not_domain(config, monkeypatch, domain_features, account_features, carbons, mam):
    client = GooseClient(config, "test-only", probe=True)
    client.init_plugins()
    monkeypatch.setattr(client, "get_roster", AsyncMock())
    monkeypatch.setattr(client, "send_presence", Mock())
    async def info(*, jid, **kwargs):
        if jid == config.jid:
            assert kwargs["local"] is False
            assert kwargs["cached"] is False
            return {"disco_info": {"features": account_features}}
        assert jid == JID(config.jid).domain
        return {"disco_info": {"features": domain_features}}
    disco = AsyncMock(side_effect=info)
    monkeypatch.setattr(client.plugin["xep_0030"], "get_info", disco)
    enable = AsyncMock()
    monkeypatch.setattr(client.plugin["xep_0280"], "enable", enable)
    try:
        await client.on_start(None)
        assert client.ready.is_set()
        assert client.features == {"carbons": carbons, "mam": mam}
        assert enable.await_count == int(carbons)
        assert disco.await_count == 2
    finally:
        await client.close()


@pytest.mark.parametrize("failure", [IqError, IqTimeout, asyncio.CancelledError])
async def test_mam_discovery_failure_is_unknown_and_cancellation_propagates(config, monkeypatch, failure):
    from goose import transport
    client = GooseClient(config, "test-only", probe=True)
    client.init_plugins()
    monkeypatch.setattr(client, "get_roster", AsyncMock())
    monkeypatch.setattr(client, "send_presence", Mock())
    iq = Iq()
    iq["error"]["condition"] = "forbidden"
    iq["error"]["text"] = "private-diagnostic-must-not-be-logged"
    error = failure() if failure is asyncio.CancelledError else failure(iq)
    disco = AsyncMock(side_effect=[{"disco_info": {"features": [CARBONS, MAM]}}, error])
    monkeypatch.setattr(client.plugin["xep_0030"], "get_info", disco)
    enable = AsyncMock()
    monkeypatch.setattr(client.plugin["xep_0280"], "enable", enable)
    warning = Mock()
    monkeypatch.setattr(transport.log, "warning", warning)
    try:
        if failure is asyncio.CancelledError:
            with pytest.raises(asyncio.CancelledError):
                await client.on_start(None)
            assert not client.ready.is_set()
            warning.assert_not_called()
        else:
            await client.on_start(None)
            assert client.ready.is_set()
            assert not client.failed
            assert "MAM=не проверено" in client.status()
            enable.assert_awaited_once_with(timeout=10)
            warning.assert_called_once_with("xmpp_discovery_incomplete feature=mam")
        assert client.features == {"carbons": True, "mam": "не проверено"}
        assert disco.await_count == 2
    finally:
        await client.close()


async def test_tls_and_probe_has_no_omemo_or_database(config):
    client = GooseClient(config, "test-only", probe=True)
    client.init_plugins()
    assert not client.enable_plaintext
    assert client.ssl_context.check_hostname
    import ssl
    assert client.ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert "xep_0384" not in set(client.plugin)
    assert client.requested_jid.resource != config.resource
    assert client.database is None
    await client.close()


async def test_no_blind_trust():
    assert GooseOmemo._btbv_enabled.fget(None) is False
    with pytest.raises(ManualTrustRequired):
        await GooseOmemo._prompt_manual_trust(None, frozenset(), None)


async def test_missing_password_does_not_construct_client(config, monkeypatch):
    monkeypatch.delenv(config.password_env, raising=False)
    factory = Mock()
    with pytest.raises(ConfigError):
        await run_sessions(config, None, asyncio.Event(), client_factory=factory)
    factory.assert_not_called()


async def test_retries_bounded_and_close_each_client(config, monkeypatch):
    from goose.config import TransientError
    monkeypatch.setenv(config.password_env, "test-only")
    clients = []
    class FailingClient:
        def __init__(self, *args, **kwargs):
            self.ready, self.ended = asyncio.Event(), asyncio.Event()
            self.fatal = False
            self.close = AsyncMock()
            clients.append(self)
        def connect(self):
            self.ended.set()
    async def immediate_timeout(*args):
        args[0].close()
        raise TimeoutError
    monkeypatch.setattr(asyncio, "wait_for", immediate_timeout)
    with pytest.raises(TransientError, match="Исчерпаны"):
        await run_sessions(config, None, asyncio.Event(), client_factory=FailingClient)
    assert len(clients) == config.max_attempts
    assert all(c.close.await_count == 1 for c in clients)


async def test_auth_failure_not_retried(config, monkeypatch):
    monkeypatch.setenv(config.password_env, "test-only")
    client = SimpleNamespace(ready=asyncio.Event(), ended=asyncio.Event(), fatal=True, close=AsyncMock())
    client.connect = lambda: client.ended.set()
    factory = Mock(return_value=client)
    with pytest.raises(ConfigError, match="авторизации"):
        await run_sessions(config, None, asyncio.Event(), client_factory=factory)
    factory.assert_called_once()
    client.close.assert_awaited_once()


async def test_stop_closes_running_session(config, monkeypatch):
    monkeypatch.setenv(config.password_env, "test-only")
    stop = asyncio.Event()
    client = SimpleNamespace(ready=asyncio.Event(), ended=asyncio.Event(), fatal=False, close=AsyncMock())
    def connected():
        client.ready.set()
        asyncio.get_running_loop().call_later(0.01, stop.set)
    client.connect = connected
    await run_sessions(config, None, stop, client_factory=Mock(return_value=client))
    client.close.assert_awaited_once()


def test_local_doctor_does_not_create_identity(config, capsys, monkeypatch):
    monkeypatch.delenv(config.password_env, raising=False)
    doctor(config)
    output = capsys.readouterr().out
    assert "ещё не создана" in output
    assert "не передан текущему процессу" in output
    assert "Локальная диагностика завершена успешно" in output
    assert list(config.data_dir.iterdir()) == []


def test_cli_missing_password_is_redacted(config_file, monkeypatch, capsys):
    monkeypatch.delenv("GOOSE_XMPP_PASSWORD", raising=False)
    assert main(["run", "--config", str(config_file)]) == 2
    assert "Пароль XMPP" in capsys.readouterr().err


@pytest.mark.parametrize('temporary,code', [(True, 75), (False, 2)])
def test_cli_distinguishes_retryable_failure_from_fatal_configuration(config_file, monkeypatch, capsys, temporary, code):
    from goose.config import TransientError
    async def fail(*args):
        raise (TransientError if temporary else ConfigError)('Подставная ошибка')
    monkeypatch.setattr('goose.cli.execute', fail)
    assert main(['run', '--config', str(config_file)]) == code
    assert 'Подставная ошибка' in capsys.readouterr().err


def test_cli_permission_error_has_safe_message(config_file, monkeypatch, capsys):
    from goose import cli
    def denied(*args, **kwargs):
        raise PermissionError("do-not-print-private-path-or-secret")
    monkeypatch.setattr(cli, "load_config", denied)
    assert main(["doctor", "--config", str(config_file)]) == 2
    error = capsys.readouterr().err
    assert "недостаточно прав" in error
    assert "do-not-print" not in error


def test_third_party_logs_suppressed(capsys):
    configure_logging("INFO")
    logging.getLogger("slixmpp").warning("sensitive stanza and JID")
    logging.getLogger("goose").info("safe_status")
    output = capsys.readouterr().err
    assert "sensitive" not in output
    assert "safe_status" in output
