"""Адаптация публичного API slixmpp-omemo 2.2.0; без слепого доверия."""

import asyncio
from contextlib import suppress
import hmac
import sys

from slixmpp.plugins import register_plugin
from slixmpp_omemo import TrustLevel, XEP_0384

from .config import ConfigError

LEGACY_NS = "eu.siacs.conversations.axolotl"


class ManualTrustRequired(Exception):
    pass


class GooseOmemo(XEP_0384):
    # Имя плагина сохраняется для разрешения зависимостей Slixmpp.
    name = "xep_0384"
    default_config = {**XEP_0384.default_config, "store": None}

    def __init__(self, *args, **kwargs):
        self.initialization_task = None
        self.initialized_manager = None
        super().__init__(*args, **kwargs)

    def session_bind(self, jid):
        # Храним задачу, чтобы отменить и дождаться её при завершении клиента.
        async def initialize():
            try:
                await self.get_session_manager()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.xmpp.event("goose_omemo_failed")
        self.initialization_task = asyncio.create_task(initialize())

    async def get_session_manager(self):
        self.initialized_manager = await super().get_session_manager()
        return self.initialized_manager

    async def aclose(self):
        if self.initialization_task and not self.initialization_task.done():
            self.initialization_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.initialization_task
        if self.initialized_manager is not None:
            await self.initialized_manager.shutdown()
        else:
            # Отменяет внутреннюю задачу create(), если инициализация не завершилась.
            super().plugin_end()
            await asyncio.sleep(0)

    @property
    def storage(self):
        return self.store

    @property
    def _btbv_enabled(self):
        return False

    async def _prompt_manual_trust(self, manually_trusted, identifier):
        # Решения принимаются отдельно через локальную CLI с проверкой отпечатка.
        raise ManualTrustRequired("Требуется проверка устройств владельцем через goose-bot devices/trust.")


def register(xmpp, storage):
    register_plugin(GooseOmemo)
    xmpp.register_plugin("xep_0384", {"store": storage}, module=sys.modules[__name__])


def fingerprint(manager, device):
    return "".join(manager.format_identity_key(device.identity_key)).replace(" ", "").lower()


async def set_verified_trust(manager, jid, device_id, expected, distrust=False):
    devices = await manager.get_device_information(jid)
    selected = next((d for d in devices if d.device_id == device_id), None)
    expected = expected.replace(" ", "").lower()
    if selected is None or not hmac.compare_digest(fingerprint(manager, selected), expected):
        raise ConfigError("Устройство не найдено или отпечаток не совпадает; доверие не изменено.")
    await manager.set_trust(jid, selected.identity_key,
                            TrustLevel.DISTRUSTED.value if distrust else TrustLevel.TRUSTED.value)
