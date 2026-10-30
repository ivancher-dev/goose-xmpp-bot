"""Настоящий локальный переход со старого updater 0.3.3 на backup 0.3.4.

Создаёт отдельную установку в /tmp; pip получает зависимости по lock с хешами.
Не использует root, systemd, XMPP, модель или данные семейного сервера.
Запускать из чистого коммита 0.3.4, не входит в обычный pytest.
"""

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile


def main():
    source = Path(__file__).resolve().parents[2]
    lab = Path(tempfile.mkdtemp(prefix='goose-backup-upgrade-', dir='/tmp'))
    repo, app, data, cfg = (lab / n for n in ('repo', 'app', 'data', 'config'))

    def run(args):
        result = subprocess.run([str(a) for a in args], cwd=lab, capture_output=True, text=True)
        (lab / 'last-command.log').write_text(result.stdout + result.stderr)
        assert result.returncode == 0, f'Команда не завершена: {lab}/last-command.log'
        return result.stdout.strip()

    target = run(['git', '-C', source, 'rev-parse', 'HEAD'])
    run(['git', 'clone', '--no-hardlinks', source, repo])
    run(['git', '-C', repo, 'switch', '--detach', 'e3dd59af744ff6450ff81d70131922055aad1a6b'])
    config = cfg / 'config.toml'
    flags = ['--local', '--prefix', app, '--config', config,
             '--data-dir', data, '--env-file', cfg / 'goose.env']
    print(f'Лаборатория: {lab}; установка прежнего 0.3.3', flush=True)
    run(['bash', repo / 'scripts/install.sh', *flags])
    assert run([app / 'venv/bin/goose-bot', '--version']) == '0.3.3'
    config.write_text(config.read_text().replace('/etc/goose-xmpp-bot/persona.md', str(cfg / 'persona.md')))
    (cfg / 'goose.env').write_text('GOOSE_XMPP_PASSWORD="fixture-password"\nGOOSE_LLM_API_KEY="fixture-key"\n')
    (cfg / 'persona.md').write_text('Личный синтетический образ, сохраняемый при обновлении.\n')
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

    def protected():
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (config, cfg / 'goose.env', cfg / 'persona.md', data / 'goose.sqlite3')}

    before = protected()
    updater = app / 'bin/goose-update'
    old_launcher = updater.read_bytes()
    assert not (app / 'bin/goose-backup').exists()
    run(['git', '-C', repo, 'switch', '--detach', target])
    print('Переход на 0.3.4 через уже установленный старый updater', flush=True)
    run([updater, '--ref', target])
    assert run([app / 'venv/bin/goose-bot', '--version']) == '0.3.4'
    assert updater.read_bytes() != old_launcher and protected() == before
    # Старый процесс updater ещё не знает о короткой backup-команде.
    assert not (app / 'bin/goose-backup').exists()
    run([app / 'venv/bin/goose-backup', '--prefix', app, 'enable'])
    command = app / 'bin/goose-backup'
    run([command, 'create'])
    backup_root = lab / 'app-backups'
    copies = list((backup_root / 'scheduled').glob('daily-*'))
    assert len(copies) == 2 and len(list(backup_root.glob('update-*'))) == 1
    for copy in copies:
        run([command, 'verify', copy])
        run([command, 'restore-test', copy])
    assert protected() == before
    preference = app / 'backup-settings.json'
    timer = app / 'goose-xmpp-bot-backup.timer'
    settings_before = (preference.read_bytes(), timer.read_bytes())
    assert json.loads(preference.read_text())['keep'] == 14
    print('Повторное обновление с настроенным копированием', flush=True)
    run([updater, '--ref', target])
    assert protected() == before and settings_before == (preference.read_bytes(), timer.read_bytes())
    run([command, 'status'])
    run([app / 'venv/bin/python', '-m', 'pip', 'check'])
    with sqlite3.connect(data / 'goose.sqlite3') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 4
        assert db.execute("SELECT grade FROM profiles WHERE jid='pupil2@example.org'").fetchone()[0] == 8
        assert db.execute("SELECT mode FROM dialogues WHERE jid='pupil2@example.org'").fetchone()[0] == 'auto'
        assert db.execute("SELECT mode FROM dialogues WHERE jid='pupil1@example.org'").fetchone()[0] == 'manual'
    print('PASS старый updater → 0.3.4 → enable/restore → повторное обновление; настройки и SQLite идентичны', flush=True)


if __name__ == '__main__':
    main()
