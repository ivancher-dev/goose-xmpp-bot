"""Строгая конфигурация без вывода пользовательских значений в ошибках."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from slixmpp import JID


class ConfigError(ValueError):
    pass


def bare_jid(value: str, *, allow_resource: bool = True) -> str:
    try:
        jid = JID(value)
        if (not jid.user or not jid.domain or "@" not in value
                or any(c.isspace() for c in value)
                or (jid.resource and not allow_resource)):
            raise ValueError
        return jid.bare
    except Exception as exc:
        raise ConfigError("Некорректный JID; требуется user@domain.") from exc


def profile_value(field: str, value):
    if field == "grade":
        if type(value) is not int or not 1 <= value <= 11:
            raise ConfigError("grade: требуется целое число от 1 до 11.")
    elif field == "academic_year":
        if (not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{4}", value)
                or int(value[5:]) != int(value[:4]) + 1):
            raise ConfigError("academic_year: требуется последовательный YYYY-YYYY.")
    elif field in {"schedule_class_id", "explanation_style"}:
        if (not isinstance(value, str) or not value.strip() or len(value) > 128
                or any(ord(c) < 32 for c in value)):
            raise ConfigError(f"{field}: требуется непустая строка до 128 символов.")
    else:
        raise ConfigError("Неизвестное поле профиля.")
    return value


def table(data, name, allowed, required=()):
    value = data.get(name, {})
    if not isinstance(value, dict) or value.keys() - set(allowed):
        raise ConfigError(f"{name}: неверная таблица или неизвестные поля.")
    if set(required) - value.keys():
        raise ConfigError(f"{name}: отсутствуют обязательные поля.")
    return value


def typed(data, name, kind, default=None):
    value = data.get(name, default)
    if type(value) is not kind:
        raise ConfigError(f"{name}: неверный тип.")
    return value


def text(data, name, default=None):
    value = typed(data, name, str, default)
    if not value.strip() or any(ord(c) < 32 for c in value):
        raise ConfigError(f"{name}: требуется непустая строка.")
    return value


@dataclass(frozen=True)
class Config:
    jid: str
    resource: str
    password_env: str
    data_dir: Path
    timezone: str
    admins: frozenset[str]
    allowed: frozenset[str]
    users: dict[str, dict]
    echo_enabled: bool
    log_level: str
    max_attempts: int
    connect_timeout: int

    def password(self) -> str:
        value = os.environ.get(self.password_env, "")
        if not value:
            raise ConfigError("Пароль XMPP не задан в настроенной переменной окружения.")
        return value


def load_config(path: Path, *, source_root: Path | None = None) -> Config:
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("Не удалось прочитать TOML-конфигурацию.") from exc
    allowed_top = {"config_version", "timezone", "data_dir", "xmpp", "access", "runtime",
                   "diagnostics", "users", "llm", "memory", "schedule"}
    if data.keys() - allowed_top or type(data.get("config_version")) is not int or data["config_version"] != 1:
        raise ConfigError("Неизвестные поля или неподдерживаемая config_version.")
    timezone = text(data, "timezone")
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ConfigError("timezone: неизвестный часовой пояс IANA.") from exc
    data_dir = Path(text(data, "data_dir"))
    if not data_dir.is_absolute():
        raise ConfigError("data_dir: требуется абсолютный путь вне исходников.")
    data_dir = data_dir.resolve()
    roots = [Path(__file__).resolve().parents[2]]
    if source_root is not None:
        roots.append(source_root.resolve())
    # При запуске установленного wheel исходники могут находиться в текущем checkout.
    # runuser может унаследовать cwd внутри /root. Обнаружение checkout —
    # дополнительная проверка, а не условие чтения абсолютного пути конфига.
    try:
        cwd = Path.cwd()
    except OSError:
        cwd = None
    if cwd is not None:
        for candidate in (cwd, *cwd.parents):
            try:
                marker = candidate / ".git"
                # Пустые .git-каталоги могут быть защитными заглушками среды.
                # HEAD отличает каталог Git, а .git-файл поддерживает linked checkout.
                if marker.is_file() or (marker.is_dir() and (marker / "HEAD").is_file()):
                    roots.append(candidate)
            except OSError:
                # Для недоступного cwd нельзя надёжно определить enclosing checkout.
                break
    if any(data_dir.is_relative_to(root) for root in roots):
        raise ConfigError("data_dir должен находиться вне исходников и virtualenv.")
    xmpp = table(data, "xmpp", {"jid", "resource", "password_env", "require_tls", "omemo"},
                 {"jid", "resource", "password_env"})
    jid = bare_jid(text(xmpp, "jid"), allow_resource=False)
    resource = text(xmpp, "resource")
    try:
        if JID(f"{jid}/{resource}").resource != resource:
            raise ValueError
    except Exception as exc:
        raise ConfigError("resource: некорректное значение.") from exc
    password_env = text(xmpp, "password_env")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", password_env):
        raise ConfigError("password_env: некорректное имя переменной.")
    if typed(xmpp, "require_tls", bool, True) is not True or xmpp.get("omemo", "required") != "required":
        raise ConfigError("TLS и OMEMO обязательны; отключение не поддерживается.")
    access = table(data, "access", {"admin_jids", "allowed_jids"}, {"admin_jids", "allowed_jids"})
    def jids(name):
        values = typed(access, name, list)
        if not values or any(not isinstance(v, str) for v in values):
            raise ConfigError(f"{name}: требуется непустой список JID.")
        return frozenset(bare_jid(v, allow_resource=False) for v in values)
    admins, allowed = jids("admin_jids"), jids("allowed_jids")
    if not admins <= allowed or jid in allowed:
        raise ConfigError("Администраторы должны входить в allowlist; аккаунт бота — исключён.")
    runtime = table(data, "runtime", {"default_mode", "log_level", "max_attempts", "connect_timeout"})
    if runtime.get("default_mode", "manual") != "manual":
        raise ConfigError("На этапе 1 поддерживается только manual.")
    level = text(runtime, "log_level", "INFO")
    if level not in {"INFO", "WARNING", "ERROR"}:
        raise ConfigError("log_level: допустимы INFO, WARNING, ERROR; DEBUG раскрывает XML.")
    attempts = typed(runtime, "max_attempts", int, 3)
    timeout = typed(runtime, "connect_timeout", int, 45)
    if not 1 <= attempts <= 10 or not 5 <= timeout <= 120:
        raise ConfigError("max_attempts: 1..10, connect_timeout: 5..120 секунд.")
    diagnostics = table(data, "diagnostics", {"echo_enabled"})
    echo = typed(diagnostics, "echo_enabled", bool, False)
    users = table(data, "users", data.get("users", {}).keys() if isinstance(data.get("users", {}), dict) else [])
    profiles = {}
    for key, value in users.items():
        normalized = bare_jid(key, allow_resource=False)
        if normalized not in allowed or normalized in profiles:
            raise ConfigError("users: профиль вне allowlist или повтор нормализованного JID.")
        fields = {"grade", "academic_year", "schedule_class_id", "explanation_style"}
        if not isinstance(value, dict) or value.keys() != fields:
            raise ConfigError("users: требуется полный профиль с известными полями.")
        profiles[normalized] = {f: profile_value(f, v) for f, v in value.items()}
    # Поля следующих этапов проверяются, но активация функций запрещена.
    llm = table(data, "llm", {"enabled", "provider", "base_url", "model", "api_key_env",
                              "timeout_seconds", "max_output_tokens", "max_concurrent_requests"})
    schedule = table(data, "schedule", {"enabled", "source_url", "timeout_seconds"})
    memory = table(data, "memory", {"recent_messages", "context_token_budget", "retention_days"})
    for section in (llm, schedule):
        if typed(section, "enabled", bool, False):
            raise ConfigError("Модель и расписание не реализованы на этапе 1.")
    for section in (llm, schedule, memory):
        for key, value in section.items():
            if key == "enabled":
                continue
            if key in {"provider", "base_url", "model", "api_key_env", "source_url"}:
                if type(value) is not str:
                    raise ConfigError(f"{key}: требуется строка.")
            elif type(value) is not int or value <= 0:
                raise ConfigError(f"{key}: требуется положительное целое число.")
    return Config(jid, resource, password_env, data_dir, timezone, admins, allowed,
                  profiles, echo, level, attempts, timeout)
