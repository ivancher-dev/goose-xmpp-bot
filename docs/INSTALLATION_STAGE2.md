# Установка этапа 2 и пилот с владельцем

Эта инструкция дополняет [INSTALLATION.md](INSTALLATION.md) для версии 0.2.0.
Пути ниже стандартные; при собственной раскладке замените их заранее.
Используйте отдельный тестовый аккаунт. Провайдер, модель и бюджет выбираются
владельцем перед платной проверкой. Дети остаются в manual на время пилота.

## 1. Зафиксировать версию и сохранить данные

Загрузите проверенный коммит PR, затем выполните из чистого checkout:

```bash
git fetch origin
# COMMIT_SHA замените полным SHA проверенного коммита.
git switch --detach COMMIT_SHA
git status --short
sudo systemctl stop goose-xmpp-bot.service
sudo install -d -m 0700 /var/backups/goose-xmpp-bot
goose_backup_dir=$(sudo mktemp -d /var/backups/goose-xmpp-bot/stage2.XXXXXX)
sudo cp -a /var/lib/goose-xmpp-bot "$goose_backup_dir/data"
sudo cp -a /etc/goose-xmpp-bot "$goose_backup_dir/config"
```

Сохраните путь `goose_backup_dir` локально. Он содержит секреты и ключи;
копия остаётся вне checkout. Если файл образа уже лежит вне каталога настроек,
сохраните и его. Останавливается только бот; Prosody и остальные службы не меняются.

## 2. Установить с выключенной моделью

Сохраните `llm.enabled=false`, `schedule.enabled=false` и `default_mode="manual"`.

```bash
sudo bash scripts/install.sh
sudo systemctl start goose-xmpp-bot.service
sudo systemctl is-active goose-xmpp-bot.service
```

Установщик сохраняет существующие конфиг, env и данные, создаёт отсутствующий
`/etc/goose-xmpp-bot/persona.md`. Его существующее содержимое сохраняется.
Локальный doctor не мигрирует базу. Первый `run` или управление устройствами
версии 0.2.0 выполняет атомарную миграцию схемы 1 → 2: добавляются память и режимы,
существующие OMEMO, доверие, профили и журнал дедупликации сохраняются.

Из управляющего аккаунта проверьте зашифрованный `!ping` и `!admin status`.
Ответ на ping прежний: `Га-Га-Га!`. В статусе — этап 2 и выключенная модель.

## 3. Настроить модель и профиль пилота

Остановите бот перед редактированием и затем заполните настройки:

```bash
sudo systemctl stop goose-xmpp-bot.service
sudo nano /etc/goose-xmpp-bot/config.toml
sudo nano /etc/goose-xmpp-bot/goose.env
sudo nano /etc/goose-xmpp-bot/persona.md
```

В существующем разделе `[llm]`:

```toml
enabled = true
provider = "openai_compatible"
base_url = "https://api.example.org/v1" # заменить адресом выбранного провайдера
model = "MODEL_ID"                   # заменить реальным идентификатором
api_key_env = "GOOSE_LLM_API_KEY"
persona_file = "/etc/goose-xmpp-bot/persona.md"
timeout_seconds = 30
max_output_tokens = 700
max_concurrent_requests = 2
requests_per_minute = 12
max_attempts = 2
```

Адаптер использует `POST <base_url>/chat/completions`, Bearer-авторизацию,
`messages`, `max_tokens` и текстовый `choices[0].message.content`.
Responses API и модели с несовместимыми параметрами требуют другого адаптера.
TLS обязателен; перенаправления не выполняются. Условия и совместимость
конкретного сервиса не считаются проверенными до пилота.

В защищённый env через редактор добавьте `GOOSE_LLM_API_KEY=...`.
Ключ не передаётся в чате или аргументом shell. Для новых установок пустая
переменная уже есть в шаблоне. Старый env установщик не дополняет и не перезаписывает.

Для auto нужен полный профиль целевого аккаунта. Если профиля владельца ещё нет,
добавьте в конфиг тестовый профиль **его разрешённого управляющего JID**:

