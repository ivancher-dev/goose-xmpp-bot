# Копия Гуся на домашнем Windows 11

Для текущей установки и входа на VDS по SSH как root. Команды работают с уже
созданной регулярной копией, не останавливают Гуся и не читают живую SQLite
для упаковки. Используются установленные на VDS goose-backup, tar, sha256sum
и OpenSSH/scp на Windows. Изменять приложение или его версию для этого не нужно.

Архив содержит конфиг, env, ключи OMEMO и память. Он передаётся по SSH и хранится
в личном LocalAppData, вне обычной синхронизации Documents/OneDrive. Сам tar.gz
не зашифрован: защита от чтения снятого диска обеспечивается включённым
шифрованием диска Windows (BitLocker/«Шифрование устройства»). Архив и его
содержимое не отправлять в чат или GitHub. Результат проверки SHA256 можно сообщить.

## 1. Подготовить архив в существующем SSH-сеансе VDS

Выполнить **на сервере, под root**. Выбирается самая новая регулярная копия;
goose-backup проверяет её до упаковки. Проверка и упаковка завершаются до замены
готового архива.
В exports остаются один готовый архив и его контрольная сумма; регулярные
копии и их ротация не меняются.

```bash
bash <<'GOOSE_EXPORT'
set -euo pipefail
goose_source=/var/backups/goose-xmpp-bot/scheduled
goose_exports=/var/backups/goose-xmpp-bot/exports
goose_snapshot=$(find "$goose_source" -mindepth 1 -maxdepth 1 -type d -name 'daily-*' -printf '%f\n' | LC_ALL=C sort | tail -n 1)
test -n "$goose_snapshot"
goose-backup verify "$goose_source/$goose_snapshot"
umask 077
test ! -L "$goose_exports"
install -d -m 0700 "$goose_exports"
goose_tmp=$(mktemp "$goose_exports/.export.XXXXXXXX")
trap 'rm -f -- "$goose_tmp" "$goose_tmp.sha256"' EXIT
tar -C "$goose_source" -czf "$goose_tmp" -- "$goose_snapshot"
sha256sum "$goose_tmp" | cut -d ' ' -f 1 > "$goose_tmp.sha256"
mv -f -- "$goose_tmp" "$goose_exports/goose-latest.tar.gz"
mv -f -- "$goose_tmp.sha256" "$goose_exports/goose-latest.tar.gz.sha256"
printf 'Архив готов: %s\n' "$goose_snapshot"
GOOSE_EXPORT
```

Если скачивание попало между заменой архива и его SHA256, проверка ниже
обнаружит несовпадение; после завершения экспорта скачивание можно повторить.

Этот блок не включает расписание экспорта: перед следующим ручным скачиванием
его повторяют, чтобы взять новую копию. Свежесть видна в имени выбранного снимка
и `goose-backup status`. Если копий нет, verify не прошёл или tar сообщил ошибку,
не продолжать скачивание как будто новая копия готова; нужен разбор ошибки.

## 2. Скачать и проверить в PowerShell на домашнем компьютере

Открыть **новое локальное окно PowerShell в Windows**, вне SSH-сеанса.
Сначала проверить наличие клиента: `Get-Command scp`. Если его нет, включить
«Клиент OpenSSH» в дополнительных компонентах Windows.

В начале блока вводится SSH-адрес в привычном виде `root@адрес`.
При нестандартном порте/ключе добавить обычные параметры scp в
`$gooseScpOptions`, например `@('-P', '2222')` или `@('-i', 'C:\путь\к\ключу')`.
Пустой список сохраняет стандартное поведение и настройки SSH клиента.
Проверка ключа сервера остаётся стандартной; отключать её не нужно.

```powershell
& {
    $ErrorActionPreference = 'Stop'
    $gooseServer = Read-Host 'SSH-адрес VDS (root@адрес)'
    if ($gooseServer -notmatch '^root@[^\s]+$') { throw 'Нужен адрес root@сервер без пробелов.' }
    $gooseScpOptions = @()
    $gooseFolder = Join-Path $env:LOCALAPPDATA 'GooseBackups'
    New-Item -ItemType Directory -Force -Path $gooseFolder | Out-Null
    $gooseStage = Join-Path $gooseFolder ('.partial-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $gooseStage | Out-Null
    & scp @gooseScpOptions "${gooseServer}:/var/backups/goose-xmpp-bot/exports/goose-latest.tar.gz*" $gooseStage
    if ($LASTEXITCODE -ne 0) { throw 'Скачивание не завершено. Прежние локальные копии сохранены.' }
    $gooseArchive = Join-Path $gooseStage 'goose-latest.tar.gz'
    $gooseExpected = (Get-Content -LiteralPath (Join-Path $gooseStage 'goose-latest.tar.gz.sha256') -Raw).Trim()
    $gooseActual = (Get-FileHash -LiteralPath $gooseArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($gooseExpected -notmatch '^[0-9a-f]{64}$' -or $gooseActual -ne $gooseExpected) {
        throw 'SHA256 не совпала. Файлы остались в .partial; готовой копии из них не создано.'
    }
    $gooseName = 'goose-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $gooseActual.Substring(0, 8) + '.tar.gz'
    $gooseFinal = Join-Path $gooseFolder $gooseName
    Move-Item -LiteralPath $gooseArchive -Destination $gooseFinal
    Move-Item -LiteralPath (Join-Path $gooseStage 'goose-latest.tar.gz.sha256') -Destination "${gooseFinal}.sha256"
    Remove-Item -LiteralPath $gooseStage -Recurse -Force
    Write-Host 'SHA256: OK'
    Write-Host "Сохранено: $gooseFinal"
}
```

На сервере пароль вводится только в обычном SSH/scp-запросе; в команды, файл
скрипта или переписку его не вставлять. Если подтверждается новый ключ сервера,
сверить его тем же способом, которым проверялось обычное SSH-подключение.

Скачивание идёт в отдельный .partial-каталог. Только после совпадения SHA256
архив получает постоянное уникальное имя; прежние успешные копии не заменяются.
При ошибке .partial не считать готовой копией. Новые скачивания получают
новый каталог. В этой первой инструкции локальной автоматической ротации нет:
до проверки восстановления старые успешные архивы сохраняются.

## Что подтверждает результат

`SHA256: OK` означает, что на компьютере есть архив с теми же байтами, что
у подготовленного на VDS экспорта. Это проверка передачи и целостности,
а не отдельное восстановление Windows-копии в приложение. Сумма не заменяет
аутентификацию источника: источник здесь подтверждается SSH.

Внутри tar сохранены исходные Linux-права и manifest, поэтому его не нужно
распаковывать на NTFS для хранения. Следующий шаг после успешного скачивания —
вернуть этот архив в отдельный приватный каталог Linux и выполнить
`goose-backup verify` / `restore-test` для извлечённого снимка. Рабочая SQLite
и основной OMEMO-процесс при этом не подменяются и не дублируются.

Регулярные календарные копии на VDS продолжаются независимо от того, включён
ли домашний компьютер. Автоматическое получение при включении Windows и локальная
ротация — последующая настройка после первой проверенной внешней копии.
До подтверждения владельцем скачивания внешняя копия не отмечается выполненной.
