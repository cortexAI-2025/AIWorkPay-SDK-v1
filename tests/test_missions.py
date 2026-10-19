from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import List

import httpx
import pytest

from aiworkpay import (
    AIWorkPay,
    BudgetLimitError,
    ConflictError,
    IdempotencyKeyReusedError,
    InsufficientScopeError,
    MissionCanceledError,
    MissionStatus,
    NotFoundError,
    PaymentProviderUnavailableError,
    ProofTooLargeError,
    WaitTimeoutError,
)

from .conftest import BASE
from .fake_api import FakeApi

DEADLINE = datetime.now(timezone.utc) + timedelta(days=3)


def order(client: AIWorkPay, **over):  # type: ignore[no-untyped-def]
    args = dict(
        title="Vérifier 20 fiches", description="Contrôler les informations des fiches.", budget=25, deadline=DEADLINE
    )
    args.update(over)
    return client.create_mission(**args)


# ── create_mission ───────────────────────────────────────────────────────────


def test_create_sends_the_documented_body_and_pays_by_checkout(client: AIWorkPay, api: FakeApi) -> None:
    created = order(client, priority="HIGH", budget=Decimal("25.50"), currency="eur")
    request = api.sent("POST", "/api/missions")[0]
    import json

    assert json.loads(request.content) == {
        "title": "Vérifier 20 fiches",
        "description": "Contrôler les informations des fiches.",
        "budget": 25.5,
        "currency": "EUR",
        "deadline": DEADLINE.isoformat(),
        "priority": "HIGH",
        "paymentMode": "checkout",
    }
    assert request.headers["idempotency-key"].startswith("awp-sdk-")
    assert request.headers["authorization"] == "Bearer awp_agent_a_000000"
    assert request.headers["user-agent"].startswith("aiworkpay-python/2")
    assert created.mission.status == MissionStatus.PAYMENT_PENDING and created.mission.awaiting_payment
    assert created.payment is not None and created.payment.url.startswith("https://checkout.stripe.com/")
    assert created.mission.budget == Decimal("25.5") and created.mission.deadline is not None
    assert created.replayed is False


def test_a_date_is_accepted_as_deadline(client: AIWorkPay, api: FakeApi) -> None:
    order(client, deadline=date.today() + timedelta(days=5))
    assert (
        api.sent("POST", "/api/missions")[0].content.decode().count(str((date.today() + timedelta(days=5)).isoformat()))
        == 1
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"budget": 0},
        {"budget": -3},
        {"budget": "abc"},
        {"budget": 1.234},
        {"title": "ab"},
        {"description": "court"},
        {"currency": "EURO"},
        {"priority": "urgent"},
        {"payment_mode": "cash"},
        {"idempotency_key": "court"},
        {"payment_method_id": "pm_1"},
        {"deadline": datetime(2030, 1, 1)},
        {"deadline": ""},
    ],
)
def test_invalid_arguments_fail_before_anything_is_sent(client: AIWorkPay, api: FakeApi, bad: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValueError):
        order(client, **bad)
    assert api.requests == []


def test_an_outage_is_retried_with_the_same_idempotency_key_and_creates_one_mission(
    client: AIWorkPay, api: FakeApi, sleeps: List[float]
) -> None:
    down = (502, {"error": "indisponible", "code": "PAYMENT_PROVIDER_UNAVAILABLE"}, {})
    api.fail = [down, down]
    created = order(client)
    posts = api.sent("POST", "/api/missions")
    assert len(posts) == 3 and len({p.headers["idempotency-key"] for p in posts}) == 1
    assert len(api.missions) == 1 and created.mission.id in api.missions
    assert sleeps == [0.5, 1.0]


