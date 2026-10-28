"""Сбой на каждом опасном переходе: настоящий диск, SQLite и журнал операции."""

import json
from pathlib import Path
import shutil
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from goose import maintenance as m
from goose.lifecycle import notify_ready


COMMIT = 'a' * 40


class FakeSystem:
    def __init__(self):
        self.events = []
        self.active = True
        self.failure = None

    def status(self):
        return {'ActiveState': 'active' if self.active else 'inactive', 'InvocationID': 'old'}

    def stop(self):
        self.events.append('stop')
        self.active = False

    def reload(self):
        self.events.append('reload')

    def start(self, previous, timeout, **kwargs):
        self.events.append('start')
        if self.failure:
            failure, self.failure = self.failure, None
            failure()
        self.active = True

    def enable(self):
        self.events.append('enable')

    def restore_enabled(self, previous):
        pass


@pytest.fixture
def installation(tmp_path, monkeypatch):
    prefix, data, repo, config = (tmp_path / n for n in ('app', 'data', 'repo', 'config'))
    for p in (prefix, data, repo, config):
        p.mkdir(mode=0o700)
    (prefix / 'venv/bin').mkdir(parents=True)
    (prefix / 'venv/bin/python').write_text('old')
    cfg = config / 'config.toml'
    cfg.write_text(f'data_dir = "{data}"\n[llm]\npersona_file = "{config / "persona.md"}"\n')
    (config / 'persona.md').write_text('Собственный образ Гуся')
    (config / 'goose.env').write_text('GOOSE_XMPP_PASSWORD="test-only-secret"\n')
    (prefix / m.UNIT).write_text('[Service]\nType=simple\n')
    with sqlite3.connect(data / 'goose.sqlite3') as db:
        db.executescript("PRAGMA user_version=4; CREATE TABLE omemo(key TEXT, value TEXT);"
                         "INSERT INTO omemo VALUES('identity','original-key');"
                         "CREATE TABLE profiles(grade INTEGER); INSERT INTO profiles VALUES(7);")
    (data / 'goose.sqlite3').chmod(0o600)
    value = dict(format=1, prefix=str(prefix), data=str(data), repo=str(repo),
                 config=str(cfg), env=str(config / 'goose.env'), unit=str(prefix / m.UNIT),
                 backups=str(tmp_path / 'backups'), local=True, commit='b' * 40)
    m.write_json(prefix / 'maintenance.json', value)
    system = FakeSystem()
    calls = []
    def run(args, **kwargs):
        args = [str(a) for a in args]
        calls.append(args)
        if args[0] == 'git':
            return '' if 'status' in args else 'b' * 40
        if args[-1] == m.INSPECT:
            return '{"omemo":"1", "slixmpp":"1.17.0"}'
        return ''
    updater = m.Updater(prefix, runner=run, controller=system)
    def stage(ref):
        release = prefix / 'releases' / COMMIT[:12]
        (release / 'venv/bin').mkdir(parents=True)
        (release / 'venv/bin/python').write_text('new')
        (release / m.UNIT).write_text('[Service]\nType=notify\n')
        source = release / 'source/src/goose'
        source.mkdir(parents=True)
        shutil.copyfile(Path(m.__file__), source / 'maintenance.py')
        m.write_json(release / '.goose-release.json', {'managed': 1, 'complete': True})
        return COMMIT, release
    monkeypatch.setattr(updater, 'stage', stage)
    return SimpleNamespace(updater=updater, system=system, calls=calls, paths=updater.paths,
                           meta=value, stage=stage)


def contents(paths):
    return {str(p.relative_to(paths['data'])): p.read_bytes()
            for p in paths['data'].rglob('*') if p.is_file() and p.name != '.lock'}


def test_success_backs_up_private_inputs_and_switches_only_code(installation, capsys):
    x = installation
    before = contents(x.paths)
    protected = {k: x.paths[k].read_bytes() for k in ('config', 'env', 'persona')}
    x.updater.update()
    assert x.system.events == ['stop', 'reload', 'start', 'enable']
    assert x.updater.active.is_symlink() and (x.updater.active / 'bin/python').read_text() == 'new'
    assert contents(x.paths) == before
    assert all(x.paths[k].read_bytes() == v for k, v in protected.items())
    copy = next(x.paths['backups'].glob('update-*'))
    assert (copy / 'data/goose.sqlite3').read_bytes() == before['goose.sqlite3']
    assert (copy / 'env').read_bytes() == protected['env']
    assert copy.stat().st_mode & 0o077 == 0
    assert all(p.stat().st_mode & 0o077 == 0 for p in copy.rglob('*') if p.is_file())
    manifest = json.loads((copy / 'manifest.json').read_text())
    assert manifest['schema'] == 4
    assert not x.updater.journal.exists()
    assert json.loads((x.paths['prefix'] / 'maintenance.json').read_text())['commit'] == COMMIT
    assert 'test-only-secret' not in capsys.readouterr().out


