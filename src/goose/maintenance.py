#!/usr/bin/env python3.12
"""Обновлятор без зависимостей: работает и при переключении своего virtualenv.

Не подключается к XMPP/API, не читает EnvironmentFile как код и никогда
не возвращает рабочую OMEMO-базу к старому состоянию.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import uuid

DEFAULT_PREFIX = "/opt/goose-xmpp-bot"
UNIT = "goose-xmpp-bot.service"


class UpdateError(Exception):
    pass


def run(args, *, cwd=None, timeout=120):
    """Вывод pip/git не печатаем: URL и сообщения хуков могут содержать секреты."""
    try:
        result = subprocess.run([str(a) for a in args], cwd=cwd, timeout=timeout,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError("Не удалось завершить внешнюю команду; активная версия сохраняется.") from exc
    if result.returncode:
        raise UpdateError(f"Команда {Path(args[0]).name} завершилась с кодом {result.returncode}.")
    return result.stdout.decode("utf-8", errors="replace").strip()


def sync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, value, mode=0o600):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".update-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=True, sort_keys=True).encode())


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def plain_file(path):
    if path.is_symlink() or not path.is_file():
        raise UpdateError("Нужен обычный файл без символической ссылки.")


@contextmanager
def locked(path, *, owner=None):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if owner is not None and os.geteuid() == 0:
            os.fchown(fd, *owner)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("Обновление или хранилище уже занято другим процессом.") from exc
        yield
    finally:
        os.close(fd)


def schema(directory):
    path = directory / "goose.sqlite3"
    if not path.exists():
        return 0
    plain_file(path)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise UpdateError("SQLite не прошла проверку целостности.")
        return db.execute("PRAGMA user_version").fetchone()[0]


def settings(prefix):
    path = prefix / "maintenance.json"
    plain_file(path)
    if path.stat().st_mode & 0o077 or (os.geteuid() == 0 and path.stat().st_uid != 0):
        raise UpdateError("Настройки обновления должны принадлежать root и иметь права 0600.")
    value = json.loads(path.read_text())
    if value["format"] != 1 or Path(value["prefix"]) != prefix:
        raise UpdateError("Неподдерживаемые настройки обновления.")
    paths = {k: Path(value[k]) for k in ("prefix", "repo", "config", "data", "env", "backups", "unit")}
    if any(not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(p)) or ".." in p.parts for p in paths.values()):
        raise UpdateError("Нужны абсолютные пути без специальных символов.")
    for p in paths.values():
        if p.is_symlink() or p.resolve() != p:
            raise UpdateError("Пути обслуживания не должны проходить через символические ссылки.")
    roots = [paths[k] for k in ("prefix", "repo", "data", "backups")]
    for index, a in enumerate(roots):
        if a == Path("/") or any(a.is_relative_to(b) or b.is_relative_to(a) for b in roots[index + 1:]):
            raise UpdateError("Каталоги кода, checkout, данных и копий должны быть разделены.")
    for key in ("config", "env"):
        plain_file(paths[key])
        if any(paths[key].is_relative_to(root) for root in roots):
            raise UpdateError("Настройки должны находиться вне каталогов кода, данных и копий.")
    config = tomllib.loads(paths["config"].read_text())
    if Path(config["data_dir"]) != paths["data"]:
        raise UpdateError("data_dir не совпадает с настройками обновления.")
    persona = Path(config.get("llm", {}).get("persona_file", paths["config"].parent / "persona.md"))
    if persona.exists():
        plain_file(persona)
        if not persona.is_absolute() or any(persona.resolve().is_relative_to(root) for root in roots):
            raise UpdateError("Образ должен находиться вне кода, данных и копий.")
    paths["persona"] = persona
    if paths["data"].stat().st_mode & 0o077:
        raise UpdateError("Каталог данных должен иметь права 0700.")
    return value, paths


def backup(paths, commit):
    """Вызывается только под data lock после остановки сервиса."""
    root = paths["backups"]
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if root.stat().st_mode & 0o077:
        raise UpdateError("Каталог резервных копий должен иметь права 0700.")
    directory = Path(tempfile.mkdtemp(prefix="update-", dir=root))
    data = paths["data"]
    # Не следуем ссылкам к произвольным файлам вне рабочего хранилища.
    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in data.rglob("*")):
        raise UpdateError("В каталоге данных есть ссылки или специальные файлы; копирование остановлено.")
    shutil.copytree(data, directory / "data", ignore=shutil.ignore_patterns(".lock"))
    for source in (p for p in data.rglob("*") if p.is_file() and p.name != ".lock"):
        if digest(source) != digest(directory / "data" / source.relative_to(data)):
            raise UpdateError("Содержимое копии хранилища не совпало с оригиналом.")
    for name in ("config", "env", "persona", "unit"):
        if paths[name].exists():
            plain_file(paths[name])
            shutil.copy2(paths[name], directory / name)
            if digest(paths[name]) != digest(directory / name):
                raise UpdateError("Копия настроек не совпала с оригиналом.")
    manifest = {"format": 1, "target": commit, "schema": schema(directory / "data"),
                "created_utc": datetime.now(timezone.utc).isoformat(), "sha256": {}}
    for file in sorted(p for p in directory.rglob("*") if p.is_file()):
        file.chmod(0o600)
        manifest["sha256"][str(file.relative_to(directory))] = digest(file)
        with file.open("rb") as stream:
            os.fsync(stream.fileno())
    write_json(directory / "manifest.json", manifest)
    for name, expected in manifest["sha256"].items():
        if digest(directory / name) != expected:
            raise UpdateError("Контрольная сумма резервной копии не совпала.")
    sync_dir(directory)
    sync_dir(root)
    return directory


# Скрипты запускаются в проверяемом окружении; ни один не открывает сеть.
INSPECT = """
import importlib.metadata as m, json
print(json.dumps({d.metadata['Name'].lower().replace('_','-'): d.version
                 for d in m.distributions() if d.metadata['Name'].lower().replace('_','-')
                 not in {'goose-xmpp-bot','pip','setuptools'}}))
