#!/usr/bin/env bash
set -euo pipefail
python_bin=${GOOSE_PYTHON:-/opt/goose-xmpp-bot/venv/bin/python}
"$python_bin" -m goose.cli doctor "$@"
if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
    # Не печатаем журнал службы: в старых установках он мог содержать личные данные.
    state=$(systemctl is-active goose-xmpp-bot.service || true)
    printf 'systemd: %s\n' "$state"
else
    echo 'systemd: недоступен в этой среде.'
fi
