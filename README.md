# Гусь — семейный XMPP-помощник

Реализована диагностическая основа первого этапа: Python 3.12, Slixmpp, SQLite, legacy OMEMO, установка и systemd. Сервис предназначен для **отдельного тестового XMPP-аккаунта**. Владелец подтвердил живой зашифрованный диагностический обмен с Gajim и Conversations, в том числе после перезапуска сервиса. Остальные сценарии [живой приёмки](docs/TESTING_XMPP.md) ещё проверяются; этап не объявляется полностью принятым.

Гусь отвечает только на зашифрованные диагностические команды от разрешённых пользователей с проверенными устройствами. Свободные сообщения остаются без автоответа. Модель, расписание, память переписки, автоматический ручной перехват и перенос на основной аккаунт не входят в эту версию.

Работают `!help`, `!ping`, `!admin status`, просмотр/изменение профилей. На `!ping` Гусь отвечает `Га-Га-Га!`. `!echo` выключен по умолчанию и доступен только владельцу при явном включении. Права определяются нормализованным bare JID. Класс и `schedule_class_id` независимы; изменения профилей сохраняются при перезапуске.

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

Подробные команды для Ubuntu 24.04, настройки тестового аккаунта, доверия устройств и безопасного повторного выполнения: [docs/INSTALLATION.md](docs/INSTALLATION.md).

```bash
# Из чистого checkout заранее выбранного коммита; по умолчанию только подготовка файлов.
sudo bash scripts/install.sh
# После заполнения /etc/goose-xmpp-bot/config.toml и защищённого goose.env:
sudo systemctl enable --now goose-xmpp-bot.service
```

Установщик не изменяет Prosody/nginx/firewall. Код и virtualenv находятся в `/opt/goose-xmpp-bot`, конфигурация — в `/etc/goose-xmpp-bot`, данные — в `/var/lib/goose-xmpp-bot`. Файл `goose.sqlite3` содержит профили, ключи OMEMO, сессии, доверие и ограниченный журнал хешей доставки. Тексты переписки не сохраняются. Ключи и рабочие данные не должны попадать в Git или обычные журналы.

TLS обязателен. Отправка и приём в этой версии ограничены `eu.siacs.conversations.axolotl` (legacy OMEMO). В slixmpp-omemo 2.2.0 не завершена обработка SCE для `urn:xmpp:omemo:2`; такое сообщение не вызывает открытый ответ. Слепое доверие отключено. Версии, ограничения и источники API приведены в [отчёте первого этапа](docs/STAGE1_REPORT.md).

## Документы

| Файл | Содержание |
| --- | --- |
| [docs/SPEC.md](docs/SPEC.md) | Целевые требования всего проекта |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Этапы внедрения |
| [docs/tasks/01-xmpp-foundation.md](docs/tasks/01-xmpp-foundation.md) | Задание первого этапа |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Настройки, профили и команды по этапам |
| [config/config.example.toml](config/config.example.toml) | Поддерживаемый пример с вымышленными аккаунтами |
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | Установка, диагностика, доверие и данные |
| [docs/TESTING_XMPP.md](docs/TESTING_XMPP.md) | Живая приёмка Gajim/Conversations |
| [docs/STAGE1_REPORT.md](docs/STAGE1_REPORT.md) | Проверки и ограничения реализации |
| [docs/SCHEDULE_SOURCE.md](docs/SCHEDULE_SOURCE.md) | Будущая интеграция расписания |
| [docs/CODEX_START.md](docs/CODEX_START.md) | Исходная постановка разработки |
| [AGENTS.md](AGENTS.md) | Правила работы |
