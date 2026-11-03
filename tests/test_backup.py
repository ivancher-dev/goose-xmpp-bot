"""Живой SQLite-писатель и restore на отдельном диске; XMPP/API не запускаются."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import time

import pytest
import pytest_asyncio

from goose import backup as b
from goose import maintenance as m
from goose.config import load_config
from goose.storage import OmemoStorage, data_lock


@pytest_asyncio.fixture
async def installation(config_file, config, database, tmp_path):
    prefix = tmp_path / 'app'
    prefix.mkdir(mode=0o700)
    # В CI Python установлен вне checkout; используем окружение текущего pytest.
    (prefix / 'venv').symlink_to(Path(sys.prefix))
    env, persona = tmp_path / 'goose.env', tmp_path / 'persona.md'
    env.write_text('GOOSE_XMPP_PASSWORD="not-executed-$(touch /tmp/never-created)"\n')
    env.chmod(0o600)
    persona.write_text('Личный синтетический образ.')
    persona.chmod(0o600)
    config_file.write_text(config_file.read_text().replace('/etc/goose-xmpp-bot/persona.md', str(persona)))
    config_file.chmod(0o600)
    unit = prefix / 'goose-xmpp-bot.service'
    unit.write_text('[Service]\nType=notify\n')
    unit.chmod(0o600)
    repo = tmp_path / 'repo'
    repo.mkdir()
    value = dict(format=1, repo=str(repo), prefix=str(prefix), config=str(config_file),
                 data=str(config.data_dir), env=str(env), backups=str(tmp_path / 'backups'),
                 unit=str(unit), local=True, commit='a' * 40)
    m.write_json(prefix / 'maintenance.json', value)
    await database.update_profile('pupil2@example.org', 'grade', 8)
    await database.set_mode('pupil2@example.org', 'auto')
    await database.set_mode('pupil1@example.org', 'manual')
    await database.add_user_message('pupil2@example.org', 'Синтетическая история')
    await database.set_weather_location('pupil2@example.org', {'id': 123, 'name': 'Fixture'})
    await OmemoStorage(database)._store('synthetic-key', {'identity': 'persistent', 'trust': 'TRUSTED'})
    await database.start_api_usage('fixture-usage', 'fixture-call', 'pupil2@example.org',
                                   'https://api.example.org/v1', 'fixture-model', 1, time.time())
    await database.finish_api_usage('fixture-usage', 'success', (20, 5, 25), time.time())
    return b.Backups(prefix), database


def logical_rows(directory):
    with sqlite3.connect(directory / 'goose.sqlite3') as db:
        return list(db.iterdump())


async def test_create_while_app_data_lock_is_held_preserves_everything_and_is_private(installation, capsys):
    instance, database = installation
    before = logical_rows(database.path.parent)
    with data_lock(database.path.parent):
        snapshot = instance.create()
    manifest = instance.verify(snapshot)
    assert manifest['schema'] == 6 and manifest['kind'] == 'scheduled'
    assert logical_rows(snapshot / 'data') == before == logical_rows(database.path.parent)
    for key in ('config', 'env', 'persona', 'unit'):
        assert (snapshot / key).read_bytes() == instance.paths[key].read_bytes()
    assert snapshot.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o077 == 0 for p in snapshot.rglob('*'))
    assert json.loads(instance.status_path.read_text())['restore_checked']
    assert not list(instance.root.glob('.restore-*')) and not list(instance.root.glob('.partial-*'))
    text = capsys.readouterr().out
    assert all(private not in text for private in ('pupil2@', 'synthetic-key', 'persistent', 'not-executed'))
    assert await database.profile('pupil2@example.org') == {
        'jid': 'pupil2@example.org', 'grade': 8, 'academic_year': '2026-2027',
        'schedule_class_id': 'cohort-example-2', 'explanation_style': 'step_by_step_with_reasoning'}


@pytest.mark.parametrize('journal_mode', ['WAL', 'DELETE'])
async def test_online_backup_includes_committed_wal_and_concurrent_transactions(installation, journal_mode):
    instance, database = installation
    with sqlite3.connect(database.path) as db:
        db.execute('PRAGMA journal_mode=' + journal_mode)
        db.execute("INSERT INTO omemo VALUES('pair-a','0')")
        db.execute("INSERT INTO omemo VALUES('pair-b','0')")
    stop, entered = threading.Event(), threading.Event()
    failures = []
    def writer():
        try:
            with sqlite3.connect(database.path, timeout=5) as db:
                db.execute('PRAGMA wal_autocheckpoint=0')
                counter = 0
                while not stop.is_set():
                    counter += 1
                    with db:
                        db.execute("UPDATE omemo SET value=? WHERE key IN ('pair-a','pair-b')", (str(counter),))
                    entered.set()
                    stop.wait(0.001)
        except Exception as exc:
            failures.append(type(exc).__name__)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert entered.wait(2)
        snapshot = instance.create()
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive() and not failures
    with sqlite3.connect(snapshot / 'data/goose.sqlite3') as db:
        rows = db.execute("SELECT value FROM omemo WHERE key IN ('pair-a','pair-b')").fetchall()
    assert rows[0] == rows[1] and int(rows[0][0]) > 0
    assert sorted(p.name for p in (snapshot / 'data').iterdir()) == ['goose.sqlite3']


def test_sqlite_wait_is_bounded_when_writer_holds_exclusive_lock(tmp_path):
    source, destination = tmp_path / 'source.sqlite3', tmp_path / 'copy.sqlite3'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE example(value)')
    with sqlite3.connect(source) as db:
        db.execute('BEGIN EXCLUSIVE')
        started = time.monotonic()
        with pytest.raises(b.BackupError, match='Истекло'):
            b.sqlite_copy(source, destination, timeout=0.03)
        assert time.monotonic() - started < 1


@pytest.mark.parametrize('failure', ['sqlite', 'restore', 'disk', 'config-change'])
async def test_failed_new_copy_never_rotates_old_verified_copies(installation, monkeypatch, failure):
    instance, database = installation
    first = instance.create()
    before = m.digest(first / 'data/goose.sqlite3')
    if failure == 'sqlite':
        def fail(*args, **kwargs):
            raise b.BackupError('подставной отказ')
        monkeypatch.setattr(b, 'sqlite_copy', fail)
    elif failure == 'restore':
        instance.run = lambda *args, **kwargs: (_ for _ in ()).throw(m.UpdateError('restore failed'))
    elif failure == 'disk':
        monkeypatch.setattr(shutil, 'disk_usage', lambda _: type('Space', (), {'free': 0})())
    else:
        original = b.sqlite_copy
        def changing(*args, **kwargs):
            original(*args, **kwargs)
            instance.paths['env'].write_text('operator edit')
        monkeypatch.setattr(b, 'sqlite_copy', changing)
    with pytest.raises((b.BackupError, m.UpdateError)):
        instance.create()
    assert first.exists() and m.digest(first / 'data/goose.sqlite3') == before
    assert not list(instance.root.glob('.partial-*'))
    assert len(list(instance.root.glob('daily-*'))) == 1
    status = json.loads(instance.status_path.read_text())
    assert status['last_outcome'] == 'failed' and status['last_success']


async def test_rotation_only_deletes_verified_scheduled_copies_of_this_installation(installation):
    instance, database = installation
    with instance.lock():
        preferences = instance.preferences(create=True)
        m.write_json(instance.preferences_path, {**preferences, 'keep': 2})
    first = instance.create()
    second = instance.create()
    foreign = instance.root / 'daily-20000101T000000Z-01234567'
    shutil.copytree(first, foreign)
    value = json.loads((foreign / 'manifest.json').read_text())
    value['installation'] = 'b' * 32
    m.write_json(foreign / 'manifest.json', value)
    corrupted = instance.root / 'daily-20000102T000000Z-01234567'
    shutil.copytree(first, corrupted)
    (corrupted / 'data/goose.sqlite3').write_bytes(b'corrupted')
    unknown = instance.root / 'manual-copy'
    unknown.mkdir(mode=0o700)
    update = instance.paths['backups'] / 'update-existing'
    update.mkdir(mode=0o700)
    (update / 'preserve').write_text('pre-update copy')
    newest = instance.create()
    assert not first.exists() and second.exists() and newest.exists()
    assert foreign.exists() and corrupted.exists() and unknown.exists() and update.exists()


@pytest.mark.parametrize('failure', ['path', 'hash', 'extra', 'symlink', 'permissions', 'manifest-permissions', 'timestamp'])
async def test_manifest_cannot_escape_or_hide_corruption(installation, tmp_path, failure):
    instance, _ = installation
    snapshot = instance.create()
    manifest_path = snapshot / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if failure == 'path':
        manifest['sha256']['../../outside'] = 'a' * 64
        m.write_json(manifest_path, manifest)
    elif failure == 'hash':
        (snapshot / 'env').write_text('changed')
    elif failure == 'extra':
        (snapshot / 'outside-manifest').write_text('extra')
    elif failure == 'symlink':
        (snapshot / 'env').unlink()
        (snapshot / 'env').symlink_to(instance.paths['env'])
    elif failure == 'manifest-permissions':
        manifest_path.chmod(0o644)
    elif failure == 'timestamp':
        manifest['created_utc'] = 1
        m.write_json(manifest_path, manifest)
    else:
        (snapshot / 'env').chmod(0o644)
    with pytest.raises(b.BackupError):
        instance.verify(snapshot)


async def test_shared_update_lock_and_interrupted_update_prevent_backup(installation):
    instance, _ = installation
    with m.locked(instance.prefix / '.update.lock'):
        with pytest.raises(b.BackupError) as captured:
            instance.create()
        assert captured.value.code == 75
    (instance.prefix / 'update-state.json').write_text('{}')
    with pytest.raises(b.BackupError, match='прерванного'):
        instance.create()
    assert not instance.root.exists()


async def test_legacy_update_copy_can_be_verified_and_restored_without_touching_live_data(installation):
    instance, database = installation
    before = logical_rows(database.path.parent)
    snapshot = m.backup(instance.paths, 'a' * 40)
    assert instance.verify(snapshot)['schema'] == 6
    assert instance.restore_test(snapshot)['omemo_preserved']
    assert logical_rows(database.path.parent) == before


async def test_configure_validates_before_changes_and_rotation_waits_for_next_good_copy(installation):
    instance, _ = installation
    instance.configure(keep=7)
    before = instance.preferences_path.read_bytes()
    for value in (0, 1, 366):
        with pytest.raises(b.BackupError):
            instance.configure(keep=value)
        assert instance.preferences_path.read_bytes() == before
    with pytest.raises(b.BackupError):
        instance.configure(calendar='daily\nExecStart=bad')
    assert instance.preferences_path.read_bytes() == before
    assert not instance.root.exists()


@pytest.mark.parametrize('restore_fails', [False, True])
async def test_enable_only_starts_backup_timer_after_successful_restore(installation, monkeypatch, restore_fails):
    instance, _ = installation
    value = dict(instance.meta, local=False)
    m.write_json(instance.prefix / 'maintenance.json', value)
    instance.meta = value
    calls = []
    real = instance.run
    def runner(args, **kwargs):
        calls.append([str(x) for x in args])
        if args[0] in {'systemctl', 'systemd-analyze'}:
            return ''
        if restore_fails:
            raise m.UpdateError('fake restore failure')
        return real(args, **kwargs)
    instance.run = runner
    def resources():
        for name in (b.SERVICE, b.TIMER):
            source = Path(b.__file__).parent / 'systemd' / (name + '.in')
            m.atomic_write(instance.prefix / (name + '.in'), source.read_bytes(), 0o644)
    monkeypatch.setattr(instance, 'install_resources', resources)
    if restore_fails:
        with pytest.raises(b.BackupError, match='Пробное восстановление'):
            instance.enable()
        assert not any(call[0] == 'systemctl' for call in calls)
    else:
        instance.enable()
        assert ['systemctl', 'enable', '--now', b.TIMER] in calls
        assert all('goose-xmpp-bot.service' not in call for call in calls if call[0] == 'systemctl')
        assert json.loads(instance.status_path.read_text())['restore_checked']


async def test_source_symlink_is_rejected_before_copy(installation):
    instance, _ = installation
    (instance.paths['data'] / 'external').symlink_to(instance.paths['env'])
    with pytest.raises(b.BackupError, match='неизвестный'):
        instance.create()


async def test_restore_check_rejects_network_even_if_future_doctor_attempts_it(installation):
    instance, _ = installation
    snapshot = instance.create()
    real = instance.run
    def runner(args, **kwargs):
        args = list(args)
        args[2] = b.RESTORE_CHECK.replace('doctor(c)', "__import__('socket').create_connection(('example.org',443))")
        return real(args, **kwargs)
    instance.run = runner
    with pytest.raises(b.BackupError, match='Пробное восстановление'):
        instance.restore_test(snapshot)
    assert instance.verify(snapshot)['schema'] == 6


async def test_real_identity_trust_and_next_message_survive_cold_restore(installation, tmp_path):
    from test_crypto import Pep, create_manager, trust_peer
    from goose.omemo import LEGACY_NS
    instance, database = installation
    config = load_config(instance.paths['config'])
    pep = Pep()
    bot, _ = await create_manager(pep, database.path.parent, config.jid)
    parent, _ = await create_manager(pep, tmp_path / 'parent-crypto', 'parent@example.org')
    recovered = None
    bot_closed = False
    try:
        await trust_peer(bot, 'parent@example.org')
        await trust_peer(parent, config.jid)
        original_identity = (await bot.get_own_device_information())[0]
        packets, errors = await parent.encrypt(frozenset({config.jid}), {LEGACY_NS: b'before backup'})
        assert not errors and (await bot.decrypt(next(iter(packets))))[0] == b'before backup'
        before = logical_rows(database.path.parent)
        snapshot = instance.create()
        assert logical_rows(database.path.parent) == before
        await bot.shutdown()
        bot_closed = True
        restored = tmp_path / 'cold-restored'
        shutil.copytree(snapshot / 'data', restored)
        recovered, _ = await create_manager(pep, restored, config.jid)
        identity = (await recovered.get_own_device_information())[0]
        assert (identity.device_id, identity.identity_key) == (original_identity.device_id, original_identity.identity_key)
        assert next(iter(await recovered.get_device_information('parent@example.org'))).trust_level_name == 'TRUSTED'
        packets, errors = await parent.encrypt(frozenset({config.jid}), {LEGACY_NS: b'after cold restore'})
        assert not errors and (await recovered.decrypt(next(iter(packets))))[0] == b'after cold restore'
    finally:
        await parent.shutdown()
        if recovered:
            await recovered.shutdown()
        if not bot_closed:
            await bot.shutdown()


async def test_updater_registration_preserves_preferences_and_enabled_timer_files(installation):
    instance, _ = installation
    instance.enable()
    instance.configure(keep=7)
    paths = [instance.preferences_path, instance.paths['unit'].parent / b.SERVICE,
             instance.paths['unit'].parent / b.TIMER, instance.prefix / 'maintenance.json']
    before = {p: p.read_bytes() for p in paths}
    source = Path(__file__).resolve().parents[1]
    m.install_launcher(source, instance.paths, True)
    assert all(path.read_bytes() == content for path, content in before.items())
    launcher = instance.prefix / 'bin/goose-backup'
    compile(launcher.read_text(), str(launcher), 'exec')
    assert launcher.stat().st_mode & 0o777 == 0o755


async def test_configure_rolls_back_files_on_manager_failure(installation, monkeypatch):
    instance, _ = installation
    instance.configure(keep=7)
    paths = [instance.preferences_path, instance.paths['unit'].parent / b.SERVICE,
             instance.paths['unit'].parent / b.TIMER]
    before = {p: p.read_bytes() for p in paths}
    instance.meta['local'] = False
    value = dict(instance.meta)
    m.write_json(instance.prefix / 'maintenance.json', value)
    monkeypatch.setattr(instance, 'install_resources', lambda: None)
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        if 'show' in args:
            return 'ActiveState=active\nUnitFileState=enabled'
        if args[:2] == ['systemctl', 'daemon-reload'] and sum(a[:2] == ['systemctl', 'daemon-reload'] for a in calls) == 1:
            raise m.UpdateError('fake reload failure')
        return ''
    instance.run = runner
    with pytest.raises(b.BackupError, match='прежние настройки'):
        instance.configure(keep=2)
    assert all(path.read_bytes() == content for path, content in before.items())
    assert ['systemctl', 'restart', b.TIMER] in calls


async def test_interruption_leaves_old_copy_and_does_not_publish_partial(installation, monkeypatch):
    instance, _ = installation
    first = instance.create()
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(b, 'sqlite_copy', interrupt)
    with pytest.raises(KeyboardInterrupt):
        instance.create()
    assert first.exists() and len(list(instance.root.glob('daily-*'))) == 1
    assert not list(instance.root.glob('.partial-*'))