def test_when_retries_run_out_the_error_says_it_is_retryable(client: AIWorkPay, api: FakeApi) -> None:
    down = (502, {"error": "indisponible", "code": "PAYMENT_PROVIDER_UNAVAILABLE"}, {})
    api.fail = [down, down, down, down]
    with pytest.raises(PaymentProviderUnavailableError) as info:
        order(client)
    assert info.value.retryable and info.value.status_code == 502 and info.value.code == "PAYMENT_PROVIDER_UNAVAILABLE"
    assert len(api.requests) == 3 and not api.missions


def test_the_same_idempotency_key_returns_the_same_mission(client: AIWorkPay, api: FakeApi) -> None:
    first = order(client, idempotency_key="verif-casa-0001")
    again = order(client, idempotency_key="verif-casa-0001")
    assert again.replayed and again.mission.id == first.mission.id and len(api.missions) == 1
    with pytest.raises(IdempotencyKeyReusedError):
        order(client, idempotency_key="verif-casa-0001", budget=30)


def test_a_cap_of_the_key_is_a_budget_error(client: AIWorkPay) -> None:
    with pytest.raises(BudgetLimitError) as info:
        order(client, budget=500)
    assert info.value.code == "BUDGET_LIMIT_EXCEEDED" and info.value.details == {"maxMissionBudget": 100}


def test_a_read_only_key_cannot_create(read_only_client: AIWorkPay) -> None:
    with pytest.raises(InsufficientScopeError) as info:
        order(read_only_client)
    assert info.value.required_scope == "missions:write"


# ── reads ────────────────────────────────────────────────────────────────────


def test_get_mission_parses_amounts_dates_and_result(client: AIWorkPay, api: FakeApi) -> None:
    mission = api.add_mission(
        status="DELIVERED",
        resultNote="Façade photographiée",
        resultData={"open": True},
        deliveredAt="2026-10-20T10:00:00.000Z",
        budget="20.00",
    )
    got = client.get_mission(mission["id"])
    assert got.status == "DELIVERED" and got.status == MissionStatus.DELIVERED and got.result_available
    assert got.result_note == "Façade photographiée" and got.result_data == {"open": True}
    assert got.budget == Decimal("20.00") and got.delivered_at == datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
    assert got.raw["id"] == mission["id"]


def test_another_agents_mission_is_not_found(client: AIWorkPay, api: FakeApi) -> None:
    other = api.add_mission(key="awp_agent_b_000000")
    with pytest.raises(NotFoundError) as info:
        client.get_mission(other["id"])
    assert info.value.code == "MISSION_NOT_FOUND"


def test_ids_are_quoted_into_the_path(client: AIWorkPay, api: FakeApi) -> None:
    with pytest.raises(NotFoundError):
        client.get_mission("../account")
    assert api.requests[-1].url.raw_path == b"/api/missions/..%2Faccount"
    with pytest.raises(ValueError):
        client.get_mission(" ")


def test_list_filters_by_status_and_iter_walks_every_page(client: AIWorkPay, api: FakeApi) -> None:
    for _ in range(5):
        api.add_mission()
    api.add_mission(status="CANCELED")
    assert len(client.list_missions(status=MissionStatus.CANCELED)) == 1
    assert len(client.list_missions(status="PUBLISHED", limit=2)) == 2
    assert [m.id for m in client.iter_missions(page_size=2)] == sorted(api.missions, reverse=True)
    with pytest.raises(ValueError):
        client.list_missions(limit=0)


# ── reviewing ────────────────────────────────────────────────────────────────


def test_approve_pays_once_and_is_harmless_to_repeat(client: AIWorkPay, api: FakeApi) -> None:
    mission = api.add_mission(status="DELIVERED")
    first = client.approve_mission(mission["id"])
    assert not first.already_approved and first.mission.status == MissionStatus.COMPLETED
    assert first.commission is not None and first.commission.payworker_amount == Decimal("18")
    assert client.approve_mission(mission["id"]).already_approved


def test_approve_before_delivery_is_a_conflict(client: AIWorkPay, api: FakeApi) -> None:
    mission = api.add_mission(status="PUBLISHED")
    with pytest.raises(ConflictError) as info:
        client.approve_mission(mission["id"])
    assert info.value.code == "MISSION_NOT_DELIVERED" and info.value.details == {"status": "PUBLISHED"}


