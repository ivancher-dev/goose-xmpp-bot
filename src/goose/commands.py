"""Детерминированные команды: права проверяются до обращения к модели."""

from .config import ConfigError, bare_jid


HELP = ("Диагностические команды: !help, !ping. Очистка своей памяти: !context clear. "
        "На свободные сообщения Гусь отвечает только в auto при включённой модели. "
        "Владельцу: !admin status, !admin user show <bare_jid>, "
        "!admin user set <bare_jid> grade=N|academic_year=YYYY-YYYY|schedule_class_id=ID. "
        "Режим диалога: !admin mode <bare_jid> [manual|auto]. "
        "Ручная отправка с другого устройства Гуся включает manual этого диалога. "
        "После подключения auto восстанавливается только при успешной проверке архива; "
        "при неполном архиве подтвердите auto явно. "
        "Погода: !weather [город], !weather tomorrow [город]; свой город: !weather city <город>. "
        "Новости: !news [источник]. Эти команды работают и в manual, если включены владельцем. "
        "!echo <текст> доступен только владельцу при echo_enabled=true.")


class Commands:
    def __init__(self, config, database, status=lambda: "Этап 3; модель отключена.", dialogues=None):
        self.config, self.database, self.status = config, database, status
        self.dialogues = dialogues
        self.facts = None

    async def handle(self, sender, body):
        sender = bare_jid(sender)
        if sender not in self.config.allowed or len(body) > 2048:
            return None
        words = body.split()
        if not words or not words[0].startswith("!"):
            return None
        if words == ["!help"]:
            return HELP
        if words == ["!ping"]:
            return "Га-Га-Га!"
        if words[0] == "!context":
            if words != ["!context", "clear"]:
                return "Использование: !context clear"
            if self.facts:
                self.facts.cancel(sender)
            if self.dialogues:
                await self.dialogues.clear(sender)
            else:
                await self.database.clear_context(sender)
            return ("Контекст этого диалога у Гуся очищен. Профиль сохранён. "
                    "История на XMPP-сервере, устройствах, у провайдера и в резервных копиях остаётся.")
        if words[0] == "!echo":
            if sender not in self.config.admins or not self.config.echo_enabled:
                return "Эхо недоступно."
            return body.split(maxsplit=1)[1] if len(words) > 1 else "Использование: !echo <текст>"
        if words[0] != "!admin":
            return "Неизвестная команда. Используйте !help."
        if sender not in self.config.admins:
            return "Команда доступна только владельцу."
        if words == ["!admin", "status"]:
            return self.status()
        try:
            if len(words) in (3, 4) and words[1] == "mode":
                target = bare_jid(words[2], allow_resource=False)
                if target not in self.config.allowed:
                    raise ConfigError("Диалог вне списка разрешённых аккаунтов.")
                if self.dialogues is None:
                    raise ConfigError("Управление режимами доступно в запущенном сервисе.")
                if len(words) == 4:
                    if words[3] == "manual" and self.facts:
                        self.facts.cancel(target)
                    await self.dialogues.set_mode(target, words[3])
                return "Режим: " + await self.dialogues.mode(target)
            if len(words) not in (4, 5) or words[1] != "user" or words[2] not in {"show", "set"}:
                raise ConfigError("Некорректный синтаксис команды профиля.")
            target = bare_jid(words[3], allow_resource=False)
            if target not in self.config.allowed:
                raise ConfigError("Профиль не найден.")
            if words[2] == "show" and len(words) == 4:
                profile = await self.database.profile(target)
                if profile is None:
                    raise ConfigError("Профиль не найден.")
                return "\n".join(f"{key}={profile[key]}" for key in ("grade", "academic_year", "schedule_class_id", "explanation_style"))
            if words[2] != "set" or len(words) != 5 or "=" not in words[4]:
                raise ConfigError("Некорректный синтаксис команды профиля.")
            field, value = words[4].split("=", 1)
            if field == "grade":
                if not value.isascii() or not value.isdecimal():
                    raise ConfigError("grade: требуется целое число от 1 до 11.")
                value = int(value)
            if self.dialogues:
                await self.dialogues.update_profile(target, field, value)
            else:
                await self.database.update_profile(target, field, value)
            return "Профиль обновлён."
        except ConfigError as exc:
            return str(exc)
