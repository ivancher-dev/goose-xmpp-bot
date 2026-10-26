import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from goose.config import ConfigError, LlmConfig
from goose.model import ModelError, OpenAICompatible, RequestBudget


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setenv("GOOSE_LLM_API_KEY", "fixture-secret-key")
    return LlmConfig(enabled=True, base_url="https://api.example.org/v1", model="test-model")


async def test_http_contract_uses_fixed_endpoint_and_no_redirects(llm):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://api.example.org/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer fixture-secret-key"
        data = json.loads(request.content)
        assert data == {"model": "test-model", "messages": [{"role": "user", "content": "Пример"}],
                        "max_tokens": 700, "stream": False}
        return httpx.Response(200, json={"choices": [{"message": {"content": "Га!"}}]})
    model = OpenAICompatible(llm, transport=httpx.MockTransport(handler))
    try:
        assert await model.complete([{"role": "user", "content": "Пример"}]) == "Га!"
        assert len(requests) == 1
    finally:
        await model.close()


@pytest.mark.parametrize("status,code,retries", [(401, "authentication", 1), (403, "authentication", 1),
    (400, "provider", 1), (302, "provider", 1), (429, "unavailable", 2),
    (503, "unavailable", 2), (502, "unavailable", 2), (504, "unavailable", 2)])
async def test_http_errors_bounded_and_redacted(llm, status, code, retries):
    calls, pauses = [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private-response-must-not-escape", headers={"Location": "https://other.example.org"})
    async def pause(delay):
        pauses.append(delay)
    model = OpenAICompatible(llm, transport=httpx.MockTransport(handler), sleep=pause)
    try:
        with pytest.raises(ModelError) as error:
            await model.complete([])
        assert str(error.value) == code
        assert len(calls) == retries
        assert len(pauses) == retries - 1
    finally:
        await model.close()


@pytest.mark.parametrize("exception,count", [(httpx.ConnectError, 2), (httpx.ConnectTimeout, 2),
                                          (httpx.ReadTimeout, 1), (httpx.ReadError, 1)])
async def test_only_pre_request_connection_errors_are_retried(llm, exception, count):
    calls = []
    def handler(request):
        calls.append(request)
        raise exception("sensitive diagnostic", request=request)
    async def pause(_delay):
        pass
    model = OpenAICompatible(llm, transport=httpx.MockTransport(handler), sleep=pause)
    try:
        with pytest.raises(ModelError) as error:
            await model.complete([])
        assert "sensitive" not in str(error.value)
        assert len(calls) == count
    finally:
        await model.close()


@pytest.mark.parametrize("payload", [b"broken json", b"null", b"{}", b'{"choices":[]}',
    b'{"choices":[{"message":{"content":null}}]}',
    b'{"choices":[{"message":{"content":[]}}]}',
    b'{"choices":[{"message":{"content":"\\u0000"}}]}',
    b'{"choices":[{"message":{"content":"x","tool_calls":[{}]}}]}', b"x" * 65537])
async def test_invalid_or_oversized_responses(llm, payload):
    model = OpenAICompatible(llm, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload)))
    try:
        with pytest.raises(ModelError, match="invalid_response"):
            await model.complete([])
    finally:
        await model.close()


async def test_cancellation_propagates_without_retry(llm):
    calls = []
    async def handler(request):
        calls.append(request)
        raise asyncio.CancelledError
    model = OpenAICompatible(llm, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(asyncio.CancelledError):
            await model.complete([])
        assert len(calls) == 1
    finally:
        await model.close()


async def test_total_timeout_bounds_a_response_that_never_finishes(llm):
    async def handler(_request):
        await asyncio.Event().wait()
    model = OpenAICompatible(replace(llm, timeout_seconds=0.01, max_attempts=1), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelError, match="timeout"):
            await model.complete([])
    finally:
        await model.close()


async def test_retries_consume_global_budget(llm):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503)
    async def pause(_delay):
        pass
    model = OpenAICompatible(replace(llm, requests_per_minute=1), transport=httpx.MockTransport(handler), sleep=pause)
    try:
        with pytest.raises(ModelError, match="rate_limited"):
            await model.complete([])
        assert len(calls) == 1
    finally:
        await model.close()


def test_budget_expires_without_unbounded_growth():
    now = [0]
    budget = RequestBudget(2, lambda: now[0])
    budget.take()
    budget.take()
    with pytest.raises(ModelError, match="rate_limited"):
        budget.take()
    now[0] = 60
    budget.take()
    assert len(budget.calls) == 1


def test_missing_key_and_output_limit(llm, monkeypatch):
    monkeypatch.delenv(llm.api_key_env)
    with pytest.raises(ConfigError, match="Ключ API"):
        OpenAICompatible(llm)
    result = OpenAICompatible.parse(json.dumps({"choices": [{"message": {"content": "я" * 5000}}]}))
    assert len(result) == 4096 and result.endswith("…")
