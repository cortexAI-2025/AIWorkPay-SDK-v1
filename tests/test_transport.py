from __future__ import annotations

from typing import List, Type

import httpx
import pytest

from aiworkpay import (
    AIWorkPay,
    AIWorkPayError,
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BudgetLimitError,
    ConflictError,
    IdempotencyKeyReusedError,
    InsufficientScopeError,
    NotFoundError,
    PaymentError,
    PaymentProviderUnavailableError,
    RateLimitError,
    ServerError,
    ValidationError,
)

from .conftest import BASE, KEY
from .fake_api import FakeApi


def bare_client(api: FakeApi, **kwargs: object) -> AIWorkPay:
    sdk = AIWorkPay(KEY, base_url=BASE, http_client=httpx.Client(transport=api.transport), **kwargs)  # type: ignore[arg-type]
    sdk._sleep = lambda _s: None
    sdk._jitter = lambda: 1.0
    return sdk


# ── Mapping of answers to exceptions ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "body", "expected", "retryable"),
    [
        (401, {"error": "Clé API manquante", "code": "UNAUTHORIZED"}, AuthenticationError, False),
        (403, {"error": "invalide", "code": "INVALID_API_KEY"}, AuthenticationError, False),
        (
            403,
            {"error": "scope", "code": "INSUFFICIENT_SCOPE", "details": {"requiredScope": "missions:write"}},
            InsufficientScopeError,
            False,
        ),
        (403, {"error": "plafond", "code": "BUDGET_LIMIT_EXCEEDED"}, BudgetLimitError, False),
        (403, {"error": "mois", "code": "MONTHLY_BUDGET_EXCEEDED"}, BudgetLimitError, False),
        (400, {"error": "invalide", "code": "INVALID_BODY"}, ValidationError, False),
        (404, {"error": "introuvable", "code": "MISSION_NOT_FOUND"}, NotFoundError, False),
        (409, {"error": "état", "code": "MISSION_NOT_CANCELABLE"}, ConflictError, False),
        (422, {"error": "clé", "code": "IDEMPOTENCY_KEY_REUSED"}, IdempotencyKeyReusedError, False),
        (402, {"error": "carte", "code": "PAYMENT_FAILED"}, PaymentError, False),
        (422, {"error": "refus", "code": "PAYMENT_REJECTED"}, PaymentError, False),
        (429, {"error": "trop", "code": "RATE_LIMITED"}, RateLimitError, True),
        (502, {"error": "stripe", "code": "PAYMENT_PROVIDER_UNAVAILABLE"}, PaymentProviderUnavailableError, True),
        (500, {"error": "oups", "code": "INTERNAL_ERROR"}, ServerError, True),
        (418, {"error": "théière"}, AIWorkPayError, False),
    ],
)
def test_every_answer_maps_to_a_typed_error(
    api: FakeApi,
    status: int,
    body: dict,
    expected: Type[AIWorkPayError],
    retryable: bool,  # type: ignore[type-arg]
) -> None:
    sdk = bare_client(api, max_retries=0)
    api.fail = [(status, body, {})]
    with pytest.raises(expected) as info:
        sdk.get_mission("msn_1")
    error = info.value
    assert type(error) is expected and error.status_code == status and error.code == body.get("code")
    assert error.retryable is retryable
    assert error.request_id == api.requests[0].headers["x-request-id"], "the id to quote to support is the one sent"
    assert body["error"] in str(error)


def test_a_non_json_error_page_still_gives_a_typed_error(api: FakeApi) -> None:
    sdk = bare_client(api, max_retries=0)
    api.fail = [(404, "<html>Not found</html>", {})]
    with pytest.raises(NotFoundError) as info:
        sdk.get_mission("msn_1")
    assert info.value.code is None and "404" in info.value.message


def test_a_malformed_success_is_reported_not_crashed_on(api: FakeApi) -> None:
    sdk = bare_client(api, max_retries=0)
    api.fail = [(200, "pas du json", {})]
    with pytest.raises(AIWorkPayError) as info:
        sdk.get_mission("msn_1")
    assert info.value.code == "INVALID_RESPONSE"
    api.fail = [(200, {"unexpected": True}, {})]
    with pytest.raises(AIWorkPayError) as info:
        sdk.get_mission("msn_1")
    assert info.value.code == "INVALID_RESPONSE"


# ── Retries ──────────────────────────────────────────────────────────────────


def test_reads_are_retried_with_a_growing_delay(client: AIWorkPay, api: FakeApi, sleeps: List[float]) -> None:
    mission = api.add_mission()
    api.fail = [(503, {"error": "x"}, {}), (500, {"error": "x"}, {})]
    assert client.get_mission(mission["id"]).id == mission["id"]
    assert sleeps == [0.5, 1.0] and len(api.requests) == 3


def test_a_rate_limit_waits_the_time_asked(client: AIWorkPay, api: FakeApi, sleeps: List[float]) -> None:
    api.fail = [(429, {"error": "trop", "code": "RATE_LIMITED"}, {"Retry-After": "3"})]
    client.list_missions()
    assert sleeps == [3.0]


