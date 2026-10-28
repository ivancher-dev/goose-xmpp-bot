"""Локальная диагностика, сервис и явное управление доверием устройств."""

import argparse
import asyncio
from importlib.metadata import version
import logging
import os
from pathlib import Path
import signal
import sqlite3
import sys
from dataclasses import replace

from slixmpp import JID

from . import __version__
from .config import ConfigError, bare_jid, load_config
from .dialogue import read_persona
from .model import ModelError, OpenAICompatible
from .omemo import fingerprint, set_verified_trust
from .storage import Database, data_lock
from .transport import run_sessions


def configure_logging(level):
    # Slixmpp/OMEMO могут писать XML и полные JID даже в предупреждениях.
    # В обычный журнал допускаются только наши сообщения с фиксированными кодами.
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(logging.NullHandler())
    root.setLevel(logging.CRITICAL + 1)
    log = logging.getLogger("goose")
    log.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.propagate = False
    log.setLevel(level)


def doctor(config):
    print(f"Python {sys.version_info.major}.{sys.version_info.minor}; Гусь {__version__}")
    print(f"Slixmpp {version('slixmpp')}; slixmpp-omemo {version('slixmpp-omemo')}")
    print("TOML: корректен; TLS обязателен; OMEMO: legacy, ручное доверие.")
    directory = config.data_dir
    if not directory.is_dir() or not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
        raise ConfigError("Каталог данных не создан или недоступен текущему пользователю.")
    if directory.stat().st_mode & 0o077:
        raise ConfigError("Каталог данных должен иметь права 0700.")
    path = directory / "goose.sqlite3"
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise ConfigError("База данных должна иметь права 0600.")
        # Режим read-only не создаёт ни БД, ни идентичность устройства.
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ConfigError("Проверка целостности SQLite не пройдена.")
            schema = db.execute("PRAGMA user_version").fetchone()[0]
            if schema not in (1, 2, 3, 4):
                raise ConfigError("Неподдерживаемая версия БД.")
            account = db.execute("SELECT value FROM metadata WHERE key='account'").fetchone()
            if account is None or account[0] != config.jid:
                raise ConfigError("База данных не соответствует аккаунту конфигурации.")
        print(f"SQLite: схема {schema}, целостность и привязка аккаунта проверены.")
    else:
        print("SQLite: ещё не создана; идентичность создаст первый запуск сервиса.")
    print("Модель: " + ("включена; auto восстанавливается после проверки архива" if config.llm.enabled else "отключена"))
    print("Ручная отправка включает manual; неполный архив требует подтверждения auto владельцем.")
    print("Погода: " + ("включена" if config.weather.enabled else "отключена") +
          "; новости: " + ("включены" if config.news.enabled else "отключены") +
          ". Локальная проверка не обращается к источникам.")
    if config.weather.enabled:
        print("Источник погоды: " + config.weather.provider)
    if config.llm.enabled:
        read_persona(config.llm.persona_file)
        print("Образ Гуся: файл корректен; ключ API: " +
              ("переменная задана" if os.environ.get(config.llm.api_key_env) else "не передан текущему процессу"))
    print("Запросов к модели в локальной проверке нет. Расписание не реализовано.")
    print("Пароль XMPP: " + ("переменная задана" if os.environ.get(config.password_env)
                            else "не передан текущему процессу; нужен для run/--xmpp"))
    print("Локальная диагностика завершена успешно. Эта проверка не подключается к XMPP.")


async def execute(args, config):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    # Безопасный обработчик фоновых исключений: без XML и значений исключений.
    loop.set_exception_handler(lambda _loop, _context: logging.getLogger("goose").error("background_task_failed"))
    if args.command == "doctor":
        await asyncio.to_thread(doctor, config)
        if args.xmpp:
            async def report(client):
                print("XMPP: авторизация, TLS и discovery выполнены; "
                      f"Carbons={client.features['carbons']}, MAM={client.features['mam']}.")
                print("Диагностический resource отдельный; OMEMO-плагин не загружался.")
            await run_sessions(config, None, stop, report, probe=True)
        if args.api:
            if not config.llm.enabled:
                raise ConfigError("Для явной API-проверки заполните llm и включите llm.enabled.")
            print("Явная API-проверка: один запрос может тарифицироваться провайдером.", flush=True)
            model = OpenAICompatible(replace(config.llm, max_output_tokens=16, max_attempts=1))
            try:
                await model.complete([{"role": "user", "content": "Ответь одним словом: Га."}])
                print("API: получен корректный текстовый ответ; содержимое не выводится.")
            except ModelError:
                raise ConfigError("API-проверка не прошла; проверьте модель, ключ, адрес и лимиты.") from None
            finally:
                await model.close()
        return
    config.password()
    if args.command == "run" and config.llm.enabled:
        config.llm.key()
        await asyncio.to_thread(read_persona, config.llm.persona_file)
    with data_lock(config.data_dir):
        db = Database(config.data_dir, config.jid)
        await db.initialize(config.users)
        if args.command == "run":
            await run_sessions(config, db, stop)
            return
        target = bare_jid(args.jid, allow_resource=False) if args.jid else config.jid
        if target not in config.allowed and target != config.jid:
            raise ConfigError("Управление устройствами разрешено только для allowlist и аккаунта бота.")
        async def manage(client):
            plugin = client.plugin["xep_0384"]
            await plugin.refresh_device_lists({JID(target)}, force_download=True)
            manager = client.manager
            if args.command == "trust":
                await set_verified_trust(manager, target, args.device_id, args.fingerprint, args.distrust)
                print("Решение доверия сохранено; перезапуск сохранит его.")
            else:
                devices = await manager.get_device_information(target)
                for device in sorted(devices, key=lambda d: d.device_id):
                    print(f"device_id={device.device_id} trust={device.trust_level_name} "
                          f"fingerprint={fingerprint(manager, device)}")
                if not devices:
                    print("Доступных устройств нет; проверьте PEP и поддержку legacy OMEMO.")
        await run_sessions(config, db, stop, manage)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Гусь: совместная переписка и восстановление, этап 3")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "doctor", "devices", "trust"):
        command = sub.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name == "doctor":
            command.add_argument("--xmpp", action="store_true", help="Явная сетевая проверка тестового аккаунта")
            command.add_argument("--api", action="store_true", help="Явный запрос к модели, может тарифицироваться")
        if name in {"devices", "trust"}:
            command.add_argument("--jid", required=name == "trust")
        if name == "trust":
            command.add_argument("--device-id", type=int, required=True)
            command.add_argument("--fingerprint", required=True)
            command.add_argument("--distrust", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    configure_logging("INFO")
    try:
        config = load_config(args.config)
        configure_logging(config.log_level)
        asyncio.run(execute(args, config))
        return 0
    except ConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except PermissionError:
        print("Ошибка: недостаточно прав для доступа к конфигурации или рабочим каталогам. "
              "Запустите команду из доступного каталога и проверьте права пользователя сервиса.",
              file=sys.stderr)
        return 2
    except Exception:
        print("Ошибка выполнения; проверьте права, зависимости, SQLite и доступность XMPP.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
