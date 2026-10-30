"""Ограниченный HTTPS-адаптер Chat Completions; ошибки не содержат тела ответа."""

import asyncio
from collections import deque
import json
import time
from typing import Protocol

import httpx

from .config import LlmConfig
from .language import PREFIX


class ModelError(Exception):
    def __init__(self, code):
        self.code = code if code in {"rate_limited", "authentication", "provider", "invalid_response",
                                     "timeout", "network", "unavailable", "context_limit", "configuration"} else "provider"
        super().__init__(self.code)


class Model(Protocol):
    async def complete(self, messages: list[dict[str, str]]) -> str: ...
    async def close(self) -> None: ...


class RequestBudget:
    def __init__(self, limit, clock=time.monotonic):
        self.limit, self.clock = limit, clock
        self.calls = deque()

    def take(self):
        now = self.clock()
        while self.calls and self.calls[0] <= now - 60:
            self.calls.popleft()
        if len(self.calls) >= self.limit:
            raise ModelError("rate_limited")
        self.calls.append(now)


class OpenAICompatible:
    """POST <base_url>/chat/completions, Bearer auth, без инструментов и редиректов."""

    def __init__(self, config: LlmConfig, *, transport=None, sleep=asyncio.sleep, clock=time.monotonic):
        self.config = config
        self._key = config.key()
        self.budget = RequestBudget(config.requests_per_minute, clock)
        self.sleep = sleep
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout_seconds), follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_connections=config.max_concurrent_requests,
                               max_keepalive_connections=config.max_concurrent_requests),
            transport=transport)

    async def complete(self, messages):
        total = (self.config.timeout_seconds * self.config.max_attempts
                 + 0.25 * self.config.max_attempts * (self.config.max_attempts - 1))
        try:
            async with asyncio.timeout(total):
                return await self._complete(messages)
        except TimeoutError:
            raise ModelError("timeout") from None

    async def _complete(self, messages):
        for attempt in range(self.config.max_attempts):
            self.budget.take()
            retry = False
            try:
                async with self.client.stream(
                    "POST", self.config.base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + self._key, "Accept-Encoding": "identity"},
                    json={"model": self.config.model, "messages": messages,
                          "max_tokens": self.config.max_output_tokens, "stream": False},
                ) as response:
                    if response.status_code in {401, 403}:
                        raise ModelError("authentication")
                    if response.status_code in {429, 502, 503, 504}:
                        retry = True
                    elif response.status_code != 200:
                        raise ModelError("provider")
                    else:
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            raise ModelError("invalid_response")
                        payload = bytearray()
                        async for part in response.aiter_bytes(chunk_size=8192):
                            payload.extend(part)
                            if len(payload) > 65536:
                                raise ModelError("invalid_response")
                        return self.parse(payload)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                retry = True
            except httpx.TimeoutException:
                # После отправки запроса провайдер мог уже выполнить платную генерацию.
                raise ModelError("timeout") from None
            except httpx.HTTPError:
                raise ModelError("network") from None
            if retry and attempt + 1 < self.config.max_attempts:
                await self.sleep(0.5 * (attempt + 1))
            else:
                raise ModelError("unavailable")
        raise ModelError("unavailable")

    @staticmethod
    def parse(payload):
        try:
            data = json.loads(payload)
            message = data["choices"][0]["message"]
            result = message["content"]
            if message.get("tool_calls") or not isinstance(result, str) or not result.strip():
                raise ValueError
            # XML 1.0 не допускает NUL, суррогаты и часть управляющих символов.
            def valid_xml(text):
                return all(c in "\t\n\r" or 0x20 <= ord(c) <= 0xD7FF
                           or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF for c in text)
            index = result.find(PREFIX)
            answer = result[:index] if index >= 0 else result
            if not valid_xml(answer):
                raise ValueError
            if not valid_xml(result):
                # Невалидный необязательный хвост не отнимает уже готовый ответ.
                result = answer
            result = result.strip()
            if not result:
                raise ValueError
            return result if len(result) <= 4096 else result[:4095] + "…"
        except (ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise ModelError("invalid_response") from None

    async def close(self):
        await self.client.aclose()