def test_a_rate_limit_asking_for_minutes_is_raised_not_slept_on(
    client: AIWorkPay, api: FakeApi, sleeps: List[float]
) -> None:
    api.fail = [(429, {"error": "trop", "code": "RATE_LIMITED"}, {"Retry-After": "600"})]
    with pytest.raises(RateLimitError) as info:
        client.list_missions()
    assert info.value.retry_after == 600 and sleeps == [] and len(api.requests) == 1


def test_a_revision_request_is_never_replayed_after_an_unknown_outcome(client: AIWorkPay, api: FakeApi) -> None:
    mission = api.add_mission(status="DELIVERED")
    api.fail = [(500, {"error": "oups"}, {})]
    with pytest.raises(ServerError):
        client.request_changes(mission["id"], "Ajoutez les horaires exacts.")
    assert len(api.requests) == 1 and api.missions[mission["id"]]["revisionCount"] == 0

    api.fail = [httpx.ReadError("connexion coupée")]
    with pytest.raises(APIConnectionError):
        client.request_changes(mission["id"], "Ajoutez les horaires exacts.")
    assert len(api.requests) == 2


def test_a_revision_request_is_retried_when_the_rate_limit_refused_it(
    client: AIWorkPay, api: FakeApi, sleeps: List[float]
) -> None:
    mission = api.add_mission(status="DELIVERED")
    api.fail = [(429, {"error": "trop", "code": "RATE_LIMITED"}, {"Retry-After": "1"})]
    assert client.request_changes(mission["id"], "Ajoutez les horaires exacts.").revisions_left == 2
    assert sleeps == [1.0]


def test_a_webhook_secret_rotation_is_not_replayed(client: AIWorkPay, api: FakeApi) -> None:
    api.fail = [(502, {"error": "x"}, {})]
    with pytest.raises(ServerError):
        client.set_webhook("https://agent.example/hooks", rotate_secret=True)
    assert len(api.requests) == 1


def test_network_errors_and_timeouts(client: AIWorkPay, api: FakeApi, sleeps: List[float]) -> None:
    mission = api.add_mission()
    api.fail = [httpx.ConnectError("boom"), httpx.ConnectError("boom")]
    assert client.get_mission(mission["id"]).id == mission["id"]
    assert sleeps == [0.5, 1.0]

    api.fail = [httpx.ConnectError("boom")] * 3
    with pytest.raises(APIConnectionError) as info:
        client.get_mission(mission["id"])
    assert info.value.retryable and "boom" not in str(info.value)

    api.fail = [httpx.ReadTimeout("slow")] * 3
    with pytest.raises(APITimeoutError):
        client.get_mission(mission["id"])


# ── Credentials and configuration ────────────────────────────────────────────


def test_the_key_and_the_url_can_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIWORKPAY_API_KEY", "awp_from_env_0000")
    monkeypatch.setenv("AIWORKPAY_BASE_URL", "https://staging.aiworkpay.test/")
    sdk = AIWorkPay()
    assert sdk._key == "awp_from_env_0000" and sdk._base == "https://staging.aiworkpay.test"
    sdk.close()


def test_the_default_url_is_the_application(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIWORKPAY_BASE_URL", raising=False)
    sdk = AIWorkPay("awp_abcdefghijkl")
    assert sdk._base == "https://app.aiworkpay.fr"
    sdk.close()


@pytest.mark.parametrize(
    ("key", "url"),
    [
        (None, None),
        ("not_a_key", None),
        ("awp_abcdefghijkl", "http://app.aiworkpay.fr"),
        ("awp_abcdefghijkl", "ftp://x.test"),
        ("awp_abcdefghijkl", "https://"),
    ],
)
def test_unsafe_or_missing_configuration_is_refused(monkeypatch: pytest.MonkeyPatch, key: object, url: object) -> None:
    monkeypatch.delenv("AIWORKPAY_API_KEY", raising=False)
    monkeypatch.delenv("AIWORKPAY_BASE_URL", raising=False)
    with pytest.raises(ValueError):
        AIWorkPay(key, base_url=url)  # type: ignore[arg-type]


def test_plain_http_is_only_for_this_machine() -> None:
    AIWorkPay("awp_abcdefghijkl", base_url="http://localhost:3000").close()
    AIWorkPay("awp_abcdefghijkl", base_url="http://127.0.0.1:3000").close()


def test_the_key_never_shows_in_repr_or_errors(client: AIWorkPay, api: FakeApi) -> None:
    assert "awp_agent_a_000000" not in repr(client)
    api.fail = [(403, {"error": "invalide", "code": "INVALID_API_KEY"}, {})]
    with pytest.raises(AuthenticationError) as info:
        client.get_mission("msn_1")
    assert "awp_agent_a_000000" not in repr(info.value) + str(info.value)


def test_redirects_are_never_followed_so_the_key_cannot_leave(api: FakeApi) -> None:
    sdk = AIWorkPay(KEY, base_url=BASE, max_retries=0)
    api_redirect = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "https://evil.example/"})
    )
    sdk._http = httpx.Client(transport=api_redirect)
    with pytest.raises(AIWorkPayError) as info:
        sdk.get_mission("msn_1")
    assert info.value.status_code == 302
