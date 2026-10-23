#!/usr/bin/env bash
# Установка из заранее загруженного фиксированного checkout; сеть — только pip.
set -euo pipefail
umask 077
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
prefix=/opt/goose-xmpp-bot
config=/etc/goose-xmpp-bot/config.toml
data_dir=/var/lib/goose-xmpp-bot
env_file=/etc/goose-xmpp-bot/goose.env
local_install=false
start=false
while (($#)); do
    case "$1" in
        --prefix|--config|--data-dir|--env-file)
            (($# >= 2)) || { echo 'Не задан путь.' >&2; exit 2; }
            case "$1" in
                --prefix) prefix=$2;; --config) config=$2;;
                --data-dir) data_dir=$2;; --env-file) env_file=$2;;
            esac
            shift 2;;
        --local) local_install=true; shift;;
        --start) start=true; shift;;
        *) echo 'Допустимы --prefix, --config, --data-dir, --env-file, --local, --start.' >&2; exit 2;;
    esac
done
for path in "$prefix" "$config" "$data_dir" "$env_file"; do
    [[ "$path" =~ ^/[A-Za-z0-9_./-]+$ && "$path" != / ]] || {
        echo 'Нужны абсолютные пути без пробелов и специальных символов.' >&2; exit 2;
    }
done
if $local_install && $start; then
    echo '--local не поддерживает запуск systemd.' >&2; exit 2
fi
if ! $local_install && ((EUID != 0)); then
    echo 'Для systemd-установки нужен root; для изолированной проверки используйте --local.' >&2; exit 2
fi
python_bin=${GOOSE_PYTHON:-python3.12}
"$python_bin" -c 'import sys; assert sys.version_info[:2] == (3,12), "Требуется Python 3.12"'
git -C "$source_dir" rev-parse --verify HEAD >/dev/null
if [[ -n "$(git -C "$source_dir" status --porcelain --untracked-files=normal)" ]]; then
    echo 'Нужен чистый checkout фиксированной версии; сохраните изменения перед установкой.' >&2; exit 2
fi
# Не допускаем удаления данных при удалении checkout и небезопасного наложения каталогов.
"$python_bin" - "$source_dir" "$prefix" "$config" "$data_dir" "$env_file" "$local_install" <<'PY'
from pathlib import Path
import sys
source, prefix, config, data, env = map(lambda s: Path(s).resolve(), sys.argv[1:6])
if any(p.is_relative_to(source) or source.is_relative_to(p) for p in (prefix, config, data, env)):
    raise SystemExit('Пути установки должны находиться вне checkout.')
if data.is_relative_to(prefix) or prefix.is_relative_to(data) or config == env:
    raise SystemExit('Код, данные и файлы настроек должны быть разделены.')
if config.is_relative_to(data) or env.is_relative_to(data):
    raise SystemExit('Настройки должны находиться вне каталога данных.')
if data.exists() and data.stat().st_mode & 0o077:
    raise SystemExit('Существующий каталог данных должен иметь права 0700; права не изменены.')
if sys.argv[6] == 'false':
    hidden = [Path(p) for p in ('/tmp', '/var/tmp', '/home', '/root', '/run/user')]
    if any(p.is_relative_to(parent) for p in (prefix, config, data, env) for parent in hidden):
        raise SystemExit('Пути systemd-установки скрыты ProtectHome/PrivateTmp; используйте /opt, /etc, /var/lib.')
PY
mkdir -p -- "$prefix" "$data_dir"
for parent in "$(dirname -- "$config")" "$(dirname -- "$env_file")"; do
    if [[ ! -d "$parent" ]]; then
        mkdir -p -- "$parent"
        if ! $local_install; then
            # Меняем только созданный нами каталог, не общие /etc или /tmp.
            chmod 755 "$parent"
        fi
    fi
done
if ! $local_install; then
    if ! getent passwd goose-xmpp-bot >/dev/null; then
        useradd --system --user-group --home-dir "$data_dir" --no-create-home --shell /usr/sbin/nologin goose-xmpp-bot
    fi
    # Код и venv читаются сервисом, но не принадлежат ему.
    chmod 755 "$prefix"
fi
"$python_bin" -m venv "$prefix/venv"
"$prefix/venv/bin/python" -m pip install --disable-pip-version-check --require-hashes -r "$source_dir/requirements.lock"
"$prefix/venv/bin/python" -m pip install --disable-pip-version-check --no-build-isolation --no-deps "$source_dir"
"$prefix/venv/bin/python" - "$source_dir" "$prefix" "$config" "$data_dir" "$env_file" <<'PY'
from pathlib import Path
import sys
source, prefix, config, data, env = map(Path, sys.argv[1:])
if not config.exists():
    content = (source / 'config/config.example.toml').read_text()
    content = content.replace('data_dir = "/var/lib/goose-xmpp-bot"', f'data_dir = "{data}"')
    with config.open('x') as f:
        f.write(content)
if not env.exists():
    with env.open('x') as f:
        f.write('# Заполните пароль отдельного тестового аккаунта; не добавляйте файл в Git.\nGOOSE_XMPP_PASSWORD=\n')
unit = (source / 'systemd/goose-xmpp-bot.service.in').read_text()
for key, value in {'PREFIX': prefix, 'CONFIG': config, 'DATA_DIR': data, 'ENV_FILE': env}.items():
    unit = unit.replace(f'@{key}@', str(value))
(prefix / 'goose-xmpp-bot.service').write_text(unit)
PY
"$prefix/venv/bin/python" - "$config" "$data_dir" <<'PY'
from pathlib import Path
import sys
from goose.config import load_config
config = load_config(Path(sys.argv[1]))
if config.data_dir != Path(sys.argv[2]).resolve():
    raise SystemExit('data_dir существующего конфига не совпадает с --data-dir; конфиг не изменён.')
PY
if ! $local_install; then
    chmod -R a+rX "$prefix/venv"
    chown goose-xmpp-bot:goose-xmpp-bot "$data_dir"
    chown root:goose-xmpp-bot "$config" "$env_file"
    chmod 640 "$config" "$env_file"
    install -m 644 "$prefix/goose-xmpp-bot.service" /etc/systemd/system/goose-xmpp-bot.service
    systemctl daemon-reload
fi
(
    # runuser сохраняет cwd вызывающего пользователя. Checkout в /root может
    # быть недоступен сервису; диагностика работает из каталога установки.
    cd -- "$prefix"
    if $local_install; then
        "$prefix/venv/bin/goose-bot" doctor --config "$config"
    else
        runuser -u goose-xmpp-bot -- "$prefix/venv/bin/goose-bot" doctor --config "$config"
    fi
)
echo 'Файлы установлены; настройки и существующие ключи сохранены.'
echo 'Перед запуском заполните тестовые JID, allowlist и защищённый файл окружения.'
if $start; then
    # systemd читает защищённый EnvironmentFile; секрет не попадает в argv.
    systemctl enable --now goose-xmpp-bot.service
else
    echo 'Сервис не запускался. Явный запуск: systemctl enable --now goose-xmpp-bot.service'
fi
