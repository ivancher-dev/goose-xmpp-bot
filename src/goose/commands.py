"""Детерминированные команды этапа 1. Свободный текст не получает ответа."""

from .config import ConfigError, bare_jid


HELP = ("Гусь работает в диагностическом режиме: !help, !ping. "
        "Свободные сообщения не вызывают автоответ. "
        "Владельцу: !admin status, !admin user show <bare_jid>, "
        "!admin user set <bare_jid> grade=N|academic_year=YYYY-YYYY|schedule_class_id=ID. "
        "!echo <текст> доступен только владельцу при echo_enabled=true.")


class Commands:
    def __init__(self, config, database, status=lambda: "Диагностический режим; генерация отключена."):
        self.config, self.database, self.status = config, database, status

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
            return "pong"
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
            await self.database.update_profile(target, field, value)
            return "Профиль обновлён."
        except ConfigError as exc:
            return str(exc)
