import json

import httpx
import pytest

from goose.cli import main
from goose.model import OpenAICompatible


@pytest.fixture
def model_config(config_file, tmp_path, monkeypatch):
    persona = tmp_path / "persona.md"
    persona.write_text("Тестовый Гусь.")
    content = config_file.read_text().replace('[llm]\nenabled = false', '[llm]\nenabled = true')
    content = content.replace('base_url = ""', 'base_url = "https://api.example.org/v1"')
    content = content.replace('model = ""', 'model = "fixture-model"')
    content = content.replace('/etc/goose-xmpp-bot/persona.md', str(persona))
    config_file.write_text(content)
    monkeypatch.setenv("GOOSE_LLM_API_KEY", "fixture-api-secret")
    return config_file


def test_local_doctor_with_enabled_model_never_constructs_http_client(model_config, monkeypatch, capsys):
    def forbidden(_config):
        raise AssertionError("Локальная диагностика не должна обращаться к API")
    monkeypatch.setattr("goose.cli.OpenAICompatible", forbidden)
    assert main(["doctor", "--config", str(model_config)]) == 0
    output = capsys.readouterr().out
    assert "Запросов к модели в локальной проверке нет" in output
    assert "fixture-api-secret" not in output


def test_api_probe_is_explicit_one_small_request_and_does_not_print_response(model_config, monkeypatch, capsys):
    requests = []
    def handler(request):
        requests.append(request)
        assert json.loads(request.content)["max_tokens"] == 16
        return httpx.Response(200, json={"choices": [{"message": {"content": "private-fixture-response"}}]})
    monkeypatch.setattr("goose.cli.OpenAICompatible", lambda config: OpenAICompatible(config, transport=httpx.MockTransport(handler)))
    assert main(["doctor", "--config", str(model_config), "--api"]) == 0
    output = capsys.readouterr().out
    assert len(requests) == 1
    assert "может тарифицироваться" in output
    assert "private-fixture-response" not in output
    assert "fixture-api-secret" not in output


def test_api_probe_never_retries_or_exposes_provider_body(model_config, monkeypatch, capsys):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(503, text="private-fixture-response")
    monkeypatch.setattr("goose.cli.OpenAICompatible", lambda config: OpenAICompatible(config, transport=httpx.MockTransport(handler)))
    assert main(["doctor", "--config", str(model_config), "--api"]) == 2
    output = capsys.readouterr()
    assert len(requests) == 1
    assert "не прошла" in output.err
    assert "private-fixture-response" not in output.err + output.out
