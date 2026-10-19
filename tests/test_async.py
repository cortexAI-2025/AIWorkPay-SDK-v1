from __future__ import annotations

import inspect
from typing import List

import httpx
import pytest

from aiworkpay import AIWorkPay, AsyncAIWorkPay, MissionCanceledError, MissionStatus, PaymentProviderUnavailableError
from aiworkpay.errors import APIConnectionError

from .fake_api import FakeApi
from .test_missions import DEADLINE, proof_mission


def test_the_async_client_offers_exactly_what_the_sync_client_does() -> None:
    sync = {n: f for n, f in inspect.getmembers(AIWorkPay, inspect.isfunction) if not n.startswith("_")}
    asynchronous = {n: f for n, f in inspect.getmembers(AsyncAIWorkPay) if not n.startswith("_") and callable(f)}
    assert set(sync) == set(asynchronous)
    for name, function in sync.items():
        assert list(inspect.signature(function).parameters) == list(inspect.signature(asynchronous[name]).parameters), (
            name
        )
        if name == "iter_missions":
            assert inspect.isasyncgenfunction(asynchronous[name])
        else:
            assert inspect.iscoroutinefunction(asynchronous[name]), name


async def test_create_retries_an_outage_with_the_same_key(
    aclient: AsyncAIWorkPay, api: FakeApi, sleeps: List[float]
) -> None:
    down = (502, {"error": "indisponible", "code": "PAYMENT_PROVIDER_UNAVAILABLE"}, {})
    api.fail = [down, down]
    created = await aclient.create_mission("Vérifier 20 fiches", "Contrôler les informations.", 25, DEADLINE)
    posts = api.sent("POST", "/api/missions")
    assert len(posts) == 3 and len({p.headers["idempotency-key"] for p in posts}) == 1 and len(api.missions) == 1
    assert created.payment is not None and created.payment.url and sleeps == [0.5, 1.0]
    api.fail = [down] * 3
    with pytest.raises(PaymentProviderUnavailableError):
        await aclient.create_mission("Vérifier 20 fiches", "Contrôler les informations.", 25, DEADLINE)


async def test_reads_and_reviews(aclient: AsyncAIWorkPay, api: FakeApi) -> None:
    mission = api.add_mission(status="DELIVERED", resultNote="Fait")
    assert (await aclient.get_mission(mission["id"])).result_available
    assert (await aclient.request_changes(mission["id"], "Ajoutez les horaires exacts.")).revisions_left == 2
    api.advance(mission["id"], status="DELIVERED")
    assert (await aclient.approve_mission(mission["id"])).commission is not None
    assert (await aclient.get_account()).usage.total_paid > 0
    assert len(await aclient.list_transactions()) == 1


async def test_iter_missions_and_concurrent_waits(aclient: AsyncAIWorkPay, api: FakeApi) -> None:
    ids = [api.add_mission(status="DELIVERED")["id"] for _ in range(5)]
    assert [m.id async for m in aclient.iter_missions(page_size=2)] == sorted(ids, reverse=True)
    waited = await aclient.wait_for_deliveries(ids)
    assert [m.id for m in waited] == ids


async def test_wait_for_delivery_polls_until_delivered_or_canceled(
    aclient: AsyncAIWorkPay, api: FakeApi, sleeps: List[float]
) -> None:
    mission = api.add_mission(status="ASSIGNED")

    async def tick(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            api.advance(mission["id"], status="DELIVERED")

    aclient._sleep = tick
    assert (
        await aclient.wait_for_delivery(mission["id"], interval=2)
    ).status == MissionStatus.DELIVERED and sleeps == [2, 2]
    api.advance(mission["id"], status="CANCELED")
    with pytest.raises(MissionCanceledError):
        await aclient.wait_for_delivery(mission["id"])


async def test_proofs_stream_and_network_errors(aclient: AsyncAIWorkPay, api: FakeApi) -> None:
    api.files["f1"] = (b"\xff\xd8\xff\xe0", "image/jpeg")
    mission_id = proof_mission(api)
    assert (await aclient.download_proof(mission_id, "f1")).content == b"\xff\xd8\xff\xe0"
    api.fail = [httpx.ConnectError("boom")] * 3
    with pytest.raises(APIConnectionError):
        await aclient.get_mission(mission_id)


async def test_the_async_client_closes_its_own_connection() -> None:
    async with AsyncAIWorkPay("awp_abcdefghijkl", base_url="http://localhost:9") as sdk:
        assert "awp_abcdefghijkl" not in repr(sdk)
    assert sdk._http.is_closed
