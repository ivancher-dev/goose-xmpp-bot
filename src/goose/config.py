"""Строгая конфигурация без вывода пользовательских значений в ошибках."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from slixmpp import JID


class ConfigError(ValueError):
    pass


class TransientError(ConfigError):
    """Временный отказ: systemd может повторить запуск с паузой."""


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
class LlmConfig:
    enabled: bool = False
    provider: str = "openai_compatible"
    base_url: str = ""
    model: str = ""
    api_key_env: str = "GOOSE_LLM_API_KEY"
    persona_file: Path = Path("/etc/goose-xmpp-bot/persona.md")
    timeout_seconds: int = 30
    max_output_tokens: int = 700
    max_concurrent_requests: int = 2
    requests_per_minute: int = 12
    max_attempts: int = 2

    def key(self):
        value = os.environ.get(self.api_key_env, "")
        if not value:
            raise ConfigError("Ключ API модели не задан в настроенной переменной окружения.")
        return value


@dataclass(frozen=True)
class MemoryConfig:
    recent_messages: int = 24
    context_token_budget: int = 6000
    retention_days: int = 30


@dataclass(frozen=True)
class InteractionConfig:
    typing_enabled: bool = False
    reactions_enabled: bool = False


@dataclass(frozen=True)
class UsageConfig:
    price_model: str = ''
    currency: str = 'RUB'
    input_per_million: Decimal | None = None
    output_per_million: Decimal | None = None


@dataclass(frozen=True)
class WeatherConfig:
    enabled: bool = False
    provider: str = "open_meteo"


@dataclass(frozen=True)
class NewsSource:
    key: str
    name: str
    url: str


@dataclass(frozen=True)
class NewsConfig:
    enabled: bool = False
    sources: tuple[NewsSource, ...] = ()
    limit: int = 5
    max_age_hours: int = 48


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
    llm: LlmConfig = LlmConfig()
    memory: MemoryConfig = MemoryConfig()
    weather: WeatherConfig = WeatherConfig()
    news: NewsConfig = NewsConfig()
    interaction: InteractionConfig = InteractionConfig()
    usage: UsageConfig = UsageConfig()

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
                   "diagnostics", "users", "llm", "memory", "schedule", "weather", "news", "interaction", "usage"}
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
        raise ConfigError("Новый диалог создаётся только в manual; auto включает владелец.")
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
    # Расписание остаётся неактивной настройкой следующего этапа.
    llm = table(data, "llm", {"enabled", "provider", "base_url", "model", "api_key_env",
                              "timeout_seconds", "max_output_tokens", "max_concurrent_requests",
                              "persona_file", "requests_per_minute", "max_attempts"})
    schedule = table(data, "schedule", {"enabled", "source_url", "timeout_seconds"})
    memory = table(data, "memory", {"recent_messages", "context_token_budget", "retention_days"})
    if typed(schedule, "enabled", bool, False):
        raise ConfigError("Расписание пока не реализовано.")
    for section in (llm, schedule, memory):
        for key, value in section.items():
            if key == "enabled":
                continue
            if key in {"provider", "base_url", "model", "api_key_env", "source_url", "persona_file"}:
                if type(value) is not str:
                    raise ConfigError(f"{key}: требуется строка.")
            elif type(value) is not int or value <= 0:
                raise ConfigError(f"{key}: требуется положительное целое число.")
    def limited(section, name, default, maximum, minimum=1):
        value = typed(section, name, int, default)
        if not minimum <= value <= maximum:
            raise ConfigError(f"{name}: значение вне допустимого диапазона.")
        return value

    enabled = typed(llm, "enabled", bool, False)
    provider = text(llm, "provider", "openai_compatible")
    base_url = typed(llm, "base_url", str, "")
    model = typed(llm, "model", str, "")
    api_env = text(llm, "api_key_env", "GOOSE_LLM_API_KEY")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_env):
        raise ConfigError("api_key_env: некорректное имя переменной.")
    persona_file = Path(text(llm, "persona_file", str(path.resolve().parent / "persona.md")))
    if not persona_file.is_absolute():
        raise ConfigError("persona_file: требуется абсолютный путь.")
    if enabled:
        try:
            address = urlsplit(base_url)
            valid_url = (not any(ord(c) <= 32 for c in base_url)
                         and address.scheme == "https" and address.hostname and not address.username
                         and not address.password and not address.query and not address.fragment)
            address.port
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ConfigError("base_url: требуется HTTPS-адрес API без учётных данных и query.")
        if provider != "openai_compatible":
            raise ConfigError("Поддерживается только адаптер openai_compatible.")
        if not model.strip() or len(model) > 128 or any(ord(c) < 32 for c in model):
            raise ConfigError("model: требуется непустой идентификатор модели до 128 символов.")
    llm_config = LlmConfig(enabled, provider, base_url.rstrip("/"), model, api_env, persona_file,
                           limited(llm, "timeout_seconds", 30, 120),
                           limited(llm, "max_output_tokens", 700, 4096),
                           limited(llm, "max_concurrent_requests", 2, 8),
                           limited(llm, "requests_per_minute", 12, 120),
                           limited(llm, "max_attempts", 2, 3))
    memory_config = MemoryConfig(limited(memory, "recent_messages", 24, 100),
                                 limited(memory, "context_token_budget", 6000, 65536, 256),
                                 limited(memory, "retention_days", 30, 365))
    weather = table(data, "weather", {"enabled", "provider"})
    weather_provider = text(weather, "provider", "open_meteo")
    if weather_provider not in {"open_meteo", "wttr"}:
        raise ConfigError("weather.provider: допустимы open_meteo и wttr.")
    weather_config = WeatherConfig(typed(weather, "enabled", bool, False), weather_provider)
    news = table(data, "news", {"enabled", "sources", "limit", "max_age_hours"})
    enabled_news = typed(news, "enabled", bool, False)
    sources = news.get("sources", {})
    if not isinstance(sources, dict) or len(sources) > 8:
        raise ConfigError("news.sources: требуется не более 8 именованных источников.")
    parsed_sources = []
    for key in sources:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", key):
            raise ConfigError("news.sources: некорректный идентификатор источника.")
        source = table(sources, key, {"name", "url"}, {"name", "url"})
        name, url = text(source, "name"), text(source, "url")
        try:
            address = urlsplit(url)
            valid = (address.scheme == "https" and address.hostname
                     and not any(ord(c) <= 32 for c in url) and not address.username
                     and not address.password and not address.query and not address.fragment)
            address.port
        except ValueError:
            valid = False
        if not valid or len(url) > 512 or len(name) > 64:
            raise ConfigError("news.sources: нужны имя до 64 символов и HTTPS URL без секретов/query.")
        parsed_sources.append(NewsSource(key, name, url))
    if enabled_news and not parsed_sources:
        raise ConfigError("news.sources: для новостей задайте хотя бы один источник.")
    news_config = NewsConfig(enabled_news, tuple(parsed_sources),
                             limited(news, "limit", 5, 10),
                             limited(news, "max_age_hours", 48, 168))
    interaction = table(data, 'interaction', {'typing_enabled', 'reactions_enabled'})
    interaction_config = InteractionConfig(typed(interaction, 'typing_enabled', bool, False),
                                           typed(interaction, 'reactions_enabled', bool, False))
    usage = table(data, 'usage', {'price_model', 'currency', 'input_per_million', 'output_per_million'})
    price_model = typed(usage, 'price_model', str, '')
    currency = text(usage, 'currency', 'RUB')
    if not re.fullmatch(r'[A-Z]{3}', currency) or len(price_model) > 128 or any(ord(c) < 32 for c in price_model):
        raise ConfigError('usage: нужны трёхбуквенная валюта и корректный price_model.')
    prices = []
    for field in ('input_per_million', 'output_per_million'):
        value = usage.get(field)
        if value is None:
            prices.append(None)
            continue
        try:
            if type(value) not in (str, int, float) or len(str(value)) > 40:
                raise ValueError
            amount = Decimal(str(value))
            if not amount.is_finite() or not 0 <= amount <= 10**9 or amount.normalize().as_tuple().exponent < -6:
                raise ValueError
        except (ValueError, InvalidOperation):
            raise ConfigError('usage: цена за миллион токенов от 0 до 1000000000, до 6 десятичных знаков.') from None
        prices.append(amount)
    if (prices[0] is None) != (prices[1] is None) or (prices[0] is not None and not price_model.strip()):
        raise ConfigError('usage: задайте обе цены и явный price_model либо оставьте цены незаданными.')
    usage_config = UsageConfig(price_model, currency, *prices)
    return Config(jid, resource, password_env, data_dir, timezone, admins, allowed,
                  profiles, echo, level, attempts, timeout, llm_config, memory_config,
                  weather_config, news_config, interaction_config, usage_config)
