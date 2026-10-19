"""I/O-free core: what each call sends, how an answer is read, when to retry.

The sync and async clients only differ in how they send a request and wait; the
operations, validation and retry rules here are shared.
"""

from __future__ import annotations

import json as _json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Generic, List, Mapping, Optional, Tuple, TypeVar, Union
from urllib.parse import quote, urlparse

from ._version import __version__
from .errors import AIWorkPayError, error_from_response
from .models import (
    Account,
    Approval,
    Cancellation,
    ChangesRequest,
    Commission,
    CreatedMission,
    Mission,
    Payment,
    Proof,
    Transaction,
    TransactionPage,
    WebhookConfig,
    WebhookDelivery,
    _dec,
    _dt,
)

T = TypeVar("T")

DEFAULT_BASE_URL = "https://app.aiworkpay.fr"
USER_AGENT = f"aiworkpay-python/{__version__}"
MAX_PROOF_BYTES = 15 * 1024 * 1024
ERROR_BODY_LIMIT = 64 * 1024

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_.:-]{8,255}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


# ── Configuration ────────────────────────────────────────────────────────────


def resolve_credentials(api_key: Optional[str], base_url: Optional[str]) -> Tuple[str, str]:
    """Read the key and the URL from the arguments, then from the environment."""
    key = api_key or os.environ.get("AIWORKPAY_API_KEY")
    if not key:
        raise ValueError("api_key is required (or set AIWORKPAY_API_KEY)")
    if not key.startswith("awp_"):
        raise ValueError("api_key must be an AIWorkPay agent key (awp_...)")

    url = (base_url or os.environ.get("AIWORKPAY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("base_url must be an http(s) URL")
    # The key travels in every request: never over clear text, except to this machine
    if parsed.scheme == "http" and parsed.hostname not in _LOCAL_HOSTS:
        raise ValueError("base_url must use https (http is only accepted for localhost)")
    return key, url


def new_idempotency_key() -> str:
    return f"awp-sdk-{secrets.token_hex(16)}"


# ── Requests and answers ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class RawResponse:
    status_code: int
    headers: Mapping[str, str]
    content: bytes

    def json(self) -> Any:
        try:
            return _json.loads(self.content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def header(self, name: str) -> Optional[str]:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


@dataclass(frozen=True)
class Operation(Generic[T]):
    method: str
    path: str
    parse: Callable[[RawResponse], T]
    params: Optional[Dict[str, str]] = None
    json: Any = None
    headers: Optional[Dict[str, str]] = None
    #: "safe": retry on 429, 5xx and network errors (reads, and writes the server deduplicates).
    #: "rate_limit_only": retry on 429 alone (the request was refused before being processed).
    retry: str = "safe"
    #: Stream the body and stop beyond this many bytes (file downloads).
    max_bytes: Optional[int] = None


def _payload(response: RawResponse) -> Any:
    data = response.json()
    if data is None:
        raise AIWorkPayError(
            "AIWorkPay returned an invalid response",
            status_code=response.status_code,
            code="INVALID_RESPONSE",
            request_id=response.header("x-request-id"),
        )
    return data


def read_error(response: RawResponse, request_id: str) -> AIWorkPayError:
    retry_after: Optional[float] = None
    raw = response.header("retry-after")
    if raw is not None:
        try:
            retry_after = float(raw)
        except ValueError:
            retry_after = None
    return error_from_response(response.status_code, response.json(), request_id=request_id, retry_after=retry_after)


# ── Retry policy ─────────────────────────────────────────────────────────────

_RETRY_STATUSES = {500, 502, 503, 504}


@dataclass
class RetryPolicy:
    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 8.0
    #: A 429 asking to wait longer than this is raised instead of slept on.
    max_retry_after: float = 30.0

    def delay(
        self, op: Operation[Any], attempt: int, jitter: Callable[[], float], response: Optional[RawResponse]
    ) -> Optional[float]:
        """Seconds to wait before attempt ``attempt + 1``, or None to give up.

        ``response`` is None after a network error or a timeout.
        """
        if attempt >= self.max_retries:
            return None
        backoff = min(self.max_delay, self.base_delay * float(2**attempt)) * jitter()
        if response is None:
            return backoff if op.retry == "safe" else None
        if response.status_code == 429:
            raw = response.header("retry-after")
            try:
                asked = float(raw) if raw is not None else 0.0
            except ValueError:
                asked = 0.0
            if asked > self.max_retry_after:
                return None
            return max(asked, backoff)
        if response.status_code in _RETRY_STATUSES and op.retry == "safe":
            return backoff
        return None


# ── Validation of arguments ──────────────────────────────────────────────────


def _id(value: str, what: str = "mission_id") -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise ValueError(f"{what} must be a non-empty string")
    return quote(value.strip(), safe="")


def _budget(value: Union[int, float, str, Decimal]) -> float:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("budget must be a number") from error
    if not amount.is_finite() or amount <= 0:
        raise ValueError("budget must be positive")
    if amount.as_tuple().exponent < -2:  # type: ignore[operator]
        raise ValueError("budget has at most 2 decimals")
    return float(amount)


def _deadline(value: Union[str, date, datetime]) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("deadline must be timezone-aware (e.g. datetime.now(timezone.utc) + timedelta(days=3))")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("deadline must be a datetime, a date or an ISO 8601 string")


_PRIORITIES = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH"}


# ── Operations ───────────────────────────────────────────────────────────────


def create_mission(
    *,
    title: str,
    description: str,
    budget: Union[int, float, str, Decimal],
    deadline: Union[str, date, datetime],
    currency: str,
    priority: str,
    payment_mode: str,
    payment_method_id: Optional[str],
    idempotency_key: Optional[str],
) -> Operation[CreatedMission]:
    if not isinstance(title, str) or not 3 <= len(title.strip()) <= 200:
        raise ValueError("title must have 3 to 200 characters")
    if not isinstance(description, str) or len(description.strip()) < 10:
        raise ValueError("description must have at least 10 characters")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency):
        raise ValueError("currency must be an ISO 4217 code such as EUR")
    if priority.lower() not in _PRIORITIES:
        raise ValueError("priority must be low, medium or high")
    if payment_mode not in ("checkout", "payment_intent"):
        raise ValueError("payment_mode must be checkout or payment_intent")
    if payment_method_id and payment_mode == "checkout":
        raise ValueError("payment_method_id only applies to payment_mode='payment_intent'")
    key = idempotency_key or new_idempotency_key()
    if not _IDEMPOTENCY_KEY.match(key):
        raise ValueError("idempotency_key must have 8 to 255 characters among A-Z a-z 0-9 _ . : -")

    body: Dict[str, Any] = {
        "title": title.strip(),
        "description": description.strip(),
        "budget": _budget(budget),
        "currency": currency.upper(),
        "deadline": _deadline(deadline),
        "priority": _PRIORITIES[priority.lower()],
        "paymentMode": payment_mode,
    }
    if payment_method_id:
        body["paymentMethodId"] = payment_method_id

    def parse(response: RawResponse) -> CreatedMission:
        data = _payload(response)
        return CreatedMission(
            mission=Mission.from_api(data["mission"]),
            payment=Payment.from_api(data.get("payment")),
            replayed=response.header("idempotent-replayed") == "true",
        )

    # The key stays the same on every retry: a repeated call returns the mission, never a second one
    return Operation("POST", "/api/missions", parse, json=body, headers={"Idempotency-Key": key})


def get_mission(mission_id: str) -> Operation[Mission]:
    return Operation("GET", f"/api/missions/{_id(mission_id)}", lambda r: Mission.from_api(_payload(r)))


def list_missions(status: Optional[str], limit: int, skip: int) -> Operation[List[Mission]]:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if not 0 <= skip <= 10_000:
        raise ValueError("skip must be between 0 and 10000")
    params = {"limit": str(limit), "skip": str(skip)}
    if status:
        params["status"] = getattr(status, "value", status)
    return Operation("GET", "/api/missions", lambda r: [Mission.from_api(m) for m in _payload(r)], params=params)


def approve_mission(mission_id: str) -> Operation[Approval]:
    def parse(response: RawResponse) -> Approval:
        data = _payload(response)
        raw = data.get("commission")
        commission = (
            Commission(
                _dec(raw["payworkerAmount"]) or Decimal(0),
                _dec(raw["platformFeeAmount"]) or Decimal(0),
                raw["transferStatus"],
            )
            if raw
            else None
        )
        return Approval(Mission.from_api(data["mission"]), bool(data.get("alreadyApproved")), commission)

    # Approving twice is answered with alreadyApproved: safe to retry
    return Operation("POST", f"/api/missions/{_id(mission_id)}/approve", parse)


def request_changes(mission_id: str, feedback: str) -> Operation[ChangesRequest]:
    if not isinstance(feedback, str) or not 10 <= len(feedback.strip()) <= 5000:
        raise ValueError("feedback must have 10 to 5000 characters")

    def parse(response: RawResponse) -> ChangesRequest:
        data = _payload(response)
        return ChangesRequest(Mission.from_api(data["mission"]), int(data["revisionsLeft"]))

    # Each success uses one of the 3 revisions: never replayed after an unknown outcome
    return Operation(
        "POST",
        f"/api/missions/{_id(mission_id)}/request-changes",
        parse,
        json={"feedback": feedback.strip()},
        retry="rate_limit_only",
    )


def cancel_mission(mission_id: str) -> Operation[Cancellation]:
    def parse(response: RawResponse) -> Cancellation:
        data = _payload(response)
        return Cancellation(Mission.from_api(data["mission"]), str(data["payment"]))

    return Operation("POST", f"/api/missions/{_id(mission_id)}/cancel", parse)


def download_proof(mission_id: str, proof_id: str, max_bytes: int) -> Operation[Proof]:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")

    def parse(response: RawResponse) -> Proof:
        return Proof(response.content, response.header("content-type") or "application/octet-stream")

    # The path is built from the ids on the configured base URL: an attachment's own url is never followed
    return Operation(
        "GET",
        f"/api/missions/{_id(mission_id)}/proofs/{_id(proof_id, 'proof_id')}",
        parse,
        headers={"Accept": "*/*"},
        max_bytes=max_bytes,
    )


def get_account() -> Operation[Account]:
    return Operation("GET", "/api/account", lambda r: Account.from_api(_payload(r)))


def list_transactions(mission_id: Optional[str], limit: int, skip: int) -> Operation[TransactionPage]:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if not 0 <= skip <= 10_000:
        raise ValueError("skip must be between 0 and 10000")
    params = {"limit": str(limit), "skip": str(skip)}
    if mission_id:
        params["missionId"] = mission_id

    def parse(response: RawResponse) -> TransactionPage:
        data = _payload(response)
        return TransactionPage(int(data["total"]), tuple(Transaction.from_api(t) for t in data["transactions"]))

    return Operation("GET", "/api/transactions", parse, params=params)


def _webhook_config(data: Mapping[str, Any]) -> WebhookConfig:
    hook = data.get("webhook") or {}
    return WebhookConfig(
        url=hook.get("url"),
        events=tuple(data.get("events") or ()),
        secret=data.get("secret"),
        updated_at=_dt(hook.get("updatedAt")),
        recent_deliveries=tuple(WebhookDelivery.from_api(d) for d in data.get("recentDeliveries") or ()),
    )


def get_webhook() -> Operation[WebhookConfig]:
    return Operation("GET", "/api/webhook", lambda r: _webhook_config(_payload(r)))


def set_webhook(url: str, rotate_secret: bool) -> Operation[WebhookConfig]:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url is required")
    body: Dict[str, Any] = {"url": url.strip()}
    if rotate_secret:
        body["rotateSecret"] = True
    # A rotation issues a new secret each time: not replayed after an unknown outcome
    return Operation(
        "PUT",
        "/api/webhook",
        lambda r: _webhook_config(_payload(r)),
        json=body,
        retry="rate_limit_only" if rotate_secret else "safe",
    )


def delete_webhook() -> Operation[None]:
    return Operation("DELETE", "/api/webhook", lambda r: None)


def send_webhook_test() -> Operation[str]:
    return Operation("POST", "/api/webhook/test", lambda r: str(_payload(r)["eventId"]), retry="rate_limit_only")
