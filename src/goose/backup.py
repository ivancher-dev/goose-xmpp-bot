#!/usr/bin/env python3.12
"""Приватные регулярные копии; стандартная библиотека, без XMPP и модели."""

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import tomllib
from types import SimpleNamespace
import uuid

DEFAULT_PREFIX = "/opt/goose-xmpp-bot"
SERVICE = 'goose-xmpp-bot-backup.service'
TIMER = 'goose-xmpp-bot-backup.timer'
DEFAULT_CALENDAR = '*-*-* 04:15:00 Europe/Moscow'
TIMEOUT = 120


class BackupError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def helpers(prefix):
    if __package__:
        from . import maintenance
        return maintenance
    path = prefix / 'backup-maintenance.py'
    # Самостоятельная команда не импортирует меняющийся активный venv.
    if (path.is_symlink() or not path.is_file() or path.stat().st_uid != os.geteuid()
            or path.stat().st_mode & 0o022 or prefix.is_symlink()
            or prefix.stat().st_uid != os.geteuid() or prefix.stat().st_mode & 0o022):
        raise BackupError('Небезопасные права файлов обслуживания.', 2)
    return SimpleNamespace(**runpy.run_path(str(path), run_name='goose_backup_helpers'))


def stamp():
    return datetime.now(timezone.utc).isoformat()


def private_directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (path.is_symlink() or path.resolve() != path or not path.is_dir()
            or path.stat().st_mode & 0o077 or path.stat().st_uid != os.geteuid()):
        raise BackupError('Каталог копий должен принадлежать текущему владельцу и иметь права 0700.', 2)


def file_bytes(path, limit=2 * 1024 * 1024):
    if path.is_symlink() or not path.is_file():
        raise BackupError('В копии или настройках нужен обычный файл без symlink.', 2)
    with path.open('rb') as source:
        value = source.read(limit + 1)
    if len(value) > limit:
        raise BackupError('Файл настроек превышает допустимый размер.', 2)
    return value


def valid_preferences(value):
    if (not isinstance(value, dict) or set(value) != {'format', 'installation', 'calendar', 'keep'}
            or type(value['format']) is not int or value['format'] != 1
            or not isinstance(value['installation'], str) or not re.fullmatch(r'[0-9a-f]{32}', value['installation'])
            or type(value['keep']) is not int or not 2 <= value['keep'] <= 365
            or not isinstance(value['calendar'], str)
            or not re.fullmatch(r'[A-Za-z0-9*/,: ._+~-]{1,120}', value['calendar'])):
        raise BackupError('Неверные настройки копирования: keep 2–365, одно выражение OnCalendar.', 2)
    return value


def sqlite_copy(source, destination, *, timeout=TIMEOUT, clock=time.monotonic):
    """Согласованный снимок committed SQLite, включая работающую WAL-базу."""
    if source.is_symlink() or not source.is_file():
        raise BackupError('Рабочая SQLite ещё не создана или недоступна.', 2)
    deadline = clock() + timeout
    def progress(status, remaining, total):
        if clock() >= deadline:
            raise BackupError('Истекло время ожидания согласованной SQLite-копии.', 75)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=min(0.1, timeout))) as live:
        with closing(sqlite3.connect(destination, timeout=0.1)) as copy:
            live.backup(copy, pages=128, progress=progress, sleep=0.02)
            # Самодостаточный снимок: последующие read-only проверки не создают WAL/SHM.
            if copy.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower() != 'delete':
                raise BackupError('Не удалось завершить самодостаточную SQLite-копию.')
    destination.chmod(0o600)


def source_size(directory):
    allowed = {'goose.sqlite3', 'goose.sqlite3-wal', 'goose.sqlite3-shm', 'goose.sqlite3-journal', '.lock'}
    size = 0
    for path in directory.iterdir():
        try:
            info = path.lstat()
        except FileNotFoundError:
            # Журнал и WAL/SHM могут исчезнуть после commit/checkpoint писателя.
            if path.name in allowed - {'goose.sqlite3', '.lock'}:
                continue
            raise
        if not stat.S_ISREG(info.st_mode) or path.name not in allowed:
            raise BackupError('В данных есть неизвестный файл; полнота копии требует проверки.', 2)
        if path.name != '.lock':
            size += info.st_size
    return size