```toml
[users."parent@example.org"] # заменить своим JID из admin_jids/allowed_jids
grade = 5                    # учебный уровень для тестовых объяснений
academic_year = "2026-2027"
schedule_class_id = "pilot"
explanation_style = "step_by_step_with_reasoning"
```

Начальные профили вставляются только при отсутствии; существующие значения из
SQLite сохраняются. Образ Гуся редактируется в файле до 8192 UTF-8 байт; новые
запросы читают его заново. Перезапуск ради изменения только текста образа не нужен.

## 4. Диагностика и один явный запрос API

Локальная диагностика с защищённым окружением, без сетевых запросов:

```bash
sudo systemd-run --pipe --wait --collect --uid=goose-xmpp-bot \
  --property=WorkingDirectory=/opt/goose-xmpp-bot \
  --property=EnvironmentFile=/etc/goose-xmpp-bot/goose.env \
  /opt/goose-xmpp-bot/venv/bin/goose-bot doctor \
  --config /etc/goose-xmpp-bot/config.toml
```

Следующая команда **делает один запрос, который может тарифицироваться**:

```bash
sudo systemd-run --pipe --wait --collect --uid=goose-xmpp-bot \
  --property=WorkingDirectory=/opt/goose-xmpp-bot \
  --property=EnvironmentFile=/etc/goose-xmpp-bot/goose.env \
  /opt/goose-xmpp-bot/venv/bin/goose-bot doctor \
  --config /etc/goose-xmpp-bot/config.toml --api
```

Проверка ограничена 16 выходными токенами и одной попыткой; текст ответа и ключ
не печатаются. XMPP и OMEMO эта команда не затрагивает. При отказе исправьте
настройки до включения auto.

## 5. Пилот в диалоге владельца

```bash
sudo systemctl start goose-xmpp-bot.service
sudo journalctl -u goose-xmpp-bot.service -n 30 --no-pager
```

Все команды ниже отправляются владельцем в XMPP с OMEMO. Подставьте собственный
bare JID вместо `parent@example.org`:

1. `!admin mode parent@example.org` — начальный manual.
2. `!admin mode parent@example.org auto` — явное включение своего диалога.
3. Отправьте вымышленный учебный вопрос, получите один ответ, затем уточнение.
4. `!admin mode parent@example.org manual` — остановка ожидающих ответов.
5. `!context clear` — очистка своего контекста; профиль остаётся.

До этапа 3 обычная ручная отправка с другого устройства Гуся **не включает паузу**.
Для ручного ответа сначала включите manual и дождитесь подтверждения команды.
Уже поставленное в XMPP-очередь сообщение не отзывается.

После перезапуска или переподключения сохранённый auto остаётся на паузе.
`!admin mode parent@example.org` показывает её; повторная команда с `auto`
явно разрешает новый обмен. Очередь старых заданий не восстанавливается.

При включённой модели сохраняются входящие свободные сообщения разрешённых
пользователей, включая manual, и успешно поставленные ответы модели. Ручные
исходящие сообщения других устройств ещё не попадают в память. История ограничена
500 ходами на диалог, сроком хранения и окном API; автоматические ответы детям
требуют отдельного включения владельцем после пилота.

OMEMO защищает XMPP до устройства бота. Контекст, отправленный в HTTPS API,
доступен выбранному провайдеру. Очистка у Гуся не удаляет архив Prosody, данные
устройств, провайдера и резервные копии. Сценарии: [TESTING_MODEL.md](TESTING_MODEL.md).

## Совместимость и возврат версии

Версия 0.1.0 отклоняет схему 2. Возврат только исходников не является откатом.
Автоматический откат относится к этапу 5. При проблеме остановите бот и сохраните
текущую базу для исправления; старую копию OMEMO нельзя подменять в работающем
процессе. Восстановление копии требует отдельного выбора владельца, совместимой
версии и учёта потери данных/состояния сессий после даты копирования.
