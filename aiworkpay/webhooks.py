"""Verify the notifications AIWorkPay sends to your webhook.

Every delivery carries ``AiWorkPay-Signature: t=<unix seconds>,v1=<hex>``, the
HMAC-SHA256 of ``"<t>.<raw body>"`` with your webhook secret. Verify it on the
**raw** body, before decoding it::

    event = construct_event(request.body, request.headers["AiWorkPay-Signature"], SECRET)
    if event.type == "mission.delivered":
        mission = event.mission

Delivery is at least once and unordered: deduplicate on ``event.id``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Union

from .errors import SignatureVerificationError
from .models import WebhookEvent

#: Tolerated difference between the signature date and now, against replays.
DEFAULT_TOLERANCE_SECONDS = 300


def _body(payload: Union[bytes, str]) -> bytes:
    return payload if isinstance(payload, bytes) else payload.encode("utf-8")


def _parse_header(header: str) -> Dict[str, List[str]]:
    parts: Dict[str, List[str]] = {}
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        if key and value:
            parts.setdefault(key, []).append(value)
    return parts


def sign(secret: str, payload: Union[bytes, str], timestamp: Optional[int] = None) -> str:
    """Build a signature header, as AIWorkPay does (useful to test your endpoint)."""
    t = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode("utf-8"), f"{t}.".encode("ascii") + _body(payload), hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify_signature(
    payload: Union[bytes, str],
    header: Optional[str],
    secret: str,
    *,
    tolerance: Optional[int] = DEFAULT_TOLERANCE_SECONDS,
    now: Optional[float] = None,
) -> bool:
    """True when ``header`` is a valid, recent signature of ``payload`` (the raw body)."""
    if not header or not secret:
        return False
    parts = _parse_header(header)
    try:
        timestamp = int(parts["t"][0])
    except (KeyError, ValueError):
        return False
    if tolerance is not None and abs((time.time() if now is None else now) - timestamp) > tolerance:
        return False
    expected = sign(secret, payload, timestamp).split("v1=", 1)[1]
    return any(hmac.compare_digest(expected, candidate) for candidate in parts.get("v1", ()))


def construct_event(
    payload: Union[bytes, str],
    header: Optional[str],
    secret: str,
    *,
    tolerance: Optional[int] = DEFAULT_TOLERANCE_SECONDS,
) -> WebhookEvent:
    """Verify the signature, then decode the event.

    Raises:
        SignatureVerificationError: the signature is missing, wrong or too old.
    """
    if not verify_signature(payload, header, secret, tolerance=tolerance):
        raise SignatureVerificationError("Invalid or expired webhook signature")
    try:
        data = json.loads(_body(payload))
        return WebhookEvent(
            id=data["id"],
            type=data["type"],
            created=datetime.fromtimestamp(int(data["created"]), tz=timezone.utc),
            data=data.get("data") or {},
        )
    except (ValueError, KeyError, TypeError) as error:
        raise SignatureVerificationError("The webhook body is not a valid event") from error
