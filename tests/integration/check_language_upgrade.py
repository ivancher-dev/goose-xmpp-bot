"""Настоящий переход 0.3.4/схема 4 → 0.3.5/схема 5 на временных данных.

Запуск из чистого коммита. /tmp, локальный Git origin, pip по lock с хешами.
Не требует root, XMPP, модели или systemd. Не входит в обычный pytest.
"""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile


def main():
    source = Path(__file__).resolve().parents[2]
    lab = Path(tempfile.mkdtemp(prefix='goose-language-upgrade-', dir='/tmp'))
    repo, app, data, cfg = (lab / name for name in ('repo', 'app', 'data', 'config'))
    def run(args):
        result = subprocess.run([str(a) for a in args], cwd=lab, capture_output=True, text=True)
        (lab / 'last-command.log').write_text(result.stdout + result.stderr)
        assert result.returncode == 0, f'Ошибка команды: {lab}/last-command.log'
        return result.stdout.strip()
    target = run(['git', '-C', source, 'rev-parse', 'HEAD'])
    run(['git', 'clone', '--no-hardlinks', source, repo])
    run(['git', '-C', repo, 'switch', '--detach', '8d8fb38'])
    config = cfg / 'config.toml'
    flags = ['--local', '--prefix', app, '--config', config,
             '--data-dir', data, '--env-file', cfg / 'goose.env']
    print(f'Лаборатория: {lab}; установка прежнего 0.3.4', flush=True)
    run(['bash', repo / 'scripts/install.sh', *flags])
    config.write_text(config.read_text().replace('/etc/goose-xmpp-bot/persona.md', str(cfg / 'persona.md')))
    (cfg / 'goose.env').write_text('GOOSE_XMPP_PASSWORD="fixture-password"\nGOOSE_LLM_API_KEY="fixture-key"\n')
    (cfg / 'persona.md').write_text('Пользовательский образ, сохраняемый при обновлении.\n')
    seed = '''
import asyncio,sys
from pathlib import Path
from goose.config import load_config
from goose.storage import Database,OmemoStorage
async def main():
 c=load_config(Path(sys.argv[1]));db=Database(c.data_dir,c.jid)
 await db.initialize(c.users)
 await db.update_profile('pupil2@example.org','grade',8)
 await db.set_mode('pupil2@example.org','auto');await db.set_mode('pupil1@example.org','manual')
 await db.add_user_message('pupil2@example.org','Синтетическая история')
 await db.set_weather_location('pupil2@example.org',{'id':123,'name':'Fixture'})
 await OmemoStorage(db)._store('fixture-key',{'persistent':True})
asyncio.run(main())
'''
    run([app / 'venv/bin/python', '-c', seed, config])
    command = app / 'bin/goose-backup'
    run([command, 'enable'])
    old_copy = next((lab / 'app-backups/scheduled').glob('daily-*'))
    old_digest = hashlib.sha256((old_copy / 'data/goose.sqlite3').read_bytes()).hexdigest()
    kept_paths = (config, cfg / 'goose.env', cfg / 'persona.md', app / 'backup-settings.json',
                  app / 'goose-xmpp-bot-backup.timer', app / 'goose-xmpp-bot-backup.service')
    kept = {p: p.read_bytes() for p in kept_paths}
    def old_tables():
        with closing(sqlite3.connect(data / 'goose.sqlite3')) as db:
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                     if r[0] != 'language_feedback']
            return {name: list(db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')) for name in names}
    before = old_tables()
    run(['git', '-C', repo, 'switch', '--detach', target])
    print('Обновление 0.3.4 → 0.3.5 с пробной миграцией', flush=True)
    run([app / 'bin/goose-update', '--ref', target])
    assert run([app / 'venv/bin/goose-bot', '--version']) == '0.3.5'
    # В --local System.start выполняет doctor, а не запускает рабочий XMPP.
    # Воспроизводим именно инициализацию БД первого настоящего run.
    initialize = '''
import asyncio,sys
from pathlib import Path
from goose.config import load_config
from goose.storage import Database
c=load_config(Path(sys.argv[1]))
asyncio.run(Database(c.data_dir,c.jid).initialize(c.users))
'''
    run([app / 'venv/bin/python', '-c', initialize, config])
    assert old_tables() == before and all(p.read_bytes() == value for p, value in kept.items())
    preferences = '''
import asyncio,sys
from pathlib import Path
from goose.commands import Commands
from goose.config import load_config
from goose.storage import Database
async def main():
 c=load_config(Path(sys.argv[1]));db=Database(c.data_dir,c.jid);commands=Commands(c,db)
 assert 'включены' in await commands.handle('parent@example.org','!admin language pupil1@example.org on')
 await commands.handle('parent@example.org','!admin language pupil1@example.org mode=detailed')
 await commands.handle('parent@example.org','!admin language pupil1@example.org max_notes=3')
 assert 'владельцу' in await commands.handle('pupil1@example.org','!admin language pupil2@example.org on')
 await Database(c.data_dir,c.jid).initialize(c.users)
 assert await db.language_preferences('pupil1@example.org') == {'enabled':True,'mode':'detailed','max_notes':3}
 assert not (await db.language_preferences('pupil2@example.org'))['enabled']
asyncio.run(main())
'''
    run([app / 'venv/bin/python', '-c', preferences, config])
    run([command, 'restore-test', old_copy])
    assert hashlib.sha256((old_copy / 'data/goose.sqlite3').read_bytes()).hexdigest() == old_digest
    with closing(sqlite3.connect(old_copy / 'data/goose.sqlite3')) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 4
    run([command, 'create'])
    current = max((lab / 'app-backups/scheduled').glob('daily-*'), key=lambda p: p.name)
    assert json.loads((current / 'manifest.json').read_text())['schema'] == 5
    run([command, 'restore-test', current])
    with closing(sqlite3.connect(current / 'data/goose.sqlite3')) as db:
        assert db.execute('SELECT enabled,mode,max_notes FROM language_feedback WHERE jid=?',
                          ('pupil1@example.org',)).fetchone() == (1, 'detailed', 3)
    run([app / 'venv/bin/python', '-m', 'pip', 'check'])
    print('PASS схема 4 → 5; старые данные, образ и таймер сохранены; независимые настройки и restore схем 4/5', flush=True)


if __name__ == '__main__':
    main()