@pytest.mark.parametrize('phase', ['download', 'migration', 'backup', 'activation', 'health'])
def test_failure_restores_old_code_without_touching_keys(installation, monkeypatch, phase):
    x = installation
    before = contents(x.paths)
    def failure(*args, **kwargs):
        raise m.UpdateError('Подставной отказ')
    if phase == 'download':
        monkeypatch.setattr(x.updater, 'stage', failure)
    elif phase == 'backup':
        monkeypatch.setattr(m, 'backup', failure)
    elif phase == 'activation':
        monkeypatch.setattr(x.updater, 'switch', failure)
    elif phase == 'health':
        x.system.failure = failure
    else:
        original = x.updater.run
        def run(args, **kwargs):
            if m.MIGRATE in args:
                failure()
            return original(args, **kwargs)
        x.updater.run = run
    with pytest.raises(m.UpdateError):
        x.updater.update()
    assert not x.updater.active.is_symlink()
    assert (x.updater.active / 'bin/python').read_text() == 'old'
    assert contents(x.paths) == before
    assert x.system.active
    assert not x.updater.journal.exists()
    if phase == 'download':
        assert x.system.events == []


def test_failed_health_preserves_advanced_ratchet_when_reverting_code(installation):
    x = installation
    def fail_after_work():
        with sqlite3.connect(x.paths['data'] / 'goose.sqlite3') as db:
            db.execute("UPDATE omemo SET value='advanced-ratchet'")
        raise m.UpdateError('Сбой после обмена')
    x.system.failure = fail_after_work
    with pytest.raises(m.UpdateError):
        x.updater.update()
    with sqlite3.connect(x.paths['data'] / 'goose.sqlite3') as db:
        assert db.execute('SELECT value FROM omemo').fetchone()[0] == 'advanced-ratchet'
    assert x.system.active and not x.updater.active.is_symlink()


@pytest.mark.parametrize('incompatible', ['schema', 'dependencies'])
def test_incompatible_rollback_keeps_new_database_and_recovery_journal(installation, incompatible):
    x = installation
    def fail():
        with sqlite3.connect(x.paths['data'] / 'goose.sqlite3') as db:
            db.execute("UPDATE omemo SET value='advanced'")
            if incompatible == 'schema':
                db.execute('PRAGMA user_version=5')
        if incompatible == 'dependencies':
            original = x.updater.run
            def runner(args, **kwargs):
                return '{"omemo":"2"}' if args[-1] == m.INSPECT else original(args, **kwargs)
            x.updater.run = runner
        raise m.UpdateError('Не готов')
    x.system.failure = fail
    with pytest.raises(m.UpdateError, match='Сервис остановлен'):
        x.updater.update()
    assert not x.system.active and x.updater.active.is_symlink() and x.updater.journal.exists()
    with sqlite3.connect(x.paths['data'] / 'goose.sqlite3') as db:
        assert db.execute('SELECT value FROM omemo').fetchone()[0] == 'advanced'
    with pytest.raises(m.UpdateError, match='прерванное'):
        x.updater.update()


def test_interruption_after_legacy_rename_is_recoverable(installation, monkeypatch):
    x = installation
    # Симулируем SIGKILL: не даём обработчику исключения выполнить откат.
    recover = x.updater.recover
    monkeypatch.setattr(x.updater, 'recover', lambda *a, **k: (_ for _ in ()).throw(m.UpdateError('power loss')))
    monkeypatch.setattr(x.updater, 'switch', lambda *a: (_ for _ in ()).throw(m.UpdateError('interrupted')))
    with pytest.raises(m.UpdateError):
        x.updater.update()
    assert not x.updater.active.exists() and x.updater.journal.exists()
    monkeypatch.setattr(x.updater, 'recover', recover)
    x.updater.update(recover=True)
    assert (x.updater.active / 'bin/python').read_text() == 'old'
    assert x.system.active and not x.updater.journal.exists()


def test_check_does_not_stop_service_and_does_not_prune_active_release(installation):
    x = installation
    release = x.paths['prefix'] / 'releases/current'
    release.mkdir(parents=True)
    x.updater.active.rename(release / 'venv')
    x.updater.active.symlink_to(release / 'venv')
    m.write_json(release / '.goose-release.json', {'managed': 1})
    before = contents(x.paths)
    x.updater.update(check=True)
    assert not x.system.events and contents(x.paths) == before
    assert x.updater.active.exists()


def test_dirty_checkout_and_concurrent_update_leave_service_running(installation):
    x = installation
    original = x.updater.run
    x.updater.run = lambda args, **kw: ' M file' if 'status' in args else original(args, **kw)
    with pytest.raises(m.UpdateError, match='изменения'):
        x.updater.update()
    assert not x.system.events
    x.updater.run = original
    with m.locked(x.paths['prefix'] / '.update.lock'):
        with pytest.raises(m.UpdateError, match='занято'):
            x.updater.update()
    assert not x.system.events


def test_data_lock_conflict_never_switches_active_code(installation):
    x = installation
    with m.locked(x.paths['data'] / '.lock'):
        with pytest.raises(m.UpdateError, match='занято'):
            x.updater.update()
    assert not x.updater.active.is_symlink() and x.updater.journal.exists()
    x.updater.update(recover=True)
    assert x.system.active


