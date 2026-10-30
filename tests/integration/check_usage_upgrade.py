"""Настоящий updater 0.3.10/схема 5 → текущий код/схема 6 на временной установке.

Запуск из чистого коммита. Сеть только для pip с lock/хешами; API и XMPP подставные.
Не требует root или systemd. Не входит в обычный pytest.
"""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import tomllib


def main():
    source = Path(__file__).resolve().parents[2]
    lab = Path(tempfile.mkdtemp(prefix='goose-usage-upgrade-', dir='/tmp'))
    repo, app, data, cfg = (lab/name for name in ('repo','app','data','config'))
    def run(args):
        result = subprocess.run([str(a) for a in args], cwd=lab, capture_output=True, text=True)
        (lab/'last-command.log').write_text(result.stdout+result.stderr)
        assert result.returncode == 0, f'Ошибка команды: {lab}/last-command.log'
        return result.stdout.strip()
    target = run(['git','-C',source,'rev-parse','HEAD'])
    version = tomllib.loads(run(['git','-C',source,'show',target+':pyproject.toml']))['project']['version']
    run(['git','clone','--no-hardlinks',source,repo])
    run(['git','-C',repo,'switch','--detach','4c200a96ba7304ea00e2ec0fa3b10c5de98261b0'])
    config = cfg/'config.toml'
    print(f'Лаборатория: {lab}; установка 0.3.10', flush=True)
    run(['bash',repo/'scripts/install.sh','--local','--prefix',app,'--config',config,
         '--data-dir',data,'--env-file',cfg/'goose.env'])
    config.write_text(config.read_text().replace('/etc/goose-xmpp-bot/persona.md',str(cfg/'persona.md')))
    (cfg/'goose.env').write_text('GOOSE_XMPP_PASSWORD="fixture-password"\nGOOSE_LLM_API_KEY="fixture-key"\n')
    (cfg/'persona.md').write_text('Синтетический сохраняемый образ.\n')
    seed = '''
import asyncio,sys
from pathlib import Path
from goose.config import load_config
from goose.storage import Database,OmemoStorage
async def main():
 c=load_config(Path(sys.argv[1]));db=Database(c.data_dir,c.jid)
 await db.initialize(c.users)
 await db.set_mode('pupil1@example.org','manual');await db.set_mode('pupil2@example.org','auto')
 await db.update_language('pupil2@example.org','enabled',True)
 await db.add_user_message('pupil2@example.org','Синтетическая память до обновления')
 await db.set_weather_location('pupil2@example.org',{'id':123,'name':'Fixture'})
 await OmemoStorage(db)._store('fixture-key',{'identity':'preserved','trust':'TRUSTED'})
asyncio.run(main())
'''
    run([app/'venv/bin/python','-c',seed,config])
    backup = app/'bin/goose-backup'
    run([backup,'enable'])
    old = next((lab/'app-backups/scheduled').glob('daily-*'))
    digest = hashlib.sha256((old/'data/goose.sqlite3').read_bytes()).hexdigest()
    keep = (config,cfg/'goose.env',cfg/'persona.md',app/'backup-settings.json',
            app/'goose-xmpp-bot-backup.timer',app/'goose-xmpp-bot-backup.service')
    kept = {p:p.read_bytes() for p in keep}
    def old_rows():
        with closing(sqlite3.connect(data/'goose.sqlite3')) as db:
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0]!='api_usage']
            return {name:list(db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')) for name in names}
    before = old_rows()
    run(['git','-C',repo,'switch','--detach',target])
    print(f'Проверка updater 0.3.10 → {version}',flush=True)
    run([app/'bin/goose-update','--ref',target])
    assert run([app/'venv/bin/goose-bot','--version']) == version
    initialize = '''
import asyncio,sys
from pathlib import Path
from goose.config import load_config
from goose.storage import Database
c=load_config(Path(sys.argv[1]));asyncio.run(Database(c.data_dir,c.jid).initialize(c.users))
'''
    run([app/'venv/bin/python','-c',initialize,config])
    assert old_rows()==before and all(p.read_bytes()==value for p,value in kept.items())
    probe = '''
import asyncio,os,sys,time
from dataclasses import replace
from pathlib import Path
import httpx
from goose.commands import Commands
from goose.config import load_config
from goose.model import OpenAICompatible
from goose.storage import Database
from goose.usage import usage_scope
async def main():
 c=load_config(Path(sys.argv[1]));db=Database(c.data_dir,c.jid);stamp=time.time()
 os.environ['GOOSE_LLM_API_KEY']='fixture-key'
 llm=replace(c.llm,enabled=True,base_url='https://api.example.org/v1',model='fixture-model')
 transport=httpx.MockTransport(lambda _: httpx.Response(200,json={'choices':[{'message':{'content':'Синтетический ответ.'}}],'usage':{'prompt_tokens':10,'completion_tokens':3,'total_tokens':13}}))
 model=OpenAICompatible(llm,transport=transport,wall_clock=lambda:stamp)
 try:
  with usage_scope(db,'pupil2@example.org'): assert await model.complete([])=='Синтетический ответ.'
 finally: await model.close()
 command=Commands(c,db,clock=lambda:stamp)
 report=await command.handle('parent@example.org','!admin usage pupil2@example.org month')
 assert 'попыток API: 1' in report and 'всего — 13' in report
 assert 'владельцу' in await command.handle('pupil2@example.org','!admin usage')
 assert (await db.dialogue('pupil1@example.org'))['mode']=='manual'
 assert (await db.language_preferences('pupil2@example.org'))['enabled']
 assert await db.call(lambda s:s.execute('PRAGMA user_version').fetchone()[0])==6
asyncio.run(main())
'''
    run([app/'venv/bin/python','-c',probe,config])
    run([backup,'restore-test',old])
    assert hashlib.sha256((old/'data/goose.sqlite3').read_bytes()).hexdigest()==digest
    with closing(sqlite3.connect(old/'data/goose.sqlite3')) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==5
    run([backup,'create'])
    latest=max((lab/'app-backups/scheduled').glob('daily-*'),key=lambda p:p.name)
    assert json.loads((latest/'manifest.json').read_text())['schema']==6
    with closing(sqlite3.connect(latest/'data/goose.sqlite3')) as db:
        assert db.execute('SELECT prompt_tokens,completion_tokens,total_tokens FROM api_usage').fetchone()==(10,3,13)
    run([backup,'restore-test',latest])
    run([app/'venv/bin/python','-m','pip','check'])
    assert all(p.read_bytes()==value for p,value in kept.items())
    print('PASS схема 5 → 6; данные/ключи/режимы/таймер сохранены; учёт и права работают; restore схем 5/6 успешен',flush=True)


if __name__=='__main__':
    main()
