"""Установленное обновление с выбранного baseline → текущий код/схема 8.

Запуск из чистого коммита. Сеть только для pip с lock/хешами; API подставной,
XMPP и системный systemd не запускаются. Данные целиком синтетические.
"""

from contextlib import closing
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import tomllib


def main(baseline_ref='5f6d26030ba6306a132b51e7ae7bcfe736c54264'):
    source = Path(__file__).resolve().parents[2]
    lab = Path(tempfile.mkdtemp(prefix='goose-takeover-upgrade-', dir='/tmp'))
    repo, app, data, cfg = (lab/name for name in ('repo','app','data','config'))
    def run(args):
        result = subprocess.run([str(a) for a in args], cwd=lab, capture_output=True, text=True)
        (lab/'last-command.log').write_text(result.stdout+result.stderr)
        assert result.returncode == 0, f'Ошибка команды: {lab}/last-command.log'
        return result.stdout.strip()
    target = run(['git','-C',source,'rev-parse','HEAD'])
    version = tomllib.loads(run(['git','-C',source,'show',target+':pyproject.toml']))['project']['version']
    baseline_version = tomllib.loads(run(['git','-C',source,'show',baseline_ref+':pyproject.toml']))['project']['version']
    run(['git','clone','--no-hardlinks',source,repo])
    run(['git','-C',repo,'switch','--detach',baseline_ref])
    config = cfg/'config.toml'
    print(f'Лаборатория: {lab}; установка {baseline_version}', flush=True)
    run(['bash',repo/'scripts/install.sh','--local','--prefix',app,'--config',config,
         '--data-dir',data,'--env-file',cfg/'goose.env'])
    config.write_text(config.read_text().replace('/etc/goose-xmpp-bot/persona.md',str(cfg/'persona.md'))
                      .replace('context_token_budget = 9000', 'context_token_budget = 6000'))
    (cfg/'goose.env').write_text('GOOSE_XMPP_PASSWORD="fixture-password"\nGOOSE_LLM_API_KEY="fixture-key"\n')
    (cfg/'persona.md').write_text(('Дружелюбный учебный помощник. ' * 100).encode()[:4029].decode('utf-8', errors='ignore'))
    seed = '''
import asyncio,sys,time
from pathlib import Path
from goose.config import load_config
from goose.storage import Database,OmemoStorage
async def main():
 c=load_config(Path(sys.argv[1]));db=Database(c.data_dir,c.jid)
 await db.initialize(c.users)
 await db.set_mode('pupil1@example.org','manual');await db.set_mode('pupil2@example.org','auto')
 await db.update_language('pupil2@example.org','enabled',True)
 await db.add_user_message('pupil2@example.org','Синтетическая память до обновления')
 for i in range(12):
  turn=await db.add_user_message('pupil2@example.org',f'Старая тема {i}: '+'я'*150)
  await db.add_assistant_message('pupil2@example.org',turn,'Старое пояснение: '+'б'*280)
 await db.set_weather_location('pupil2@example.org',{'id':123,'name':'Fixture'})
 await OmemoStorage(db)._store('fixture-key',{'identity':'preserved','trust':'TRUSTED'})
 await db.start_api_usage('before','before-call','pupil2@example.org','https://api.example.org/v1','fixture-model',1,time.time())
 await db.finish_api_usage('before','success',(100,20,120),time.time())
asyncio.run(main())
'''
    run([app/'venv/bin/python','-c',seed,config])
    with closing(sqlite3.connect(data/'goose.sqlite3')) as db:
        baseline_schema = db.execute('PRAGMA user_version').fetchone()[0]
        baseline_tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    backup = app/'bin/goose-backup'
    run([backup,'enable'])
    old = next((lab/'app-backups/scheduled').glob('daily-*'))
    digest = hashlib.sha256((old/'data/goose.sqlite3').read_bytes()).hexdigest()
    keep = (config,cfg/'goose.env',cfg/'persona.md',app/'backup-settings.json',
            app/'goose-xmpp-bot-backup.timer',app/'goose-xmpp-bot-backup.service')
    kept = {p:p.read_bytes() for p in keep}
    def old_rows():
        with closing(sqlite3.connect(data/'goose.sqlite3')) as db:
            return {name:list(db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')) for name in baseline_tables}
    before = old_rows()
    run(['git','-C',repo,'switch','--detach',target])
    print(f'Проверка updater {baseline_version} → {version}',flush=True)
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
import asyncio,json,os,sys,time
from dataclasses import replace
from datetime import datetime,timezone
from pathlib import Path
import httpx
from goose.commands import Commands
from goose.config import load_config,WeatherConfig
from goose.dialogue import Dialogues,estimate_tokens
from goose.facts import Facts
from goose.model import OpenAICompatible
from goose.storage import Database
from goose.weather_alerts import WeatherAlerts
async def main():
 c=load_config(Path(sys.argv[1]));db=Database(c.data_dir,c.jid);now=[time.time()];sent=[]
 os.environ['GOOSE_LLM_API_KEY']='fixture-key'
 llm=replace(c.llm,enabled=True,base_url='https://api.example.org/v1',model='fixture-model')
 def response(request):
  messages=json.loads(request.content)['messages']
  if 'candidate_ids' in messages[-1]['content']:
   ids=json.loads(messages[-1]['content'])['candidate_ids'];text=json.dumps({'certain':True,'unanswered':ids})
  else:
   assert estimate_tokens(messages)<=c.memory.context_token_budget==6000
   combined=' '.join(m['content'] for m in messages)
   assert 'Старая тема' not in combined and 'Старое пояснение' not in combined
   assert 'Первая подсказка.' in combined and 'Вторая подсказка.' in combined
   text='Синтетическое продолжение'
  return httpx.Response(200,json={'choices':[{'message':{'content':text}}],'usage':{'prompt_tokens':10,'completion_tokens':3,'total_tokens':13}})
 model=OpenAICompatible(llm,transport=httpx.MockTransport(response))
 async def prepare(jid,text):
  def send(): sent.append((jid,text));return True
  return send
 engine=Dialogues(replace(c,llm=llm),db,model,prepare,clock=lambda:now[0],wall_clock=lambda:now[0],can_resume=lambda jid:True)
 await engine.initialize()
 try:
  await db.save_archive_cursor('pupil2@example.org',None,now[0],confirmed=True)
  await engine.set_mode('pupil2@example.org','auto')
  await engine.restore('pupil2@example.org',('manual',),'manual','Минутку',now[0],live=True)
  await engine.submit('pupil2@example.org','Новый синтетический вопрос')
  now[0]=time.time()
  await engine.restore('pupil2@example.org',('hint1',),'manual','Первая подсказка.',now[0],live=True)
  await engine.restore('pupil2@example.org',('hint2',),'manual','Вторая подсказка.',now[0],live=True)
  now[0]+=61;await engine.resume_takeovers()
  await asyncio.wait_for(asyncio.gather(*tuple(engine.tasks)),3)
  assert sent==[('pupil2@example.org','Синтетическое продолжение')]
  assert (await db.dialogue('pupil1@example.org'))['mode']=='manual'
  assert (await db.dialogue('pupil2@example.org'))['mode']=='auto'
  command=Commands(c,db,clock=lambda:now[0])
  report=await command.handle('parent@example.org','!admin usage pupil2@example.org month')
  assert 'попыток API: 3' in report and 'всего — 146' in report
  assert (await db.language_preferences('pupil2@example.org'))['enabled']
  assert await db.call(lambda s:s.execute('PRAGMA user_version').fetchone()[0])==8
  await engine.restore('pupil2@example.org',('pending-owner',),'manual','Незавершённая пауза',now[0],live=True)
  weather_now=[datetime(2026,11,3,4,50,tzinfo=timezone.utc)]
  wc=replace(c,weather=WeatherConfig(True))
  place={'id':123,'name':'Fixture','country':'','admin1':'','latitude':55.0,'longitude':37.0,'timezone':'Europe/Moscow'}
  await db.set_weather_location('pupil1@example.org',place)
  weather_calls=[]
  def weather_http(request):
   weather_calls.append(request.url.host)
   if request.url.host=='api.open-meteo.com': return httpx.Response(503)
   assert request.url.host=='wttr.in'
   return httpx.Response(200,json={'nearest_area':[{'latitude':'55','longitude':'37'}],
    'current_condition':[{'localObsDateTime':'2026-11-03 08:00','temp_C':'4','FeelsLikeC':'2','windspeedKmph':'25','weatherCode':'296'}],
    'weather':[{'date':'2026-11-03','mintempC':'3','maxtempC':'9','hourly':[
     {'time':'1200','chanceofrain':'70','windspeedKmph':'25','weatherCode':'296'}]}]})
  facts=Facts(wc,db,prepare,transport=httpx.MockTransport(weather_http),now=lambda:weather_now[0])
  alerts=WeatherAlerts(wc,db,facts,prepare,now=lambda:weather_now[0]);facts.alerts=alerts
  try:
   command=Commands(wc,db);command.facts=facts
   assert '08:00' in await command.handle('parent@example.org','!admin weather pupil1@example.org утром')
   weather_now[0]=datetime(2026,11,3,5,tzinfo=timezone.utc)
   await alerts.tick();await asyncio.gather(*list(alerts.jobs.values()))
   assert sent[-1][0]=='pupil1@example.org' and 'Доброе утро!' in sent[-1][1]
   assert 'зонт' in sent[-1][1] and 'шапку' in sent[-1][1]
   assert weather_calls==['api.open-meteo.com','wttr.in']
   await alerts.tick();await asyncio.gather(*list(alerts.jobs.values()))
   assert len(sent)==2 and (await db.dialogue('pupil1@example.org'))['mode']=='manual'
  finally: await alerts.close();await facts.close()
  os.environ['GOOSE_YANDEX_WEATHER_KEY']='fixture-weather-key'
  pool_calls=[]
  def pool_http(request):
   pool_calls.append(request)
   if request.url.host!='www.7timer.info': return httpx.Response(503)
   return httpx.Response(200,json={'product':'civil','init':'2026110300','dataseries':[
    {'timepoint':h,'temp2m':4,'wind10m':{'speed':3},'weather':'lightrainday','prec_type':'rain'}
    for h in range(0,73,3)]})
  pool=Facts(wc,db,prepare,transport=httpx.MockTransport(pool_http),now=lambda:weather_now[0])
  try:
   report=await pool.get_weather(place)
   assert report.source=='7Timer! (GFS)' and report.day=='2026-11-03'
   assert [r.url.host for r in pool_calls]==['api.open-meteo.com','api.weather.yandex.ru','wttr.in','api.met.no','www.7timer.info']
   assert all(('X-Yandex-Weather-Key' in r.headers)==(r.url.host=='api.weather.yandex.ru') for r in pool_calls)
  finally: await pool.close()
  window_now=datetime(2026,11,3,10,46,tzinfo=timezone.utc)
  window_data={'data':{'serverTime':window_now.isoformat(),'weatherByPoint':{
   'now':{'temperature':15,'feelsLike':15,'windSpeed':1,'condition':'CLEAR'},
   'forecast':{'days':[{'hours':[
    {'time':f'2026-11-03T{h:02}:00:00+03:00','temperature':-15 if h<10 else 15,
     'windSpeed':10 if h<10 else 1,'condition':'RAIN' if h<10 else 'CLEAR'} for h in range(24)]}]}}}}
  window=Facts(replace(c,weather=WeatherConfig(True,'yandex',())),db,prepare,
   transport=httpx.MockTransport(lambda request:httpx.Response(200,json=window_data)),now=lambda:window_now)
  try:
   report=await window.get_weather(place)
   assert report.forecast.minimum==15 and not report.forecast.rain_expected
   assert 'зонт' not in report.render(advice=True) and 'шапку' not in report.render(advice=True)
   assert 'По данным на 13:46: ясно.' in report.render() and 'UTC+03:00' in report.render()
   window_data['data']['weatherByPoint']['forecast']['days'][0]['hours'][18]['condition']='RAIN'
   report=await window.get_weather(place)
   assert 'Дождь в прогнозных интервалах: 18:00–19:00.' in report.render(advice=True)
   assert 'зонт' in report.render(advice=True)
  finally: await window.close()
 finally: await engine.close()
asyncio.run(main())
'''
    run([app/'venv/bin/python','-c',probe,config])
    run([backup,'restore-test',old])
    assert hashlib.sha256((old/'data/goose.sqlite3').read_bytes()).hexdigest()==digest
    with closing(sqlite3.connect(old/'data/goose.sqlite3')) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==baseline_schema
    run([backup,'create'])
    latest=max((lab/'app-backups/scheduled').glob('daily-*'),key=lambda p:p.name)
    assert json.loads((latest/'manifest.json').read_text())['schema']==8
    with closing(sqlite3.connect(latest/'data/goose.sqlite3')) as db:
        assert db.execute('SELECT SUM(total_tokens),COUNT(*) FROM api_usage').fetchone()==(146,3)
        assert db.execute('SELECT COUNT(*) FROM takeover_pauses').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM takeover_answers').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM weather_subscriptions').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM weather_deliveries').fetchone()[0]==1
    run([backup,'restore-test',latest])
    run([app/'venv/bin/python','-m','pip','check'])
    assert all(p.read_bytes()==value for p,value in kept.items())
    print(f'PASS схема {baseline_schema} → 8; ключи/настройки/статистика/таймер сохранены; длинный контекст/auto/продолжение работают; restore обеих копий успешен',flush=True)


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-ref', default='5f6d26030ba6306a132b51e7ae7bcfe736c54264')
    main(parser.parse_args().baseline_ref)