def test_symlink_in_data_cannot_copy_external_secrets(installation, tmp_path):
    x = installation
    external = tmp_path / 'external'
    external.write_text('must not copy')
    (x.paths['data'] / 'link').symlink_to(external)
    with pytest.raises(m.UpdateError):
        x.updater.update()
    assert x.system.active and not x.updater.active.is_symlink()
    assert not list(x.paths['backups'].rglob('link'))


def test_migration_runs_against_clone_not_live_or_preserved_copy(installation):
    x = installation
    original = x.updater.run
    def run(args, **kwargs):
        if m.MIGRATE in args:
            clone = Path(args[-1])
            assert clone != x.paths['data'] and clone.parent.name.startswith('migration-')
            with sqlite3.connect(clone / 'goose.sqlite3') as db:
                db.execute("UPDATE omemo SET value='trial-only'")
        return original(args, **kwargs)
    x.updater.run = run
    before = contents(x.paths)
    x.updater.update()
    assert contents(x.paths) == before


def test_system_requires_new_invocation_notify_and_running_pid(monkeypatch):
    seen = []
    states = iter([
        'Type=notify\nActiveState=active\nSubState=running\nMainPID=10\nInvocationID=old',
        'Type=notify\nActiveState=activating\nSubState=start\nMainPID=20\nInvocationID=new',
        'Type=notify\nActiveState=active\nSubState=running\nMainPID=20\nInvocationID=new'])
    def run(args, **kwargs):
        seen.append(args)
        return next(states) if 'show' in args else ''
    monkeypatch.setattr(m.time, 'sleep', lambda _: None)
    m.System(False, run).start('old', 1)
    assert sum('show' in args for args in seen) == 3


def test_plain_active_status_is_insufficient_for_success(monkeypatch):
    def run(args, **kwargs):
        return 'Type=simple\nActiveState=active\nSubState=running\nMainPID=20\nInvocationID=new'
    times = iter([0, 0, 2])
    monkeypatch.setattr(m.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(m.time, 'sleep', lambda _: None)
    with pytest.raises(m.UpdateError, match='готовность XMPP'):
        m.System(False, run).start('old', 1)


def test_settings_changed_during_stage_cancel_before_service_stop(installation, monkeypatch):
    x = installation
    def stage(ref):
        result = x.stage(ref)
        x.paths['env'].write_text('operator-changed-env')
        return result
    monkeypatch.setattr(x.updater, 'stage', stage)
    with pytest.raises(m.UpdateError, match='настройки изменились'):
        x.updater.update()
    assert not x.system.events and x.paths['env'].read_text() == 'operator-changed-env'


@pytest.mark.parametrize('ref', ['--upload-pack=bad', '../main', 'main;echo', 'main\nother'])
def test_invalid_ref_is_rejected_before_git_or_service(installation, ref):
    x = installation
    with pytest.raises(m.UpdateError, match='Некорректный ref'):
        m.Updater.stage(x.updater, ref)
    assert not x.calls and not x.system.events


def test_fetch_failure_is_before_service_stop(installation):
    x = installation
    def run(args, **kwargs):
        raise m.UpdateError('Сеть Git недоступна')
    x.updater.run = run
    with pytest.raises(m.UpdateError, match='Git недоступна'):
        m.Updater.stage(x.updater, 'main')
    assert not x.system.events and not (x.paths['prefix'] / 'releases').exists()


def test_adoption_installs_standalone_launcher_without_touching_legacy_code(installation, monkeypatch):
    x = installation
    (x.paths['prefix'] / 'maintenance.json').unlink()
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(m, 'run', lambda args, **kwargs: COMMIT)
    before = contents(x.paths)
    m.bootstrap(root, x.paths['prefix'], x.paths['config'], x.paths['data'], x.paths['env'], True)
    launcher = x.paths['prefix'] / 'bin/goose-update'
    assert launcher.stat().st_mode & 0o777 == 0o755
    assert f"DEFAULT_PREFIX = {str(x.paths['prefix'])!r}" in launcher.read_text()
    assert (x.updater.active / 'bin/python').read_text() == 'old'
    assert contents(x.paths) == before and not x.system.events
    metadata = (x.paths['prefix'] / 'maintenance.json').read_bytes()
    with pytest.raises(m.UpdateError, match='уже настроен'):
        m.bootstrap(root, x.paths['prefix'], x.paths['config'], x.paths['data'], x.paths['env'], True)
    assert (x.paths['prefix'] / 'maintenance.json').read_bytes() == metadata


def test_notify_uses_unix_socket_and_never_includes_configuration(tmp_path, monkeypatch):
    path = str(tmp_path / 'notify')
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(path)
        server.settimeout(1)
        monkeypatch.setenv('NOTIFY_SOCKET', path)
        assert notify_ready()
        data = server.recv(4096)
    assert data.startswith(b'READY=1\nSTATUS=') and b'@' not in data


def test_unavailable_notify_is_nonfatal(monkeypatch):
    monkeypatch.setenv('NOTIFY_SOCKET', '/missing/notification/socket')
    assert not notify_ready()
