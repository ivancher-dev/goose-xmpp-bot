# Гусь — семейный XMPP-помощник

Версия 0.2.2 поддерживает погоду Open-Meteo или wttr.in и свежие заголовки из настроенных RSS/Atom-лент поверх модели, раздельной памяти и режимов manual/auto версии 0.2.0. Основа: Python 3.12, Slixmpp, SQLite и legacy OMEMO. Сервис предназначен для **отдельного тестового XMPP-аккаунта**. В живом пилоте 0.2.0 подтверждены миграция базы, API Alice AI LLM Flash и первый учебный ответ владельцу. [Пилот модели](docs/TESTING_MODEL.md), [транспортная приёмка](docs/TESTING_XMPP.md) и [живые погода/новости](docs/WEATHER_NEWS.md) ещё не завершены.

Гусь принимает только разрешённых пользователей с доверенными устройствами и OMEMO. Модель выключена по умолчанию; новый диалог начинается в manual. Включение модели и auto — явные действия владельца. Расписание, изображения, автоматический ручной перехват, чтение MAM и перенос на основной аккаунт остаются следующими этапами.

Работают `!help`, `!ping`, `!admin status`, просмотр/изменение профилей, `!admin mode <bare_jid> [manual|auto]` и `!context clear`. На `!ping` Гусь отвечает `Га-Га-Га!`. `!echo` выключен по умолчанию и доступен только владельцу при явном включении. Права определяются нормализованным bare JID. Класс и `schedule_class_id` независимы; изменения профилей сохраняются при перезапуске.

manual, очистка контекста и изменение профиля отменяют ожидающие ответы модели своего диалога. Поздний результат API проходит проверку ревизии перед отправкой. После подключения сохранённый auto ждёт повторного подтверждения владельца: восстановление ручной переписки относится к этапу 3. Перед ручным ответом сначала включите manual и дождитесь подтверждения; уже отправленное сообщение не отзывается.

Погода и новости по умолчанию отключены. После включения работают и в manual, без модели:
`!weather city Москва` сохраняет свой город, `!weather` показывает погоду,
`погода в Нью-Йорке` делает разовый запрос, `!weather tomorrow` даёт прогноз.
`!news` выводит до пяти свежих заголовков с датами и ссылками из заданных владельцем лент.
Источник погоды выбирается явно через `weather.provider`: `open_meteo` (по умолчанию) или `wttr`.
Модель не придумывает и не пересказывает эти данные. [Настройка и установка 0.2.2](docs/WEATHER_NEWS.md).

Команды принимаются напрямую и через проверенные входящие Carbons, если исходное сообщение получил другой клиент тестового аккаунта. Обе доставки проходят общую дедупликацию, allowlist и проверку устройства OMEMO. Исходящие копии и архив команды не запускают.

## Разработка и офлайн-проверки

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python -m pytest -q
bash -n scripts/install.sh scripts/doctor.sh
```

Тесты используют временные данные и подставной транспорт, запрещают исходящие соединения и не требуют секретов или root. Они включают реальное шифрование и повторное открытие хранилища, но не заменяют проверку с клиентами. Зависимости зафиксированы с SHA-256; при обычной установке lock-файлы не пересоздаются. Для намеренного обновления: `uv pip compile --generate-hashes requirements.in -o requirements.lock` и аналогично для `requirements-dev.in`, затем проверки API и тесты.

## Установка и запуск

Базовая установка и доверие устройств: [docs/INSTALLATION.md](docs/INSTALLATION.md). **Переход с 0.1.0, резервная копия перед схемой 2 и настройка API:** [docs/INSTALLATION_STAGE2.md](docs/INSTALLATION_STAGE2.md).

```bash
# Из чистого checkout заранее выбранного коммита; по умолчанию только подготовка файлов.
sudo bash scripts/install.sh
# После заполнения /etc/goose-xmpp-bot/config.toml и защищённого goose.env:
sudo systemctl enable --now goose-xmpp-bot.service
```

Установщик не изменяет Prosody/nginx/firewall. Код и virtualenv находятся в `/opt/goose-xmpp-bot`, конфигурация — в `/etc/goose-xmpp-bot`, данные — в `/var/lib/goose-xmpp-bot`. SQLite схемы 3 содержит сохранённые города, профили, OMEMO, доверие, хеши доставки, режимы и ограниченную память. При включённой модели сохраняются входящие свободные сообщения и ответы модели; при выключенной новые тексты не сохраняются. Старую память можно удалить через `!context clear`; срок хранения продолжает действовать. Ключи и рабочие данные не должны попадать в Git или обычные журналы.

TLS обязателен. Отправка и приём в этой версии ограничены `eu.siacs.conversations.axolotl` (legacy OMEMO). В slixmpp-omemo 2.2.0 не завершена обработка SCE для `urn:xmpp:omemo:2`; такое сообщение не вызывает открытый ответ. Слепое доверие отключено. Версии, ограничения и источники API приведены в [отчёте первого этапа](docs/STAGE1_REPORT.md).

## Документы

| Файл | Содержание |
| --- | --- |
| [docs/SPEC.md](docs/SPEC.md) | Целевые требования всего проекта |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Этапы внедрения |
| [docs/tasks/01-xmpp-foundation.md](docs/tasks/01-xmpp-foundation.md) | Задание первого этапа |
| [docs/tasks/02-model-memory.md](docs/tasks/02-model-memory.md) | Задание второго этапа |
| [docs/tasks/02a-weather-news.md](docs/tasks/02a-weather-news.md) | Расширение: погода и новости |
| [docs/WEATHER_NEWS.md](docs/WEATHER_NEWS.md) | Установка 0.2.2, команды и проверки источников |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Настройки, профили и команды по этапам |
| [config/config.example.toml](config/config.example.toml) | Поддерживаемый пример с вымышленными аккаунтами |
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | Установка, диагностика, доверие и данные |
| [docs/INSTALLATION_STAGE2.md](docs/INSTALLATION_STAGE2.md) | Миграция, настройка модели и пилот |
| [docs/TESTING_XMPP.md](docs/TESTING_XMPP.md) | Живая приёмка Gajim/Conversations |
| [docs/STAGE1_REPORT.md](docs/STAGE1_REPORT.md) | Проверки и ограничения реализации |
| [docs/TESTING_MODEL.md](docs/TESTING_MODEL.md) | Живая приёмка модели и режимов |
| [docs/STAGE2_REPORT.md](docs/STAGE2_REPORT.md) | Проверки и ограничения этапа 2 |
| [docs/SCHEDULE_SOURCE.md](docs/SCHEDULE_SOURCE.md) | Будущая интеграция расписания |
| [docs/CODEX_START.md](docs/CODEX_START.md) | Исходная постановка разработки |
| [AGENTS.md](AGENTS.md) | Правила работы |