RESTORE_CHECK = r'''
import asyncio, contextlib, hashlib, io, json, sqlite3, sys
from pathlib import Path
from dataclasses import replace
def no_network(event, args):
    if event in {'socket.connect','socket.getaddrinfo','socket.gethostbyname'}:
        raise RuntimeError('Network forbidden in restore check')
sys.addaudithook(no_network)
from goose.config import load_config
from goose.storage import Database
from goose.cli import doctor
root = Path(sys.argv[1])
c = load_config(root / 'config')
c = replace(c, data_dir=root / 'data', llm=replace(c.llm, persona_file=root / 'persona'))
def keys():
    value = hashlib.sha256()
    with contextlib.closing(sqlite3.connect((root/'data/goose.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        for row in db.execute('SELECT key,value FROM omemo ORDER BY key'):
            value.update(json.dumps(list(row),ensure_ascii=True).encode())
        return value.hexdigest()
before = keys()
with contextlib.redirect_stdout(io.StringIO()):
    doctor(c)
    asyncio.run(Database(c.data_dir,c.jid).initialize(c.users))
    doctor(c)
if before != keys():
    raise RuntimeError('OMEMO changed in restore check')
print(json.dumps({'application_check':True,'omemo_preserved':True}))
'''


class Backups:
    def __init__(self, prefix, *, helper=None, runner=None):
        self.m = helper or helpers(prefix)
        self.meta, self.paths = self.m.settings(prefix)
        self.prefix = prefix
        self.run = runner or self.m.run
        self.preferences_path = prefix / 'backup-settings.json'
        self.root = self.paths['backups'] / 'scheduled'
        self.status_path = self.root / 'status.json'

    @contextmanager
    def lock(self):
        fd = os.open(self.prefix / '.update.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackupError('Обслуживание занято: идёт обновление или другая копия.', 75) from exc
            if (self.prefix / 'update-state.json').exists():
                raise BackupError('Найден журнал прерванного обновления; сначала нужен goose-update --recover.', 2)
            # За время загрузки команды обновление могло успеть завершиться.
            self.meta, self.paths = self.m.settings(self.prefix)
            self.root = self.paths['backups'] / 'scheduled'
            self.status_path = self.root / 'status.json'
            yield
        finally:
            os.close(fd)

    def preferences(self, *, create=False):
        if self.preferences_path.exists():
            if self.preferences_path.stat().st_mode & 0o077:
                raise BackupError('Настройки копирования должны иметь права 0600.', 2)
            return valid_preferences(json.loads(file_bytes(self.preferences_path)))
        value = dict(format=1, installation=uuid.uuid4().hex, calendar=DEFAULT_CALENDAR, keep=14)
        if create:
            self.m.write_json(self.preferences_path, value)
        return value

    def directories(self):
        private_directory(self.paths['backups'], create=True)
        private_directory(self.root, create=True)

    def inputs(self):
        sources = {k: self.paths[k] for k in ('config', 'env', 'persona', 'unit') if self.paths[k].exists()}
        sources['maintenance.json'] = self.prefix / 'maintenance.json'
        sources['backup-settings.json'] = self.preferences_path
        for name in (SERVICE, TIMER):
            path = self.paths['unit'].parent / name
            if path.exists():
                sources[name] = path
        values = {name: file_bytes(path) for name, path in sources.items()}
        config = tomllib.loads(values['config'].decode())
        persona = Path(config.get('llm', {}).get('persona_file', self.paths['config'].parent / 'persona.md'))
        if Path(config['data_dir']) != self.paths['data'] or persona != self.paths['persona']:
            raise BackupError('Настройки изменились при подготовке копии; повторите позже.', 75)
        return sources, values

    def verify(self, directory):
        directory = Path(directory).absolute()
        private_directory(directory)
        manifest_path = directory / 'manifest.json'
        manifest = json.loads(file_bytes(manifest_path))
        if manifest_path.stat().st_mode & 0o077 or manifest_path.stat().st_uid != os.geteuid():
            raise BackupError('Manifest должен принадлежать владельцу копии и иметь права 0600.', 2)
        if (not isinstance(manifest, dict) or type(manifest.get('format')) is not int
                or manifest.get('format') != 1 or not isinstance(manifest.get('sha256'), dict)
                or not 1 <= len(manifest['sha256']) <= 10000
                or type(manifest.get('schema')) is not int):
            raise BackupError('Неизвестный формат manifest.', 2)
        try:
            if not isinstance(manifest.get('created_utc'), str):
                raise ValueError()
            if datetime.fromisoformat(manifest['created_utc']).tzinfo is None:
                raise ValueError()
        except ValueError as exc:
            raise BackupError('Неверное время в manifest.', 2) from exc
        expected = set(manifest['sha256']) | {'manifest.json'}
        required = {'config', 'env', 'data/goose.sqlite3'}
        if not required <= expected:
            raise BackupError('Копия не содержит обязательные файлы.', 2)
        for name, checksum in manifest['sha256'].items():
            if not isinstance(name, str) or not isinstance(checksum, str):
                raise BackupError('Недопустимый путь или checksum в manifest.', 2)
            rel = PurePosixPath(name)
            if (rel.is_absolute() or any(part in {'.', '..'} for part in name.split('/'))
                    or not name or not re.fullmatch(r'[0-9a-f]{64}', checksum)):
                raise BackupError('Недопустимый путь или checksum в manifest.', 2)
            path = directory / name
            if (path.is_symlink() or not path.resolve().is_relative_to(directory)
                    or path.stat().st_mode & 0o077 or path.stat().st_uid != os.geteuid()
                    or self.m.digest(path) != checksum):
                raise BackupError('Права или контрольная сумма копии не совпали.', 2)
        for path in directory.rglob('*'):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise BackupError('В копии есть symlink или специальный файл.', 2)
            if path.is_dir() and path.stat().st_mode & 0o077:
                raise BackupError('Каталоги внутри копии должны иметь права 0700.', 2)
            if path.is_file() and str(path.relative_to(directory)) not in expected:
                raise BackupError('В копии есть файл вне manifest.', 2)
        if self.m.schema(directory / 'data') != manifest['schema']:
            raise BackupError('Схема SQLite не совпала с manifest.', 2)
        return manifest

    def restore_test(self, directory):
        """Полное копирование в scratch; работающий каталог не является целью."""
        directory = Path(directory).absolute()
        manifest = self.verify(directory)
        # На диске backup-root: temp приватен, не удаляется исходная копия.
        self.directories()
        with tempfile.TemporaryDirectory(prefix='.restore-', dir=self.root) as trial:
            restored = Path(trial) / 'restored'
            shutil.copytree(directory, restored)
            self.verify(restored)
            try:
                result = json.loads(self.run([self.prefix / 'venv/bin/python', '-c', RESTORE_CHECK, restored],
                                             cwd=self.prefix, timeout=60))
            except (self.m.UpdateError, ValueError) as exc:
                raise BackupError('Пробное восстановление установленным приложением не пройдено. '
                                  'Проверьте локальный doctor; рабочая база не менялась.') from exc
            if result != {'application_check': True, 'omemo_preserved': True}:
                raise BackupError('Пробное восстановление не подтвердило данные.', 2)
        if self.verify(directory) != manifest:
            raise BackupError('Копия изменилась во время проверки.', 2)
        return result

    def _create(self):
        try:
            return self._build()
        except Exception:
            # Не скрываем отказ за временем прежней успешной копии.
            try:
                private_directory(self.root)
                previous = json.loads(file_bytes(self.status_path)) if self.status_path.exists() else {'format': 1}
                self.m.write_json(self.status_path, {**previous, 'last_attempt': stamp(), 'last_outcome': 'failed'})
            except Exception:
                pass
            raise

    def _build(self):
        preferences = self.preferences(create=True)
        self.directories()
        sources, values = self.inputs()
        size = source_size(self.paths['data'])
        if shutil.disk_usage(self.root).free < size * 2 + sum(map(len, values.values())) * 2 + 16 * 1024 * 1024:
            raise BackupError('Недостаточно места для копии и пробного восстановления.', 75)
        partial = Path(tempfile.mkdtemp(prefix='.partial-', dir=self.root))
        try:
            data = partial / 'data'
            data.mkdir(mode=0o700)
            started = stamp()
            sqlite_copy(self.paths['data'] / 'goose.sqlite3', data / 'goose.sqlite3')
            source_size(self.paths['data'])
            for name, content in values.items():
                self.m.atomic_write(partial / name, content)
            if any(file_bytes(path) != values[name] for name, path in sources.items()):
                raise BackupError('Настройки изменились во время копирования; повторите позже.', 75)
            manifest = dict(format=1, kind='scheduled', installation=preferences['installation'],
                            created_utc=stamp(), started_utc=started, target=self.meta.get('commit'),
                            schema=self.m.schema(data), sha256={})
            for path in sorted(p for p in partial.rglob('*') if p.is_file()):
                path.chmod(0o600)
                manifest['sha256'][str(path.relative_to(partial))] = self.m.digest(path)
                with path.open('rb') as stream:
                    os.fsync(stream.fileno())
            self.m.sync_dir(data)
            self.m.write_json(partial / 'manifest.json', manifest)
            self.restore_test(partial)
            manifest['restore_checked'] = True
            self.m.write_json(partial / 'manifest.json', manifest)
            name = 'daily-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
            destination = self.root / name
            partial.rename(destination)
            self.m.sync_dir(self.root)
            self.m.write_json(self.status_path, dict(format=1, last_success=manifest['created_utc'],
                                                     last_attempt=stamp(), last_outcome='ok',
                                                     snapshot=name, restore_checked=True))
            removed, invalid = self.prune(destination, preferences)
            print(f'Копия проверена, пробное восстановление выполнено: {destination}')
            print(f'Ротация: удалено {removed}; хранить {preferences["keep"]} регулярных копий. '
                  'Копии перед обновлением сохранены.')
            if invalid:
                print('Некоторые старые каталоги не прошли проверку; они сохранены для ручного разбора.')
            return destination
        finally:
            if partial.exists():
                shutil.rmtree(partial)

    def create(self):
        with self.lock():
            return self._create()

    def prune(self, newest, preferences):
        candidates, invalid = [], 0
        for path in self.root.iterdir():
            if not re.fullmatch(r'daily-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}', path.name) or path == newest:
                continue
            try:
                value = self.verify(path)
                if (value.get('kind') != 'scheduled' or value.get('installation') != preferences['installation']
                        or value.get('restore_checked') is not True):
                    continue
                created = datetime.fromisoformat(value['created_utc'])
                if created.tzinfo is None:
                    raise ValueError()
                candidates.append((created, path))
            except (BackupError, OSError, ValueError, KeyError, sqlite3.Error, self.m.UpdateError):
                invalid += 1
        candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        removed = 0
        for _, path in candidates[preferences['keep'] - 1:]:
            shutil.rmtree(path)
            removed += 1
        self.m.sync_dir(self.root)
        return removed, invalid

    def timer_state(self):
        if self.meta['local']:
            return {'ActiveState': 'local', 'UnitFileState': 'local'}
        if not (self.paths['unit'].parent / TIMER).exists():
            return {'ActiveState': 'inactive', 'UnitFileState': 'disabled'}
        output = self.run(['systemctl', 'show', TIMER,
                           '--property=ActiveState,UnitFileState,NextElapseUSecRealtime'])
        return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)

    def install_resources(self):
        if __package__:
            self.m.install_backup_launcher(Path(__file__).parent, self.prefix, self.meta['local'])

    def units(self, preferences):
        self.install_resources()
        outputs = {}
        for name in (SERVICE, TIMER):
            value = (self.prefix / (name + '.in')).read_text()
            replacements = {'PREFIX': self.prefix, 'BACKUPS': self.paths['backups'],
                            'CALENDAR': preferences['calendar']}
            for key, replacement in replacements.items():
                value = value.replace('@' + key + '@', str(replacement))
            outputs[self.paths['unit'].parent / name] = value.encode()
        # Валидация до записи действующих unit.
        if not self.meta['local']:
            self.run(['systemd-analyze', 'calendar', '--iterations=1', preferences['calendar']])
            with tempfile.TemporaryDirectory(prefix='goose-backup-units-') as directory:
                files = []
                for path, content in outputs.items():
                    temporary = Path(directory) / path.name
                    temporary.write_bytes(content)
                    files.append(temporary)
                self.run(['systemd-analyze', 'verify', *files])
        return outputs

    def enable(self):
        with self.lock():
            preferences = self.preferences(create=True)
            outputs = self.units(preferences)
            self._create()  # Включение только после настоящей успешной копии и restore-test.
            self.apply_units(outputs, preferences, enable=True)
            print('Регулярные копии настроены: ' + preferences['calendar'] +
                  f'; хранить {preferences["keep"]} копий.' + (' Локальная проверка, systemd не запускался.'
                                                            if self.meta['local'] else ' Таймер включён.'))

    def configure(self, calendar=None, keep=None):
        with self.lock():
            old = self.preferences(create=True)
            new = valid_preferences({**old, 'calendar': calendar if calendar is not None else old['calendar'],
                                     'keep': keep if keep is not None else old['keep']})
            outputs = self.units(new)
            self.apply_units(outputs, new)
            print(f'Расписание: {new["calendar"]}; хранить {new["keep"]} копий. '
                  'Ротация применяется после следующей успешной копии.')

    def apply_units(self, outputs, preferences, *, enable=False):
        previous = self.timer_state()
        originals = {p: file_bytes(p) if p.exists() else None for p in outputs}
        originals[self.preferences_path] = file_bytes(self.preferences_path)
        try:
            for path, content in outputs.items():
                self.m.atomic_write(path, content, 0o644)
            self.m.write_json(self.preferences_path, preferences)
            if not self.meta['local']:
                self.run(['systemctl', 'daemon-reload'])
                if enable:
                    self.run(['systemctl', 'enable', '--now', TIMER])
                elif previous.get('ActiveState') == 'active':
                    self.run(['systemctl', 'restart', TIMER])
        except Exception as exc:
            try:
                for path, value in originals.items():
                    if value is None:
                        path.unlink(missing_ok=True)
                    else:
                        self.m.atomic_write(path, value, 0o600 if path == self.preferences_path else 0o644)
                if not self.meta['local']:
                    self.run(['systemctl', 'daemon-reload'])
                    if previous.get('ActiveState') == 'active':
                        self.run(['systemctl', 'restart', TIMER])
                    elif enable:
                        if previous.get('UnitFileState') in {'enabled', 'enabled-runtime'}:
                            self.run(['systemctl', 'stop', TIMER])
                        else:
                            self.run(['systemctl', 'disable', '--now', TIMER])
            except Exception:
                raise BackupError('Не удалось полностью вернуть настройки таймера; копии и рабочие данные сохранены.', 2) from exc
            raise BackupError('Изменение таймера не завершено; прежние настройки возвращены.') from exc

    def disable(self):
        with self.lock():
            if not self.meta['local']:
                self.run(['systemctl', 'disable', '--now', TIMER])
            print('Таймер регулярных копий выключен; имеющиеся копии сохранены.')

    def status(self):
        value = self.preferences()
        timer = self.timer_state()
        print(f'Расписание: {value["calendar"]}; хранить {value["keep"]} копий.')
        print(f'Таймер: {timer.get("ActiveState", "unknown")}; {timer.get("UnitFileState", "unknown")}.')
        if timer.get('NextElapseUSecRealtime'):
            print('Следующий запуск: ' + timer['NextElapseUSecRealtime'])
        if not self.meta['local'] and (self.paths['unit'].parent / SERVICE).exists():
            output = self.run(['systemctl', 'show', SERVICE,
                               '--property=Result,ExecMainStatus,ExecMainStartTimestamp'])
            service = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
            if service.get('ExecMainStartTimestamp'):
                print('Последний системный запуск: ' + service['ExecMainStartTimestamp'] +
                      '; результат=' + service.get('Result', 'unknown') +
                      '; код=' + service.get('ExecMainStatus', 'unknown'))
            else:
                print('Служба по расписанию ещё не запускалась.')
        if self.status_path.exists():
            saved = json.loads(file_bytes(self.status_path))
            print('Последняя попытка: ' + ('успех' if saved.get('last_outcome') == 'ok' else 'ошибка'))
            if saved.get('last_success'):
                print('Последняя успешная копия UTC: ' + saved['last_success'])
                print('Пробное восстановление: ' + ('выполнено' if saved.get('restore_checked') else 'нет'))
        else:
            print('Успешных регулярных копий пока нет.')
        if self.root.exists():
            incomplete = sum(p.name.startswith(('.partial-', '.restore-')) for p in self.root.iterdir())
            if incomplete:
                print(f'Незавершённых временных каталогов: {incomplete}; требуется ручная проверка.')

    def listing(self):
        if not self.root.exists():
            print('Регулярных копий пока нет.')
            return
        for path in sorted(self.root.iterdir(), reverse=True):
            if not path.name.startswith('daily-'):
                continue
            try:
                manifest = self.verify(path)
                print(path.name + ' — ' + manifest['created_utc'] + '; целостность проверена')
            except (BackupError, OSError, ValueError, KeyError, sqlite3.Error, self.m.UpdateError):
                print(path.name + ' — проверка не пройдена')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Регулярные приватные копии и изолированная проверка восстановления.')
    parser.add_argument('--prefix', type=Path, default=Path(DEFAULT_PREFIX))
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('create', 'list', 'status', 'enable', 'disable'):
        commands.add_parser(name)
    for name in ('verify', 'restore-test'):
        commands.add_parser(name).add_argument('snapshot', type=Path)
    configure = commands.add_parser('configure')
    configure.add_argument('--calendar')
    configure.add_argument('--keep', type=int)
    args = parser.parse_args(argv)
    try:
        os.umask(0o077)
        instance = Backups(args.prefix)
        if not instance.meta['local'] and os.geteuid() != 0:
            raise BackupError('Системное копирование выполняется через sudo.', 2)
        if args.command == 'create':
            instance.create()
        elif args.command == 'list':
            instance.listing()
        elif args.command == 'status':
            instance.status()
        elif args.command == 'enable':
            instance.enable()
        elif args.command == 'disable':
            instance.disable()
        elif args.command == 'configure':
            instance.configure(args.calendar, args.keep)
        else:
            with instance.lock():
                if args.command == 'restore-test':
                    instance.restore_test(args.snapshot)
                    print('Пробное восстановление выполнено в отдельном каталоге. Рабочие данные сохранены.')
                else:
                    instance.verify(args.snapshot)
                    print('Целостность копии проверена.')
        return 0
    except BackupError as exc:
        print('Ошибка копирования: ' + str(exc), file=sys.stderr)
        return exc.code
    except Exception:
        # Исключения SQLite/JSON/подпроцессов могут содержать личные значения.
        print('Ошибка копирования: операция не завершена; проверьте status и журнал. Секреты не выводятся.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
