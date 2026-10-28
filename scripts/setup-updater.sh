#!/usr/bin/env bash
# Однократно регистрирует обновлятор поверх прежней установки, не меняя её venv.
set -euo pipefail
umask 077
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
prefix=/opt/goose-xmpp-bot
config=/etc/goose-xmpp-bot/config.toml
data_dir=/var/lib/goose-xmpp-bot
env_file=/etc/goose-xmpp-bot/goose.env
local_install=false
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
        *) echo 'Допустимы --prefix, --config, --data-dir, --env-file, --local.' >&2; exit 2;;
    esac
done
if ! $local_install && ((EUID != 0)); then
    echo 'Для системной установки нужен sudo.' >&2; exit 2
fi
if [[ -n "$(git -C "$source_dir" status --porcelain --untracked-files=normal)" ]]; then
    echo 'Нужен чистый checkout выбранной версии.' >&2; exit 2
fi
python_bin=${GOOSE_PYTHON:-python3.12}
"$python_bin" - "$source_dir" "$prefix" "$config" "$data_dir" "$env_file" "$local_install" <<'PY'
from pathlib import Path
import sys
assert sys.version_info[:2] == (3, 12), 'Требуется Python 3.12'
sys.path.insert(0, str(Path(sys.argv[1]) / 'src'))
from goose.maintenance import bootstrap, UpdateError
try:
    bootstrap(*map(Path, sys.argv[1:6]), sys.argv[6] == 'true')
except (UpdateError, OSError, ValueError, KeyError):
    raise SystemExit('Не удалось зарегистрировать обновлятор. Проверьте пути, права и наличие прежней установки; секреты не выводятся.')
PY
echo 'Команда goose-update установлена. Текущий сервис, venv и данные не менялись.'
if $local_install; then
    echo "Локальная команда: $prefix/bin/goose-update"
else
    echo 'После слияния версии в main: sudo goose-update'
    echo 'Для проверки выбранной ветки: sudo goose-update --ref ИМЯ_ВЕТКИ'
fi
