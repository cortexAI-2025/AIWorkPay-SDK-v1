"""Async client tests using httpx mock transport."""
import httpx
import pytest
import pytest_asyncio

from aiworkpay import AsyncAIWorkPay, AuthenticationError, NotFoundError, TaskTimeoutError


def _mock_transport(responses: list[tuple[int, dict]]):
    calls = iter(responses)

    async def handler(request: httpx.Request) -> httpx.Response:
        status, body = next(calls)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def make_client(responses: list[tuple[int, dict]]) -> AsyncAIWorkPay:
    client = AsyncAIWorkPay(api_key="test-key")
    client._client = httpx.AsyncClient(transport=_mock_transport(responses))
    return client


# ------------------------------------------------------------------
# create_task
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_create_task_success():
    client = make_client([(201, {"id": "a1", "status": "pending"})])
    task = await client.create_task({"type": "image_check", "instructions": "Real?"})
    assert task["id"] == "a1"


@pytest.mark.asyncio
async def test_async_create_task_auth_error():
    client = make_client([(401, {"error": "unauthorized"})])
    with pytest.raises(AuthenticationError):
        await client.create_task({"type": "x", "instructions": "y"})


# ------------------------------------------------------------------
# get_task
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_get_task_not_found():
    client = make_client([(404, {"error": "not found"})])
    with pytest.raises(NotFoundError):
        await client.get_task("ghost")


@pytest.mark.asyncio
async def test_async_get_task_success():
    client = make_client([(200, {"id": "a2", "status": "completed", "result": "real"})])
    task = await client.get_task("a2")
    assert task["result"] == "real"


# ------------------------------------------------------------------
# wait_for_result
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_wait_for_result_completes():
    responses = [
        (200, {"id": "a3", "status": "pending"}),
        (200, {"id": "a3", "status": "completed", "result": "done"}),
    ]
    client = make_client(responses)
    task = await client.wait_for_result("a3", interval=0)
    assert task["status"] == "completed"


@pytest.mark.asyncio
async def test_async_wait_for_result_timeout():
    responses = [(200, {"id": "a4", "status": "pending"})] * 10
    client = make_client(responses)
    with pytest.raises(TaskTimeoutError):
        await client.wait_for_result("a4", interval=0, timeout=0.0)


@pytest.mark.asyncio
async def test_async_wait_for_result_failed_status():
    responses = [
        (200, {"id": "a5", "status": "assigned"}),
        (200, {"id": "a5", "status": "failed"}),
    ]
    client = make_client(responses)
    task = await client.wait_for_result("a5", interval=0)
    assert task["status"] == "failed"


# ------------------------------------------------------------------
# approve / reject
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_approve_and_pay():
    client = make_client([(200, {"task_id": "a6", "paid": True})])
    resp = await client.approve_and_pay("a6")
    assert resp["paid"] is True


@pytest.mark.asyncio
async def test_async_reject_task():
    client = make_client([(200, {"task_id": "a7", "reason": "bad"})])
    resp = await client.reject_task("a7", reason="bad")
    assert resp["reason"] == "bad"


# ------------------------------------------------------------------
# Batch (concurrent)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_create_tasks_batch():
    responses = [
        (201, {"id": "b1", "status": "pending"}),
        (201, {"id": "b2", "status": "pending"}),
    ]
    client = make_client(responses)
    tasks = await client.create_tasks([
        {"type": "a", "instructions": "x"},
        {"type": "b", "instructions": "y"},
    ])
    assert {t["id"] for t in tasks} == {"b1", "b2"}


# ------------------------------------------------------------------
# Context manager
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_context_manager():
    responses = [(201, {"id": "cm2", "status": "pending"})]
    async with AsyncAIWorkPay(api_key="test") as client:
        client._client = httpx.AsyncClient(transport=_mock_transport(responses))
        task = await client.create_task({"type": "a", "instructions": "b"})
        assert task["id"] == "cm2"
