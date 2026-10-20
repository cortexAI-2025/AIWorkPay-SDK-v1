"""The 12-step scenario through the SDK, against the isolated AiWorkPay environment of Aiworkpay-v2.

Start it there with ``npm run e2e:harness`` (real server + PostgreSQL + Stripe *simulator* + simulated Payworker), then:

    E2E_CONTROL_URL=http://127.0.0.1:4100 pytest tests/test_e2e.py

Skipped when E2E_CONTROL_URL is not set. It never touches a real Stripe account or a real person.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from aiworkpay import (
    AIWorkPay,
    AsyncAIWorkPay,
    AuthenticationError,
    BudgetLimitError,
    InsufficientScopeError,
    MissionStatus,
    NotFoundError,
)

CONTROL = os.environ.get("E2E_CONTROL_URL")
pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not CONTROL, reason="E2E_CONTROL_URL not set")]

_counter = 0


def control(method: str, path: str, body: Any = None) -> Any:
    request = urllib.request.Request(
        f"{CONTROL}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def new_agent(scopes: Any = ("missions:read", "missions:write", "missions:approve"), **extra: Any) -> dict:
    global _counter
    _counter += 1
    return control(
        "POST",
        "/agents",
        {
            "label": f"sdk-{int(time.time())}-{_counter}",
            "scopes": list(scopes),
            "maxMissionBudget": 100,
            "monthlyBudget": 500,
            **extra,
        },
    )


@pytest.fixture(scope="module")
def base_url() -> str:
    return control("GET", "/info")["baseUrl"]


# One fixed value: an identical retry must be an identical request
DEADLINE = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=5)


def deadline() -> datetime:
    return DEADLINE


def order(sdk: AIWorkPay, **over: Any):
    args = dict(
        title="Horaires et photo de la façade",
        description="Relever les horaires d'ouverture du commerce et rapporter une photo de sa façade.",
        budget=30,
        deadline=deadline(),
    )
    args.update(over)
    return sdk.create_mission(**args)


def test_scenario_opening_hours_and_front_photo(base_url: str) -> None:
    agent = new_agent()
    with AIWorkPay(agent["key"], base_url=base_url) as sdk:
        account = sdk.get_account()
        assert account.key.active and account.limits.max_mission_budget == 100

        created = order(sdk, idempotency_key=f"sdk-e2e-{agent['slug']}")
        mission_id = created.mission.id
        assert created.payment is not None and created.payment.mode == "checkout"
        assert created.payment.url and created.payment.url.startswith("https://checkout.stripe.com/")
        assert created.mission.status == MissionStatus.PAYMENT_PENDING

        # The same call again never creates a second mission
        again = order(sdk, idempotency_key=f"sdk-e2e-{agent['slug']}")
        assert again.mission.id == mission_id and again.replayed

        # A human pays on Stripe: the webhook publishes the mission
        assert control("POST", "/stripe/pay", {"missionId": mission_id})["webhookStatus"] == 200
        assert sdk.get_mission(mission_id).status == MissionStatus.PUBLISHED

        # The Payworker accepts, works, uploads a photo and a PDF, delivers
        control("POST", "/payworker/work", {"missionId": mission_id, "proof": "both"})
        delivered = sdk.wait_for_delivery(mission_id, interval=0.5, timeout=30)
        assert delivered.status == MissionStatus.DELIVERED
        assert "ouvert du lundi au samedi" in (delivered.result_note or "")
        assert delivered.result_data == {"open": True, "hours": "9h-19h"}

        uploads = [a for a in delivered.attachments if a.kind == "PROOF" and a.source == "upload"]
        assert {a.filename for a in uploads} == {"facade.png", "horaires.pdf"}
        photo = next(a for a in uploads if a.filename == "facade.png")
        proof = sdk.download_proof(mission_id, photo.id)
        assert proof.content.startswith(b"\x89PNG") and proof.content_type == "image/png"

        approval = sdk.approve_mission(mission_id)
        assert approval.commission is not None and approval.commission.transfer_status == "SUCCEEDED"
        assert float(approval.commission.payworker_amount) == 27.0
        assert sdk.approve_mission(mission_id).already_approved  # a second approval moves no money

        transfers = [
            t for t in control("GET", "/stripe/state")["transfers"] if t["metadata"]["missionId"] == mission_id
        ]
        assert len(transfers) == 1

        page = sdk.list_transactions(mission_id=mission_id)
        assert page.total == 1


def test_errors_and_refusals(base_url: str) -> None:
    with pytest.raises(AuthenticationError):
        with AIWorkPay("awp_" + "x" * 40, base_url=base_url, max_retries=0) as bad:
            bad.get_account()

    reader = new_agent(scopes=("missions:read",))
    with AIWorkPay(reader["key"], base_url=base_url) as sdk:
        with pytest.raises(InsufficientScopeError):
            order(sdk)

    agent = new_agent()
    other = new_agent()
    with AIWorkPay(agent["key"], base_url=base_url) as sdk, AIWorkPay(other["key"], base_url=base_url) as intruder:
        with pytest.raises(BudgetLimitError):
            order(sdk, budget=101)
        mission = order(sdk).mission
        with pytest.raises(NotFoundError):  # another agent's mission looks like it does not exist
            intruder.get_mission(mission.id)
        with pytest.raises(ValueError):
            order(sdk, budget=-5)


def test_cancel_before_payment(base_url: str) -> None:
    agent = new_agent()
    with AIWorkPay(agent["key"], base_url=base_url) as sdk:
        mission = order(sdk).mission
        canceled = sdk.cancel_mission(mission.id)
        assert canceled.mission.status == MissionStatus.CANCELED


async def test_async_client(base_url: str) -> None:
    agent = new_agent()
    async with AsyncAIWorkPay(agent["key"], base_url=base_url) as sdk:
        created = await sdk.create_mission(
            title="Photo async", description="Prendre une photo de la façade.", budget=20, deadline=deadline()
        )
        assert created.payment is not None and created.payment.url
        assert (await sdk.get_mission(created.mission.id)).id == created.mission.id
