"""Настоящие ключи/ratchet и XML плагина; подставной только PEP-транспорт."""

from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from oldmemo import Oldmemo
from omemo.session_manager import BundleNotFound
from slixmpp import JID
from slixmpp_omemo import TrustLevel
from slixmpp_omemo.base_session_manager import BaseSessionManager

from goose.config import ConfigError
from goose.omemo import LEGACY_NS, ManualTrustRequired, fingerprint, set_verified_trust
from goose.storage import Database, OmemoStorage
from goose.transport import GooseClient


class Pep:
    def __init__(self):
        self.bundles = {}
        self.devices = {}
        self.sent = []

    def manager_class(self, jid):
        pep = self
        class Manager(BaseSessionManager):
            @staticmethod
            async def _upload_bundle(bundle):
                pep.bundles[(bundle.namespace, bundle.bare_jid, bundle.device_id)] = bundle

            @staticmethod
            async def _download_bundle(namespace, bare_jid, device_id):
                try:
                    return pep.bundles[(namespace, bare_jid, device_id)]
                except KeyError as exc:
                    raise BundleNotFound() from exc

            @staticmethod
            async def _delete_bundle(namespace, device_id):
                pep.bundles.pop((namespace, jid, device_id), None)

            @staticmethod
            async def _upload_device_list(namespace, device_list):
                pep.devices[(namespace, jid)] = dict(device_list)

            @staticmethod
            async def _download_device_list(namespace, bare_jid):
                return dict(pep.devices.get((namespace, bare_jid), {}))

            @staticmethod
            async def _send_message(message, bare_jid):
                pep.sent.append((message, bare_jid))

            @property
            def _btbv_enabled(self):
                return False

            async def _prompt_manual_trust(self, devices, identifier):
                raise ManualTrustRequired()
        return Manager


async def create_manager(pep, directory, jid):
    directory.mkdir(mode=0o700, exist_ok=True)
    db = Database(directory, jid)
    await db.initialize({})
    storage = OmemoStorage(db)
    manager = await pep.manager_class(jid).create(
        [Oldmemo(storage)], storage, jid, None, TrustLevel.UNDECIDED.value)
    await manager.after_history_sync()
    return manager, db


async def trust_peer(manager, jid):
    await manager.refresh_device_lists(jid)
    peer = next(iter(await manager.get_device_information(jid)))
    await set_verified_trust(manager, jid, peer.device_id, fingerprint(manager, peer))
    return peer


async def test_real_crypto_identity_trust_sessions_survive_restart(tmp_path):
    pep = Pep()
    alice, _ = await create_manager(pep, tmp_path / "alice", "alice@example.org")
    bob, _ = await create_manager(pep, tmp_path / "bob", "bob@example.org")
    reopened = None
    try:
        await alice.refresh_device_lists("bob@example.org")
        with pytest.raises(ManualTrustRequired):
            await alice.encrypt(frozenset({"bob@example.org"}), {LEGACY_NS: b"not-sent"})
        peer = next(iter(await alice.get_device_information("bob@example.org")))
        with pytest.raises(ConfigError, match="отпечаток"):
            await set_verified_trust(alice, "bob@example.org", peer.device_id, "0" * 64)
        assert next(iter(await alice.get_device_information("bob@example.org"))).trust_level_name == "UNDECIDED"
        await trust_peer(alice, "bob@example.org")
        await trust_peer(bob, "alice@example.org")
        own_before = (await bob.get_own_device_information())[0]
        packets, errors = await alice.encrypt(frozenset({"bob@example.org"}), {LEGACY_NS: b"first"})
        assert not errors
        assert (await bob.decrypt(next(iter(packets))))[0] == b"first"
        await bob.shutdown()
        reopened, db = await create_manager(pep, tmp_path / "bob", "bob@example.org")
        own_after = (await reopened.get_own_device_information())[0]
        assert (own_before.device_id, own_before.identity_key) == (own_after.device_id, own_after.identity_key)
        peer_after = next(iter(await reopened.get_device_information("alice@example.org")))
        assert peer_after.trust_level_name == "TRUSTED"
        packets, errors = await alice.encrypt(frozenset({"bob@example.org"}), {LEGACY_NS: b"after restart"})
        assert not errors
        assert (await reopened.decrypt(next(iter(packets))))[0] == b"after restart"
        await set_verified_trust(reopened, "alice@example.org", peer_after.device_id,
                                 fingerprint(reopened, peer_after), distrust=True)
        packets, _ = await alice.encrypt(frozenset({"bob@example.org"}), {LEGACY_NS: b"blocked"})
        from omemo.session_manager import SenderDistrusted
        with pytest.raises(SenderDistrusted):
            await reopened.decrypt(next(iter(packets)))
        assert db.path.stat().st_mode & 0o077 == 0
    finally:
        await alice.shutdown()
        if reopened:
            await reopened.shutdown()
        else:
            await bob.shutdown()


@pytest.mark.parametrize("first_carbon", [False, True])
async def test_plugin_real_xml_encrypted_ping_and_reply(config, database, tmp_path, monkeypatch, carbon_message, first_carbon):
    pep = Pep()
    bot_manager, _ = await create_manager(pep, tmp_path / "bot-crypto", config.jid)
    parent_manager, parent_db = await create_manager(pep, tmp_path / "parent-crypto", "parent@example.org")
    bot = GooseClient(config, "test-only", database)
    parent = GooseClient(replace(config, jid="parent@example.org"), "test-only", parent_db)
    bot.init_plugins()
    parent.init_plugins()
    try:
        for client, manager in [(bot, bot_manager), (parent, parent_manager)]:
            client.ready.set()
            plugin = client.plugin["xep_0384"]
            monkeypatch.setattr(plugin, "get_session_manager", AsyncMock(return_value=manager))
            async def refresh(jids, force_download=False, manager=manager):
                for jid in jids:
                    await manager.refresh_device_lists(jid.bare)
            monkeypatch.setattr(plugin, "refresh_device_lists", refresh)
        await trust_peer(bot_manager, "parent@example.org")
        await trust_peer(parent_manager, config.jid)
        incoming = parent.make_message(mto=config.jid, mtype="chat", mbody="!ping")
        incoming["from"] = parent.boundjid
        encrypted, errors = await parent.plugin["xep_0384"].encrypt_message(incoming, {JID(config.jid)})
        assert not errors
        assert encrypted["body"] != "!ping"
        # send() отправлял бы в сокет; заменяем именно передачу, не шифрование.
        captured = []
        monkeypatch.setattr(bot, "send", lambda stanza, **kwargs: captured.append(stanza))
        carbon = carbon_message(encrypted)
        deliveries = (carbon, encrypted) if first_carbon else (encrypted, carbon)
        bot.admit(deliveries[0])
        assert bot.queue.qsize() == 1
        await bot.process_message(bot.queue.get_nowait())
        assert len(captured) == 1
        assert captured[0]["body"] != "pong"
        decoded, device = await parent.plugin["xep_0384"].decrypt_message(captured[0])
        assert decoded["body"] == "pong"
        assert device.bare_jid == config.jid
        bot.admit(deliveries[1])
        assert bot.queue.qsize() == 1
        await bot.process_message(bot.queue.get_nowait())
        assert len(captured) == 1
    finally:
        await bot_manager.shutdown()
        await parent_manager.shutdown()
