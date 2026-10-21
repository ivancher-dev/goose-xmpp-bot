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
