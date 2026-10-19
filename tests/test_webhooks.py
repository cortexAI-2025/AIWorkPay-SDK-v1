from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from aiworkpay import MissionStatus, SignatureVerificationError, construct_event, sign, verify_signature

# Produced by the signing code of Aiworkpay-v2 (lib/webhookSecurity.ts, signPayload):
# the SDK must verify exactly what the server signs.
SECRET = "awhsec_goldenSecretForTests"
TIMESTAMP = 1792400000
BODY = (
    '{"id":"evt_golden","type":"mission.delivered","created":1792400000,"data":{"mission":{"id":"msn_1","status":"DELIVERED",'
    '"title":"Vérifier 20 fiches","budget":25,"currency":"EUR","resultNote":"Fait é€"}}}'
)
HEADER = "t=1792400000,v1=d4bfca559a99db862f57e3471bbc7f4d60f6e63052456dd54672141393d63137"
NOW = TIMESTAMP + 10


def test_it_verifies_a_signature_produced_by_the_server() -> None:
    assert sign(SECRET, BODY, TIMESTAMP) == HEADER
    assert verify_signature(BODY, HEADER, SECRET, now=NOW)
    assert verify_signature(BODY.encode("utf-8"), HEADER, SECRET, now=NOW), "bytes and str are the same body"


def test_it_rejects_anything_that_was_changed() -> None:
    assert not verify_signature(BODY + " ", HEADER, SECRET, now=NOW)
    assert not verify_signature(BODY.replace("25", "26"), HEADER, SECRET, now=NOW)
    assert not verify_signature(BODY, HEADER, SECRET + "x", now=NOW)
    assert not verify_signature(BODY, HEADER.replace("d4bf", "0000"), SECRET, now=NOW)
    assert not verify_signature(BODY, HEADER.replace("t=1792400000", "t=1792400001"), SECRET, now=NOW)


@pytest.mark.parametrize("header", [None, "", "garbage", "t=abc,v1=00", "v1=00", "t=1792400000", "t=1792400000,v1="])
def test_it_rejects_a_missing_or_malformed_header(header: object) -> None:
    assert not verify_signature(BODY, header, SECRET, now=NOW)  # type: ignore[arg-type]
    assert not verify_signature(BODY, HEADER, "", now=NOW)


def test_it_rejects_a_replay_outside_the_tolerance() -> None:
    assert not verify_signature(BODY, HEADER, SECRET, now=TIMESTAMP + 301)
    assert not verify_signature(BODY, HEADER, SECRET, now=TIMESTAMP - 301)
    assert verify_signature(BODY, HEADER, SECRET, now=TIMESTAMP + 301, tolerance=600)
    assert verify_signature(BODY, HEADER, SECRET, now=TIMESTAMP + 10**6, tolerance=None)


def test_a_second_signature_is_accepted_during_a_secret_rotation() -> None:
    header = HEADER + ",v1=" + "0" * 64
    assert verify_signature(BODY, header, SECRET, now=NOW)
    assert verify_signature(BODY, "t=1792400000,v1=" + "0" * 64 + ",v1=" + HEADER.split("v1=")[1], SECRET, now=NOW)


def test_construct_event_decodes_the_event_and_its_mission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aiworkpay.webhooks.time.time", lambda: NOW)
    event = construct_event(BODY, HEADER, SECRET)
    assert event.id == "evt_golden" and event.type == "mission.delivered"
    assert event.created == datetime(2026, 10, 19, 8, 53, 20, tzinfo=timezone.utc)
    mission = event.mission
    assert mission is not None and mission.status == MissionStatus.DELIVERED and mission.result_note == "Fait é€"
    assert mission.title == "Vérifier 20 fiches" and mission.result_available


def test_construct_event_refuses_a_bad_signature_and_a_bad_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aiworkpay.webhooks.time.time", lambda: NOW)
    with pytest.raises(SignatureVerificationError):
        construct_event(BODY, "t=1792400000,v1=" + "0" * 64, SECRET)
    not_an_event = '{"hello": "world"}'
    with pytest.raises(SignatureVerificationError):
        construct_event(not_an_event, sign(SECRET, not_an_event, TIMESTAMP), SECRET)
    with pytest.raises(SignatureVerificationError):
        construct_event("pas du json", sign(SECRET, "pas du json", TIMESTAMP), SECRET)


def test_ping_and_cancellation_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aiworkpay.webhooks.time.time", lambda: NOW)
    ping = json.dumps(
        {"id": "evt_1", "type": "webhook.ping", "created": TIMESTAMP, "data": {"message": "Test AiWorkPay"}}
    )
    assert construct_event(ping, sign(SECRET, ping, TIMESTAMP), SECRET).mission is None
    canceled = json.dumps(
        {
            "id": "evt_2",
            "type": "mission.canceled",
            "created": TIMESTAMP,
            "data": {"mission": {"id": "m", "status": "CANCELED", "budget": 10}, "reason": "checkout_expired"},
        }
    )
    event = construct_event(canceled, sign(SECRET, canceled, TIMESTAMP), SECRET)
    assert event.reason == "checkout_expired" and event.mission is not None and event.mission.status == "CANCELED"
