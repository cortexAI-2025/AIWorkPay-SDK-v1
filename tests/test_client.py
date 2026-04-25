"""Sync client tests using httpx mock transport."""
import json

import httpx
import pytest

from aiworkpay import AIWorkPay, AIWorkPayError, AuthenticationError, NotFoundError, TaskTimeoutError


def _mock_transport(responses: list[tuple[int, dict]]):
    """Return an httpx.MockTransport that yields responses in order."""
    calls = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = next(calls)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def make_client(responses: list[tuple[int, dict]]) -> AIWorkPay:
    client = AIWorkPay(api_key="test-key")
    client._client = httpx.Client(transport=_mock_transport(responses))
    return client


# ------------------------------------------------------------------
# create_task
# ------------------------------------------------------------------

def test_create_task_success():
    task_payload = {"id": "t1", "status": "pending", "type": "text_validation"}
    client = make_client([(201, task_payload)])
    task = client.create_task({"type": "text_validation", "instructions": "Is spam?"})
    assert task["id"] == "t1"
    assert task["status"] == "pending"


def test_create_task_auth_error():
    client = make_client([(401, {"error": "unauthorized"})])
    with pytest.raises(AuthenticationError):
        client.create_task({"type": "text_validation", "instructions": "x"})


# ------------------------------------------------------------------
# get_task
# ------------------------------------------------------------------

def test_get_task_not_found():
    client = make_client([(404, {"error": "not found"})])
    with pytest.raises(NotFoundError):
        client.get_task("missing-id")


def test_get_task_success():
    payload = {"id": "t2", "status": "completed", "result": "not spam"}
    client = make_client([(200, payload)])
    task = client.get_task("t2")
    assert task["result"] == "not spam"


# ------------------------------------------------------------------
# wait_for_result
# ------------------------------------------------------------------

def test_wait_for_result_completes():
    responses = [
        (200, {"id": "t3", "status": "pending"}),
        (200, {"id": "t3", "status": "assigned"}),
        (200, {"id": "t3", "status": "completed", "result": "ok"}),
    ]
    client = make_client(responses)
    # Pass interval=0 to avoid sleeping in tests
    task = client.wait_for_result("t3", interval=0)
    assert task["status"] == "completed"
    assert task["result"] == "ok"


def test_wait_for_result_timeout():
    """Timeout fires before completion."""
    responses = [(200, {"id": "t4", "status": "pending"})] * 10
    client = make_client(responses)
    with pytest.raises(TaskTimeoutError):
        client.wait_for_result("t4", interval=0, timeout=0.0)


def test_wait_for_result_failed_status():
    """A 'failed' status should also terminate polling."""
    responses = [
        (200, {"id": "t5", "status": "pending"}),
        (200, {"id": "t5", "status": "failed", "result": None}),
    ]
    client = make_client(responses)
    task = client.wait_for_result("t5", interval=0)
    assert task["status"] == "failed"


# ------------------------------------------------------------------
# approve_and_pay / reject_task
# ------------------------------------------------------------------

def test_approve_and_pay():
    payload = {"task_id": "t6", "paid": True, "amount": 0.05}
    client = make_client([(200, payload)])
    resp = client.approve_and_pay("t6")
    assert resp["paid"] is True


def test_reject_task():
    payload = {"task_id": "t7", "reason": "wrong output"}
    client = make_client([(200, payload)])
    resp = client.reject_task("t7", reason="wrong output")
    assert resp["reason"] == "wrong output"


# ------------------------------------------------------------------
# Batch
# ------------------------------------------------------------------

def test_create_tasks_batch():
    responses = [
        (201, {"id": "b1", "status": "pending"}),
        (201, {"id": "b2", "status": "pending"}),
    ]
    client = make_client(responses)
    tasks = client.create_tasks([
        {"type": "a", "instructions": "x"},
        {"type": "b", "instructions": "y"},
    ])
    assert [t["id"] for t in tasks] == ["b1", "b2"]


# ------------------------------------------------------------------
# Context manager
# ------------------------------------------------------------------

def test_context_manager():
    responses = [(201, {"id": "cm1", "status": "pending"})]
    with AIWorkPay(api_key="test") as client:
        client._client = httpx.Client(transport=_mock_transport(responses))
        task = client.create_task({"type": "a", "instructions": "b"})
        assert task["id"] == "cm1"
