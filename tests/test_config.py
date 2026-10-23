import os
from pathlib import Path
import subprocess
import sys

import pytest

from goose.config import ConfigError, bare_jid, load_config


def test_example_and_normalization(config):
    assert config.echo_enabled is False
    assert config.timezone == "Europe/Moscow"
    assert bare_jid("PARENT@EXAMPLE.ORG/mobile") == "parent@example.org"


@pytest.mark.parametrize("old,new", [
    ('config_version = 1', 'config_version = true'),
    ('config_version = 1', 'unknown = 1\nconfig_version = 1'),
    ('timezone = "Europe/Moscow"', 'timezone = "Missing/Zone"'),
    ('require_tls = true', 'require_tls = false'),
    ('omemo = "required"', 'omemo = "disabled"'),
    ('resource = "goose-bot"', 'resource = ""'),
    ('password_env = "GOOSE_XMPP_PASSWORD"', 'password_env = "bad-name"'),
    ('jid = "goose-test@example.org"', 'jid = "missing-local-part"'),
    ('jid = "goose-test@example.org"', 'jid = "goose-test@example.org/mobile"'),
    ('admin_jids = ["parent@example.org"]', 'admin_jids = ["stranger@example.org"]'),
    ('admin_jids = ["parent@example.org"]', 'admin_jids = []'),
    ('admin_jids = ["parent@example.org"]', 'admin_jids = "parent@example.org"'),
    ('grade = 2', 'grade = true'),
    ('grade = 2', 'grade = 12'),
    ('academic_year = "2026-2027"', 'academic_year = "2026-2028"'),
    ('schedule_class_id = "cohort-example-1"', 'schedule_class_id = ""'),
    ('default_mode = "manual"', 'default_mode = "auto"'),
    ('log_level = "INFO"', 'log_level = "DEBUG"'),
    ('echo_enabled = false', 'echo_enabled = "false"'),
    ('enabled = false', 'enabled = true'),
    ('recent_messages = 24', 'recent_messages = 0'),
    ('max_output_tokens = 700', 'max_output_tokens = true'),
    ('timeout_seconds = 30', 'unsupported_field = 30'),
    ('[users."pupil1@example.org"]', '[users."stranger@example.org"]'),
    ('[runtime]', '[runtime]\nmax_attempts = 0'),
    ('[runtime]', '[runtime]\nconnect_timeout = 500'),
    ('[xmpp]', '[xmpp]\nunknown = 1'),
])
def test_rejects_invalid_config(config_file, old, new):
    config_file.write_text(config_file.read_text().replace(old, new))
    with pytest.raises(ConfigError):
        load_config(config_file)


def test_data_cannot_live_in_source(config_file):
    text = config_file.read_text()
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    text = text.replace(str(config_file.parent / 'data'), str(root / 'data'))
    config_file.write_text(text)
    with pytest.raises(ConfigError, match="вне исходников"):
        load_config(config_file)


def test_password_missing_and_redacted(config, monkeypatch):
    monkeypatch.delenv(config.password_env, raising=False)
    with pytest.raises(ConfigError, match="Пароль XMPP"):
        config.password()
    monkeypatch.setenv(config.password_env, "sensitive-example-password")
    assert config.password() == "sensitive-example-password"


def test_toml_error_does_not_expose_content(config_file):
    config_file.write_text('secret = "do-not-print-me')
    with pytest.raises(ConfigError) as error:
        load_config(config_file)
    assert "do-not-print-me" not in str(error.value)


def test_doctor_from_inaccessible_checkout(config_file, tmp_path):
    # Воспроизводим cwd, который runuser наследует от root, без root и сети.
    # Дочерний процесс сам лишает себя поиска в каталоге после chdir.
    if os.geteuid() == 0:
        pytest.skip("Для реальной проверки EACCES нужен непривилегированный пользователь")
    directory = tmp_path / "private-checkout"
    directory.mkdir()
    (directory / ".git").mkdir()
    script = '''
import os, socket, sys
from goose.cli import main
def blocked(*args, **kwargs):
    raise AssertionError("Network is forbidden in doctor")
socket.socket.connect = blocked
socket.getaddrinfo = blocked
directory, config = sys.argv[1:]
os.chdir(directory)
os.chmod(directory, 0)
try:
    result = main(["doctor", "--config", config])
finally:
    os.chmod(directory, 0o700)
raise SystemExit(result)
'''
    # Поддерживаем запуск тестов как установленного wheel, так и editable checkout.
    result = subprocess.run([sys.executable, "-c", script, str(directory), str(config_file)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "SQLite: ещё не создана" in result.stdout
    assert "Локальная диагностика завершена успешно" in result.stdout
    assert not list((tmp_path / "data").iterdir())


def test_unavailable_current_directory_is_optional(config_file, monkeypatch):
    def inaccessible():
        raise PermissionError("private path must not be required")
    monkeypatch.setattr(Path, "cwd", inaccessible)
    assert load_config(config_file).jid == "goose-test@example.org"


def test_cwd_git_discovery_still_protects_data(config_file, tmp_path, monkeypatch):
    # Имитируем установленный wheel: текущий checkout отличается от каталога пакета.
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: /unused/linked-metadata\n")
    config_file.write_text(config_file.read_text().replace(str(tmp_path / "data"), str(checkout / "data")))
    monkeypatch.chdir(checkout)
    with pytest.raises(ConfigError, match="вне исходников"):
        load_config(config_file)


def test_empty_git_placeholder_does_not_mark_source(config_file, tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    assert load_config(config_file).data_dir == tmp_path / "data"


def test_cwd_real_git_directory_still_protects_data(config_file, tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="вне исходников"):
        load_config(config_file)
