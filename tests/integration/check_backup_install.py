"""Offline root-контейнер: wheel 0.3.4, первый enable, живой SQLite и стабильный CLI.

/repo — исходники; /installed-packages — runtime прежнего тестового venv;
/artifact — собранный wheel. Только systemd подставлен; сеть контейнера отключена.
Не запускать этот сценарий на VDS: он создаёт временную тестовую установку.
"""

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time


def main():
    assert os.geteuid() == 0
    prefix = Path('/opt/goose-xmpp-bot')
    data = Path('/var/lib/goose-xmpp-bot')
    config = Path('/etc/goose-xmpp-bot/config.toml')
    backups = Path('/var/backups/goose-xmpp-bot')
    for path in (prefix, data, config.parent, Path('/etc/systemd/system')):
        path.mkdir(parents=True, exist_ok=True)
    prefix.chmod(0o755)
    data.chmod(0o700)
    venv = prefix / 'venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', venv], check=True)
    shutil.copytree('/installed-packages', venv / 'lib/python3.12/site-packages', dirs_exist_ok=True)
    wheel = next(Path('/artifact').glob('goose_xmpp_bot-0.3.4-*.whl'))
    subprocess.run([venv / 'bin/python', '-m', 'pip', 'install', '--no-index', '--no-deps',
                    '--disable-pip-version-check', '--root-user-action=ignore', wheel], check=True)
    from_source = "import runpy; runpy.run_path('/repo/src/goose/maintenance.py',run_name='helper')['readable_release'](__import__('pathlib').Path('/opt/goose-xmpp-bot/venv'))"
    subprocess.run([sys.executable, '-c', from_source], check=True)
    config.write_text(Path('/repo/config/config.example.toml').read_text())
    config.chmod(0o640)
    env = config.parent / 'goose.env'
    env.write_text('GOOSE_XMPP_PASSWORD="fixture-$(touch /tmp/forbidden-shell)"\n')
    env.chmod(0o600)
    (config.parent / 'persona.md').write_text('Тестовый образ.')
    unit = Path('/etc/systemd/system/goose-xmpp-bot.service')
    unit.write_text('[Service]\nType=notify\n')
    # Резервирование не должно зависеть от доступности checkout в /root.
    metadata = dict(format=1, repo='/root/unavailable-checkout', prefix=str(prefix), config=str(config),
                    data=str(data), env=str(env), backups=str(backups), unit=str(unit),
                    local=False, commit='a' * 40)
    (prefix / 'maintenance.json').write_text(json.dumps(metadata))
    (prefix / 'maintenance.json').chmod(0o600)
    # Менеджер служб отсутствует в контейнере; журналируются только вызовы таймера.
    manager = Path('/usr/local/bin/systemctl')
    manager.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> /tmp/backup-systemctl.calls\n'
                       'if [ "$1" = show ]; then printf "ActiveState=active\\nUnitFileState=enabled\\n"; fi\n')
    manager.chmod(0o755)
    analyze = Path('/usr/local/bin/systemd-analyze')
    analyze.write_text('#!/bin/sh\nexit 0\n')
    analyze.chmod(0o755)
    seed = '''
import asyncio
from pathlib import Path
from goose.config import load_config
from goose.storage import Database,OmemoStorage
async def main():
 c=load_config(Path('/etc/goose-xmpp-bot/config.toml'));db=Database(c.data_dir,c.jid)
 await db.initialize(c.users)
 await db.set_mode('pupil1@example.org','manual');await db.set_mode('pupil2@example.org','auto')
 await db.update_profile('pupil2@example.org','grade',8)
 await OmemoStorage(db)._store('fixture-identity',{'key':'persistent'})
 await db.call(lambda con: con.execute('CREATE TABLE backup_probe(a INTEGER,b INTEGER)'))
 await db.call(lambda con: con.execute('INSERT INTO backup_probe VALUES(0,0)'))
asyncio.run(main())
'''
    subprocess.run([venv / 'bin/python', '-c', seed], cwd=prefix, check=True)
    for path in (data, data / 'goose.sqlite3'):
        os.chown(path, 65534, 65534)
    writer_code = '''
import fcntl,sqlite3,time
from pathlib import Path
data=Path('/var/lib/goose-xmpp-bot')
with (data/'.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 with sqlite3.connect(data/'goose.sqlite3') as db:
  Path('/tmp/backup-writer-ready').touch()
  while not Path('/tmp/backup-writer-stop').exists():
   with db: db.execute('UPDATE backup_probe SET a=a+1,b=b+1')
   time.sleep(.002)
'''
    def identity():
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)
    writer = subprocess.Popen([venv / 'bin/python', '-c', writer_code], cwd=prefix, preexec_fn=identity)
    def run(args, expected=0):
        result = subprocess.run([str(a) for a in args], cwd=prefix, capture_output=True, text=True)
        assert result.returncode == expected, result.stdout + result.stderr
        return result.stdout
    try:
        deadline = time.monotonic() + 5
        while not Path('/tmp/backup-writer-ready').exists():
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert not Path('/usr/local/sbin/goose-backup').exists()
        run([venv / 'bin/goose-backup', 'enable'])
        command = Path('/usr/local/sbin/goose-backup')
        assert command.is_file()
        run([command, 'status'])
        run([command, 'configure', '--keep', '2'])
        run([command, 'create'])
        protected = backups / 'update-preserved'
        protected.mkdir(mode=0o700)
        run([command, 'create'])
        snapshots = sorted((backups / 'scheduled').glob('daily-*'))
        assert len(snapshots) == 2 and protected.exists()
        run([command, 'restore-test', snapshots[-1]])
        for snapshot in snapshots:
            run([command, 'verify', snapshot])
            assert snapshot.stat().st_mode & 0o777 == 0o700
            assert all(p.stat().st_mode & 0o077 == 0 for p in snapshot.rglob('*'))
            with closing(sqlite3.connect(snapshot / 'data/goose.sqlite3')) as db:
                a, b = db.execute('SELECT a,b FROM backup_probe').fetchone()
                assert a == b and a > 0
                assert db.execute("SELECT grade FROM profiles WHERE jid='pupil2@example.org'").fetchone()[0] == 8
        # Стабильный CLI читает список и проверяет копию даже без активного venv.
        moved = prefix / 'venv-unavailable'
        venv.rename(moved)
        try:
            run([command, 'status'])
            run([command, 'list'])
            run([command, 'verify', snapshots[-1]])
        finally:
            moved.rename(venv)
        originals = {p: p.read_bytes() for p in (prefix / 'backup-settings.json', unit.parent / 'goose-xmpp-bot-backup.timer')}
        before_calls = Path('/tmp/backup-systemctl.calls').read_bytes()
        refresh = "from pathlib import Path;from goose.maintenance import settings,install_launcher;m,p=settings(Path('/opt/goose-xmpp-bot'));install_launcher(Path('/repo'),p,False)"
        run([venv / 'bin/python', '-c', refresh])
        assert all(p.read_bytes() == value for p, value in originals.items())
        assert Path('/tmp/backup-systemctl.calls').read_bytes() == before_calls
        assert not Path('/tmp/forbidden-shell').exists()
        assert 'goose-xmpp-bot.service' not in before_calls.decode()
        assert writer.poll() is None
        print('PASS root installed-wheel bootstrap: online copies, rotation, restore, stable CLI and preserved timer; app writer kept running')
    finally:
        Path('/tmp/backup-writer-stop').touch()
        writer.wait(timeout=5)


if __name__ == '__main__':
    main()
