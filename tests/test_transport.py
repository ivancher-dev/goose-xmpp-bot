import asyncio
from dataclasses import replace
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import xml.etree.ElementTree as ET

import pytest
from slixmpp import Message

from goose.cli import configure_logging, doctor, main
from goose.config import ConfigError
from goose.omemo import LEGACY_NS, GooseOmemo, ManualTrustRequired
from goose.transport import GooseClient, classify, run_sessions


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
    assert sent_plaintext["body"] == "pong"
    assert str(sent_plaintext["to"]) == "parent@example.org"
    # Сам клиент не подключался к сети; plugin_end требуется только реальному плагину.


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
    with pytest.raises(ConfigError, match="Исчерпаны"):
        await run_sessions(config, None, asyncio.Event(), client_factory=FailingClient)
    assert len(clients) == config.max_attempts
    assert all(c.close.await_count == 1 for c in clients)


def test_local_doctor_does_not_create_identity(config, capsys):
    doctor(config)
    assert "ещё не создана" in capsys.readouterr().out
    assert list(config.data_dir.iterdir()) == []


def test_cli_missing_password_is_redacted(config_file, monkeypatch, capsys):
    monkeypatch.delenv("GOOSE_XMPP_PASSWORD", raising=False)
    assert main(["run", "--config", str(config_file)]) == 2
    assert "Пароль XMPP" in capsys.readouterr().err


def test_third_party_logs_suppressed(capsys):
    configure_logging("INFO")
    logging.getLogger("slixmpp").warning("sensitive stanza and JID")
    logging.getLogger("goose").info("safe_status")
    output = capsys.readouterr().err
    assert "sensitive" not in output
    assert "safe_status" in output