"""
MIGRATE = """
import asyncio, sys
from pathlib import Path
from dataclasses import replace
from goose.config import load_config
from goose.storage import Database
from goose.cli import doctor
c = replace(load_config(Path(sys.argv[1])), data_dir=Path(sys.argv[2]))
asyncio.run(Database(c.data_dir, c.jid).initialize(c.users))
doctor(c)
"""


def inspect(venv, runner=run):
    return json.loads(runner([venv / "bin/python", "-c", INSPECT]))


def readable_release(release):
    """Как chmod a+rX: сохраняем исполняемость скриптов для пользователя службы."""
    for path in release.rglob('*'):
        if not path.is_symlink():
            mode = path.stat().st_mode
            path.chmod(mode | (0o555 if path.is_dir() or mode & 0o111 else 0o444))


def doctor(venv, paths, *, local, runner=run):
    args = [venv / "bin/python", "-m", "goose.cli", "doctor", "--config", paths["config"]]
    if not local:
        args = ["runuser", "-u", "goose-xmpp-bot", "--", *args]
    runner(args, cwd=paths["prefix"])


class System:
    def __init__(self, local, runner=run):
        self.local, self.run = local, runner

    def status(self):
        if self.local:
            return {"ActiveState": "inactive", "InvocationID": "", "MainPID": "0"}
        output = self.run(["systemctl", "show", UNIT,
                           "--property=Type,ActiveState,SubState,MainPID,InvocationID,UnitFileState"])
        return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)

    def stop(self):
        if not self.local:
            self.run(["systemctl", "stop", UNIT], timeout=45)

    def reload(self):
        if not self.local:
            self.run(["systemctl", "daemon-reload"])

    def start(self, previous, timeout, *, notify=True):
        if self.local:
            return
        self.run(["systemctl", "reset-failed", UNIT])
        self.run(["systemctl", "start", "--no-block", UNIT])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.status()
            if (state.get("ActiveState") == "active" and state.get("SubState") == "running"
                    and state.get("MainPID", "0") != "0" and state.get("InvocationID")
                    and state["InvocationID"] != previous
                    and (not notify or state.get("Type") == "notify")):
                return
            if state.get("ActiveState") == "failed":
                break
            time.sleep(1)
        raise UpdateError("Новый процесс не подтвердил готовность XMPP за отведённое время.")

    def enable(self):
        if not self.local:
            self.run(["systemctl", "enable", UNIT])

    def restore_enabled(self, previous):
        if not self.local and previous == 'disabled':
            self.run(["systemctl", "disable", UNIT])


def render_unit(source, paths):
    content = (source / "systemd/goose-xmpp-bot.service.in").read_text()
    if "Type=notify\n" not in content or not (source / "src/goose/lifecycle.py").is_file():
        raise UpdateError("Эта версия не поддерживает безопасный обновлятор; нужна 0.3.3 или новее.")
    for key, path in {"PREFIX": "prefix", "CONFIG": "config", "DATA_DIR": "data", "ENV_FILE": "env"}.items():
        content = content.replace(f"@{key}@", str(paths[path]))
    return content.encode()


def install_launcher(source, paths, local):
    target = paths["prefix"] / "bin/goose-update" if local else Path("/usr/local/sbin/goose-update")
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    code = (source / "src/goose/maintenance.py").read_text()
    code = code.replace('DEFAULT_PREFIX = "/opt/goose-xmpp-bot"', f'DEFAULT_PREFIX = {str(paths["prefix"])!r}', 1)
    compile(code, str(target), 'exec')
    atomic_write(target, code.encode(), 0o755)


def bootstrap(repo, prefix, config, data, env, local):
    """Однократная регистрация существующей установки установщиком."""
    metadata = prefix / 'maintenance.json'
    if metadata.exists() or metadata.is_symlink():
        raise UpdateError('Обновлятор уже настроен; используйте goose-update.')
    if not (prefix / 'venv/bin/python').is_file():
        raise UpdateError('Сначала нужна существующая установка Гуся.')
    value = {"format": 1, "repo": str(repo), "prefix": str(prefix), "config": str(config),
             "data": str(data), "env": str(env), "local": local,
             "unit": str(prefix / UNIT if local else Path("/etc/systemd/system") / UNIT),
             "backups": str(prefix.parent / (prefix.name + "-backups") if local
                            else Path("/var/backups/goose-xmpp-bot")),
             "commit": run(["git", "-C", repo, "rev-parse", "HEAD"])}
    write_json(metadata, value)
    try:
        _, paths = settings(prefix)
        plain_file(paths['unit'])
        install_launcher(repo, paths, local)
    except BaseException:
        metadata.unlink()
        raise


class Updater:
    def __init__(self, prefix, *, runner=run, controller=None):
        self.meta, self.paths = settings(prefix)
        self.run = runner
        self.system = controller or System(self.meta["local"], runner)
        self.journal = prefix / "update-state.json"
        self.active = prefix / "venv"

    def save(self, state):
        write_json(self.journal, state)

    def clean_repo(self):
        repo = self.paths["repo"]
        if self.run(["git", "-C", repo, "status", "--porcelain", "--untracked-files=normal"]):
            raise UpdateError("Checkout содержит изменения; сохраните их перед обновлением.")
        return self.run(["git", "-C", repo, "rev-parse", "HEAD"])

    def input_hashes(self):
        return {name: digest(self.paths[name]) for name in ("config", "env", "persona", "unit")
                if self.paths[name].exists()}

    def stage(self, ref):
        repo, prefix = self.paths["repo"], self.paths["prefix"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", ref) or ".." in ref or ref.startswith("-"):
            raise UpdateError("Некорректный ref; используйте имя ветки, тега или SHA.")
        print("Загружаю выбранную версию и фиксирую коммит.", flush=True)
        self.run(["git", "-C", repo, "fetch", "--no-tags", "origin", ref], timeout=300)
        commit = self.run(["git", "-C", repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}"])
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise UpdateError("Не удалось зафиксировать коммит.")
        releases = prefix / "releases"
        releases.mkdir(mode=0o755, exist_ok=True)
        if releases.is_symlink():
            raise UpdateError("Каталог версий не должен быть ссылкой.")
        release = releases / (commit[:12] + "-" + uuid.uuid4().hex[:8])
        release.mkdir(mode=0o755)
        write_json(release / ".goose-release.json", {"commit": commit, "managed": 1})
        source, venv = release / "source", release / "venv"
        source.mkdir(mode=0o755)
        try:
            archive = release / "source.tar"
            self.run(["git", "-C", repo, "archive", "--format=tar", f"--output={archive}", commit])
            with tarfile.open(archive) as tar:
                tar.extractall(source, filter="data")
            archive.unlink()
            unit = render_unit(source, self.paths)
            print("Готовлю отдельное окружение; Гусь продолжает работать.", flush=True)
            self.run([sys.executable, "-m", "venv", venv])
            self.run([venv / "bin/python", "-m", "pip", "install", "--disable-pip-version-check",
                      "--require-hashes", "-r", source / "requirements.lock"], timeout=600)
            self.run([venv / "bin/python", "-m", "pip", "install", "--disable-pip-version-check",
                      "--no-build-isolation", "--no-deps", source], timeout=300)
            self.run([venv / "bin/python", "-m", "pip", "check"])
            # Сервис читает root-owned код; секретов внутри release нет.
            readable_release(release)
            doctor(venv, self.paths, local=self.meta["local"], runner=self.run)
            candidate_unit = release / UNIT
            atomic_write(candidate_unit, unit, 0o644)
            if not self.meta["local"]:
                self.run(["systemd-analyze", "verify", candidate_unit])
            write_json(release / ".goose-release.json", {"commit": commit, "managed": 1, "complete": True})
            return commit, release
        except BaseException:
            shutil.rmtree(release)
            raise

    def switch(self, target):
        temporary = self.active.with_name(".venv-" + uuid.uuid4().hex)
        try:
            temporary.symlink_to(target)
            os.replace(temporary, self.active)
            sync_dir(self.active.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def recover(self, state, *, timeout=240):
        """Откат кода: рабочие данные никогда не копируются в обратную сторону."""
        self.system.stop()
        old = Path(state["old_env"])
        if not self.active.exists() and old.exists() and not state["old_link"]:
            # Прерывание между переименованием legacy-каталога и symlink.
            old.rename(self.active)
        with locked(self.paths["data"] / ".lock"):
            if schema(self.paths["data"]) != state["old_schema"]:
                raise UpdateError("Схема БД изменилась. Сервис остановлен; автоматический откат запрещён. Копия сохранена.")
            if self.active.resolve() != Path(state["original_env"]):
                if inspect(self.active.resolve(), self.run) != state["old_dependencies"]:
                    raise UpdateError("Зависимости изменились. Сервис остановлен; откат требует отдельной проверки OMEMO.")
            changed_code = state['phase'] in {'switching', 'activated'}
            if changed_code:
                expected = {state['old_unit'].encode(), (Path(state['release']) / UNIT).read_bytes()}
                if self.paths['unit'].read_bytes() not in expected:
                    raise UpdateError('Unit изменён другим процессом. Сервис остановлен; правки сохранены для проверки.')
            doctor(old if old.exists() else self.active, self.paths, local=self.meta["local"], runner=self.run)
            if state["old_link"]:
                self.switch(old)
            elif old.exists():
                self.active.unlink(missing_ok=True)
                old.rename(self.active)
                sync_dir(self.active.parent)
            if changed_code:
                atomic_write(self.paths["unit"], state["old_unit"].encode(), 0o644)
                write_json(self.paths["prefix"] / "maintenance.json", state["old_metadata"])
        self.system.reload()
        self.system.restore_enabled(state.get('was_enabled'))
        if state["was_active"]:
            self.system.start(self.system.status().get("InvocationID", ""), timeout,
                              notify="Type=notify\n" in state["old_unit"])
        self.journal.unlink()
        sync_dir(self.journal.parent)
        self.prune({self.active.resolve()})
        print("Возвращён прежний код. Рабочая БД и ключи не откатывались.", flush=True)

    def prune(self, keep):
        root = self.paths["prefix"] / "releases"
        # Удаляем только каталоги, созданные обновлятором, сохраняя текущий и предыдущий.
        for release in root.iterdir():
            if release.is_symlink() or not release.is_dir() or any(k.is_relative_to(release) for k in keep):
                continue
            marker = release / ".goose-release.json"
            if marker.is_file() and json.loads(marker.read_text()).get("managed") == 1:
                shutil.rmtree(release)

    def update(self, ref="main", *, check=False, recover=False, timeout=240):
        with locked(self.paths["prefix"] / ".update.lock"):
            if self.journal.exists():
                if not recover:
                    raise UpdateError("Найдено прерванное обновление. Выполните goose-update --recover; новая установка не начата.")
                self.recover(json.loads(self.journal.read_text()), timeout=timeout)
                return
            if recover:
                print("Прерванного обновления нет.")
                return
            head = self.clean_repo()
            inputs = self.input_hashes()
            original = self.active.resolve()
            commit, release = self.stage(ref)
            if check:
                print(f"Подготовлена версия {commit[:12]}; сервис и данные не изменены.")
                self.prune({original, release})
                return
            if self.clean_repo() != head or self.input_hashes() != inputs:
                self.prune({original})
                raise UpdateError("Checkout или настройки изменились во время подготовки; активация отменена.")
            self.paths['backups'].mkdir(mode=0o700, parents=True, exist_ok=True)
            size = sum(p.stat().st_size for p in self.paths['data'].rglob('*') if p.is_file())
            if shutil.disk_usage(self.paths['backups']).free < size * 2 + 16 * 1024 * 1024:
                raise UpdateError("Недостаточно места для резервной и пробной копий; сервис не остановлен.")
            status = self.system.status()
            old_link = self.active.is_symlink()
            legacy = self.paths["prefix"] / ("legacy-" + uuid.uuid4().hex)
            state = {"target": commit, "release": str(release), "original_env": str(original),
                     "old_env": str(original if old_link else legacy), "old_link": old_link,
                     "old_schema": schema(self.paths["data"]),
                     "old_dependencies": inspect(original, self.run),
                     "old_unit": self.paths["unit"].read_text(),
                     "old_metadata": self.meta,
                     "was_enabled": status.get('UnitFileState'),
                     "was_active": status.get("ActiveState") in {"active", "activating"}, "phase": "prepared"}
            self.save(state)
            try:
                print("Останавливаю Гуся для согласованной копии и переключения.", flush=True)
                self.system.stop()
                stat = self.paths["data"].stat()
                with locked(self.paths["data"] / ".lock", owner=(stat.st_uid, stat.st_gid)):
                    if self.input_hashes() != inputs:
                        raise UpdateError("Настройки изменились; активация отменена.")
                    state["old_schema"] = schema(self.paths["data"])
                    copy = backup(self.paths, commit)
                    state.update(backup=str(copy), phase="backed_up")
                    self.save(state)
                    print(f"Копия проверена: {copy}", flush=True)
                    with tempfile.TemporaryDirectory(prefix="migration-", dir=self.paths["backups"]) as trial:
                        clone = Path(trial) / "data"
                        shutil.copytree(copy / "data", clone)
                        self.run([release / "venv/bin/python", "-c", MIGRATE, self.paths["config"], clone],
                                 cwd=self.paths["prefix"])
                        schema(clone)
                    if self.input_hashes() != inputs:
                        raise UpdateError('Настройки изменились во время копирования или пробной миграции; активация отменена.')
                    state["phase"] = "switching"
                    self.save(state)
                    if not old_link:
                        self.active.rename(legacy)
                        sync_dir(self.active.parent)
                    self.switch(release / "venv")
                    atomic_write(self.paths["unit"], (release / UNIT).read_bytes(), 0o644)
                    state["phase"] = "activated"
                    self.save(state)
                self.system.reload()
                self.system.start(status.get("InvocationID", ""), timeout)
                self.system.enable()
                # Завершение также повторяемо после сбоя записи метаданных.
                install_launcher(release / "source", self.paths, self.meta["local"])
                write_json(self.paths["prefix"] / "maintenance.json", {**self.meta, "commit": commit})
            except BaseException as exc:
                try:
                    self.recover(state, timeout=timeout)
                except Exception as recovery:
                    raise UpdateError(f"Обновление не завершено. {recovery} Журнал: {self.journal}") from exc
                raise UpdateError("Обновление отменено; прежняя установка восстановлена без отката данных.") from exc
            self.journal.unlink()
            sync_dir(self.journal.parent)
            try:
                if self.clean_repo() == head:
                    self.run(["git", "-C", self.paths["repo"], "switch", "--detach", commit])
            except UpdateError:
                print("Приложение обновлено; checkout оставлен без изменений.")
            self.prune({release, Path(state["old_env"]).parent if old_link else legacy})
            print(f"Версия {commit[:12]} установлена. " + (
                "Локальная проверка завершена; XMPP и systemd не запускались." if self.meta["local"] else
                "XMPP готов; автозапуск включён. Прежний auto вернётся после полного архива; manual сохранён."))
            if not self.meta['local']:
                try:
                    with sqlite3.connect((self.paths['data'] / 'goose.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
                        rows = db.execute('SELECT mode,suspended,count(*) FROM dialogues GROUP BY mode,suspended').fetchall()
                    counts = {(mode, suspended): count for mode, suspended, count in rows}
                    print('Диалоги: auto готово={}; auto на паузе={}; manual={}.'.format(
                        counts.get(('auto', 0), 0), counts.get(('auto', 1), 0),
                        sum(n for (mode, _), n in counts.items() if mode == 'manual')))
                except sqlite3.Error:
                    print('Режимы можно проверить командой !admin mode в чате владельца.')


def main(argv=None):
    parser = argparse.ArgumentParser(description="Обновить только Гуся с проверенной копией и сохранением ключей.")
    parser.add_argument("--prefix", type=Path, default=Path(DEFAULT_PREFIX))
    parser.add_argument("--ref", default="main", help="ветка, тег или SHA из origin; по умолчанию main")
    parser.add_argument("--check", action="store_true", help="только подготовить новую версию")
    parser.add_argument("--recover", action="store_true", help="завершить безопасный откат прерванной операции")
    parser.add_argument("--timeout", type=int, default=240, help="ожидание готовности XMPP, 30–900 секунд")
    args = parser.parse_args(argv)
    try:
        os.umask(0o077)
        if sys.version_info[:2] != (3, 12) or not 30 <= args.timeout <= 900:
            raise UpdateError("Нужен Python 3.12; timeout — от 30 до 900 секунд.")
        updater = Updater(args.prefix)
        if not updater.meta["local"] and os.geteuid() != 0:
            raise UpdateError("Системное обновление выполняется через sudo.")
        updater.update(args.ref, check=args.check, recover=args.recover, timeout=args.timeout)
        return 0
    except (UpdateError, OSError, ValueError, KeyError, sqlite3.Error, tarfile.TarError):
        # Не выводим системные исключения/JSON/config: они могут содержать значения пользователя.
        exc = sys.exception()
        print(f"Ошибка обновления: {exc}" if isinstance(exc, UpdateError) else
              "Ошибка обновления: проверьте файлы и журнал операции; секреты не выводятся.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
