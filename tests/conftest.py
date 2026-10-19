from __future__ import annotations

from typing import Iterator, List

import httpx
import pytest

from aiworkpay import AIWorkPay, AsyncAIWorkPay

from .fake_api import FakeApi

KEY = "awp_agent_a_000000"
READ_ONLY_KEY = "awp_agent_b_000000"
BASE = "https://app.aiworkpay.test"


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def sleeps() -> List[float]:
    return []


@pytest.fixture
def client(api: FakeApi, sleeps: List[float]) -> Iterator[AIWorkPay]:
    sdk = AIWorkPay(KEY, base_url=BASE, http_client=httpx.Client(transport=api.transport))
    sdk._sleep = sleeps.append
    sdk._jitter = lambda: 1.0
    yield sdk
    sdk.close()


@pytest.fixture
def read_only_client(api: FakeApi) -> AIWorkPay:
    return AIWorkPay(READ_ONLY_KEY, base_url=BASE, http_client=httpx.Client(transport=api.transport))


@pytest.fixture
def aclient(api: FakeApi, sleeps: List[float]) -> AsyncAIWorkPay:
    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    sdk = AsyncAIWorkPay(KEY, base_url=BASE, http_client=httpx.AsyncClient(transport=api.transport))
    sdk._sleep = record
    sdk._jitter = lambda: 1.0
    return sdk