def test_approval_needs_its_own_permission(api: FakeApi, client: AIWorkPay) -> None:
    api.keys["awp_agent_a_000000"]["scopes"] = ["missions:read", "missions:write"]
    mission = api.add_mission(status="DELIVERED")
    with pytest.raises(InsufficientScopeError) as info:
        client.approve_mission(mission["id"])
    assert info.value.required_scope == "missions:approve"


def test_request_changes_reports_the_revisions_left_and_validates_feedback(client: AIWorkPay, api: FakeApi) -> None:
    mission = api.add_mission(status="DELIVERED")
    result = client.request_changes(mission["id"], "Ajoutez les horaires exacts.")
    assert result.revisions_left == 2 and result.mission.status == MissionStatus.IN_PROGRESS
    with pytest.raises(ValueError):
        client.request_changes(mission["id"], "court")


def test_cancel_tells_what_happened_to_the_payment(client: AIWorkPay, api: FakeApi) -> None:
    paid, unpaid = api.add_mission(status="PUBLISHED"), api.add_mission(status="PAYMENT_PENDING")
    assert client.cancel_mission(paid["id"]).payment == "refunded"
    assert client.cancel_mission(unpaid["id"]).payment == "checkout_closed"
    engaged = api.add_mission(status="IN_PROGRESS")
    with pytest.raises(ConflictError) as info:
        client.cancel_mission(engaged["id"])
    assert info.value.code == "MISSION_NOT_CANCELABLE"


# ── waiting ──────────────────────────────────────────────────────────────────


def test_wait_returns_as_soon_as_the_result_can_be_read(client: AIWorkPay, api: FakeApi, sleeps: List[float]) -> None:
    mission = api.add_mission(status="ASSIGNED")

    def tick(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            api.advance(mission["id"], status="DELIVERED", resultNote="Fait")

    client._sleep = tick
    got = client.wait_for_delivery(mission["id"])
    assert got.status == MissionStatus.DELIVERED and sleeps == [5.0, 5.0]
    assert len(api.sent("GET", f"/api/missions/{mission['id']}")) == 3


def test_wait_does_not_hang_on_an_already_completed_mission(client: AIWorkPay, api: FakeApi) -> None:
    mission = api.add_mission(status="COMPLETED")
    assert client.wait_for_delivery(mission["id"]).status == "COMPLETED"


def test_wait_raises_when_canceled_or_when_time_is_up(client: AIWorkPay, api: FakeApi) -> None:
    canceled = api.add_mission(status="CANCELED")
    with pytest.raises(MissionCanceledError):
        client.wait_for_delivery(canceled["id"])
    open_mission = api.add_mission(status="PUBLISHED")
    client._sleep = time.sleep
    with pytest.raises(WaitTimeoutError):
        client.wait_for_delivery(open_mission["id"], interval=0.01, timeout=0.05)
    with pytest.raises(ValueError):
        client.wait_for_delivery(open_mission["id"], interval=0)


def test_wait_for_deliveries_returns_them_in_order(client: AIWorkPay, api: FakeApi) -> None:
    ids = [api.add_mission(status="DELIVERED")["id"] for _ in range(3)]
    assert [m.id for m in client.wait_for_deliveries(ids)] == ids


# ── proofs ───────────────────────────────────────────────────────────────────


def proof_mission(api: FakeApi, file_id: str = "f1") -> str:
    attachment = {
        "id": file_id,
        "filename": "facade.jpg",
        "kind": "PROOF",
        "source": "upload",
        "url": "https://evil.example/steal",
        "mimeType": "image/jpeg",
        "size": 4,
        "scanStatus": "CLEAN",
        "createdAt": "2026-10-20T10:00:00.000Z",
    }
    return api.add_mission(status="DELIVERED", attachments=[attachment])["id"]


def test_a_proof_is_downloaded_from_the_configured_host_only(client: AIWorkPay, api: FakeApi) -> None:
    api.files["f1"] = (b"\xff\xd8\xff\xe0", "image/jpeg")
    mission_id = proof_mission(api)
    mission = client.get_mission(mission_id)
    proof = mission.proofs[0]
    assert proof.is_uploaded and proof.scan_status == "CLEAN"
    got = client.download_proof(mission_id, proof.id)
    assert got.content == b"\xff\xd8\xff\xe0" and got.content_type == "image/jpeg"
    assert {r.url.host for r in api.requests} == {"app.aiworkpay.test"}


def test_a_file_over_the_limit_is_refused_even_without_content_length(client: AIWorkPay, api: FakeApi) -> None:
    api.files["big"] = (b"x" * 100, "image/png")
    big = proof_mission(api, "big")
    with pytest.raises(ProofTooLargeError):
        client.download_proof(big, "big", max_bytes=10)
    api.chunked["stream"] = [b"a" * 8, b"b" * 8]
    streamed = proof_mission(api, "stream")
    with pytest.raises(ProofTooLargeError):
        client.download_proof(streamed, "stream", max_bytes=10)
    assert client.download_proof(streamed, "stream", max_bytes=16).content == b"a" * 8 + b"b" * 8


# ── account ──────────────────────────────────────────────────────────────────


def test_account_and_transactions(client: AIWorkPay, api: FakeApi) -> None:
    kept, refunded = api.add_mission(budget="40"), api.add_mission(budget="30")
    client.cancel_mission(refunded["id"])
    account = client.get_account()
    assert account.key.label == "Agent A" and account.key.scopes == (
        "missions:read",
        "missions:write",
        "missions:approve",
    )
    assert account.usage.committed_this_month == Decimal("40") and account.usage.remaining_this_month == Decimal("260")
    assert account.usage.total_paid == Decimal("70") and account.usage.total_refunded == Decimal("30")
    assert account.usage.net_paid == Decimal("40") and account.missions_by_status == {"PUBLISHED": 1, "CANCELED": 1}
    assert account.max_next_mission_budget == Decimal("100") and not account.webhook_configured

    page = client.list_transactions()
    assert page.total == 2 and len(page) == 2
    assert {t.mission_id: t.refunded for t in page} == {kept["id"]: False, refunded["id"]: True}
    assert [t.mission_id for t in client.list_transactions(mission_id=kept["id"])] == [kept["id"]]


def test_the_largest_next_mission_follows_the_monthly_budget(client: AIWorkPay, api: FakeApi) -> None:
    api.keys["awp_agent_a_000000"]["monthly"] = 130
    api.add_mission(budget="90")
    assert client.get_account().max_next_mission_budget == Decimal("40")


# ── webhook of the key ───────────────────────────────────────────────────────


def test_webhook_configuration(client: AIWorkPay, api: FakeApi) -> None:
    assert client.get_webhook().url is None
    first = client.set_webhook("https://agent.example/hooks")
    assert first.secret == "awhsec_issued_once" and "awhsec" not in repr(first)
    assert client.set_webhook("https://agent.example/hooks").secret is None
    assert client.set_webhook("https://agent.example/hooks", rotate_secret=True).secret is not None
    assert client.get_account().webhook_configured
    assert client.send_webhook_test() == "evt_ping"
    client.delete_webhook()
    assert client.get_webhook().url is None
    from aiworkpay import AIWorkPayError

    with pytest.raises(AIWorkPayError) as info:
        client.set_webhook("http://agent.example/hooks")
    assert info.value.code == "INVALID_WEBHOOK_URL"


def test_the_transport_can_be_a_plain_client_with_its_own_base_url(api: FakeApi) -> None:
    with AIWorkPay("awp_agent_a_000000", base_url=BASE, http_client=httpx.Client(transport=api.transport)) as sdk:
        assert sdk.list_missions() == []
