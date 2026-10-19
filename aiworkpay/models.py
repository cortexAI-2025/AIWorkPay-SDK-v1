"""Typed results returned by the client.

Amounts are :class:`~decimal.Decimal`, dates are timezone-aware
:class:`~datetime.datetime`. Every object keeps the answer it was built from in
``raw`` (not part of the stable interface: fields may be added there first).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple, Union


def _dec(value: Any) -> Optional[Decimal]:
    return None if value is None else Decimal(str(value))


def _dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class MissionStatus(str, Enum):
    """Where a mission stands. Compares equal to the API's own strings."""

    PAYMENT_PENDING = "PAYMENT_PENDING"  # created, waiting for the human owner to pay
    CREATED = "CREATED"
    PUBLISHED = "PUBLISHED"  # paid, open to Payworkers
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    DELIVERED = "DELIVERED"  # the result can be read; approve it or request changes
    COMPLETED = "COMPLETED"  # approved and paid
    CANCELED = "CANCELED"


def _status(value: Any) -> Union[MissionStatus, str]:
    try:
        return MissionStatus(value)
    except ValueError:
        return str(value)


@dataclass(frozen=True)
class Attachment:
    """A file or link attached to a mission: its brief, or the proofs delivered by the Payworker."""

    id: str
    filename: str
    kind: str  # "BRIEF" or "PROOF"
    source: Optional[str] = None  # "upload" (read with download_proof) or "link"
    url: Optional[str] = None
    mime_type: Optional[str] = None
    size: Optional[int] = None
    scan_status: Optional[str] = None  # "CLEAN", or "NOT_SCANNED" if antivirus was off
    created_at: Optional[datetime] = None

    @property
    def is_uploaded(self) -> bool:
        return self.source == "upload"

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "Attachment":
        return cls(
            id=data["id"],
            filename=data.get("filename", ""),
            kind=data.get("kind", ""),
            source=data.get("source"),
            url=data.get("url"),
            mime_type=data.get("mimeType"),
            size=data.get("size"),
            scan_status=data.get("scanStatus"),
            created_at=_dt(data.get("createdAt")),
        )


@dataclass(frozen=True)
class Mission:
    """A mission ordered with your key."""

    id: str
    title: str
    description: str
    budget: Decimal
    currency: str
    status: Union[MissionStatus, str]
    priority: Optional[str] = None
    deadline: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    checkout_url: Optional[str] = None
    checkout_expires_at: Optional[datetime] = None
    result_note: Optional[str] = None
    result_data: Any = None
    delivered_at: Optional[datetime] = None
    revision_count: int = 0
    revision_feedback: Optional[str] = None
    payworker_amount: Optional[Decimal] = None
    platform_fee_amount: Optional[Decimal] = None
    attachments: Tuple[Attachment, ...] = ()
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def result_available(self) -> bool:
        """The Payworker's result can be read (delivered, or already approved)."""
        return self.status in (MissionStatus.DELIVERED, MissionStatus.COMPLETED)

    @property
    def is_final(self) -> bool:
        return self.status in (MissionStatus.COMPLETED, MissionStatus.CANCELED)

    @property
    def awaiting_payment(self) -> bool:
        return self.status in (MissionStatus.PAYMENT_PENDING, MissionStatus.CREATED)

    @property
    def proofs(self) -> Tuple[Attachment, ...]:
        return tuple(a for a in self.attachments if a.kind == "PROOF")

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "Mission":
        """Build from an API answer or from the ``mission`` of a webhook event."""
        return cls(
            id=data["id"],
            title=data.get("title", ""),
            description=data.get("description", ""),
            budget=_dec(data.get("budget")) or Decimal(0),
            currency=data.get("currency", "EUR"),
            status=_status(data.get("status")),
            priority=data.get("priority"),
            deadline=_dt(data.get("deadline")),
            created_at=_dt(data.get("createdAt")),
            updated_at=_dt(data.get("updatedAt")),
            checkout_url=data.get("checkoutUrl"),
            checkout_expires_at=_dt(data.get("checkoutExpiresAt")),
            result_note=data.get("resultNote"),
            result_data=data.get("resultData"),
            delivered_at=_dt(data.get("deliveredAt")),
            revision_count=int(data.get("revisionCount") or 0),
            revision_feedback=data.get("revisionFeedback"),
            payworker_amount=_dec(data.get("payworkerAmount")),
            platform_fee_amount=_dec(data.get("platformFeeAmount")),
            attachments=tuple(Attachment.from_api(a) for a in data.get("attachments") or ()),
            raw=dict(data),
        )


@dataclass(frozen=True)
class Payment:
    """How a created mission is paid. With ``checkout`` (the default), give ``url`` to your human owner."""

    mode: str  # "checkout" or "payment_intent"
    url: Optional[str] = None
    checkout_session_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    payment_intent_id: Optional[str] = None
    client_secret: Optional[str] = field(default=None, repr=False)

    @classmethod
    def from_api(cls, data: Optional[Mapping[str, Any]]) -> Optional["Payment"]:
        if not data:
            return None
        return cls(
            mode=data.get("mode", ""),
            url=data.get("url"),
            checkout_session_id=data.get("checkoutSessionId"),
            expires_at=_dt(data.get("expiresAt")),
            payment_intent_id=data.get("paymentIntentId"),
            client_secret=data.get("clientSecret"),
        )


@dataclass(frozen=True)
class CreatedMission:
    mission: Mission
    payment: Optional[Payment]
    #: True when the Idempotency-Key had already created this mission: nothing new was created.
    replayed: bool = False


@dataclass(frozen=True)
class Commission:
    payworker_amount: Decimal
    platform_fee_amount: Decimal
    transfer_status: str  # "SUCCEEDED" or "PENDING"


@dataclass(frozen=True)
class Approval:
    mission: Mission
    already_approved: bool
    commission: Optional[Commission] = None


@dataclass(frozen=True)
class ChangesRequest:
    mission: Mission
    revisions_left: int


@dataclass(frozen=True)
class Cancellation:
    mission: Mission
    #: "checkout_closed", "refunded", "payment_canceled", "nothing_to_release" or "already_canceled"
    payment: str


@dataclass(frozen=True)
class Proof:
    """The content of an uploaded proof file."""

    content: bytes = field(repr=False)
    content_type: str


# ── Account ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KeyInfo:
    id: str
    label: str
    prefix: str
    scopes: Tuple[str, ...]
    active: bool
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class Limits:
    max_mission_budget: Optional[Decimal]
    monthly_budget: Optional[Decimal]


@dataclass(frozen=True)
class Usage:
    month_start: Optional[datetime]
    committed_this_month: Decimal
    remaining_this_month: Optional[Decimal]
    total_paid: Decimal
    total_refunded: Decimal
    net_paid: Decimal


@dataclass(frozen=True)
class Account:
    """Your key, its limits and what it has spent. Amounts are summed in one currency."""

    key: KeyInfo
    limits: Limits
    usage: Usage
    missions_total: int
    missions_by_status: Mapping[str, int]
    webhook_configured: bool
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def max_next_mission_budget(self) -> Optional[Decimal]:
        """The largest budget the next mission may have under the key's own caps (None: no cap)."""
        ceilings = [c for c in (self.limits.max_mission_budget, self.usage.remaining_this_month) if c is not None]
        return min(ceilings) if ceilings else None

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "Account":
        key, limits, usage, missions = data["key"], data["limits"], data["usage"], data["missions"]
        return cls(
            key=KeyInfo(
                id=key["id"],
                label=key["label"],
                prefix=key["prefix"],
                scopes=tuple(key.get("scopes") or ()),
                active=bool(key.get("active")),
                expires_at=_dt(key.get("expiresAt")),
                last_used_at=_dt(key.get("lastUsedAt")),
                created_at=_dt(key.get("createdAt")),
            ),
            limits=Limits(_dec(limits.get("maxMissionBudget")), _dec(limits.get("monthlyBudget"))),
            usage=Usage(
                month_start=_dt(usage.get("monthStart")),
                committed_this_month=_dec(usage.get("committedThisMonth")) or Decimal(0),
                remaining_this_month=_dec(usage.get("remainingThisMonth")),
                total_paid=_dec(usage.get("totalPaid")) or Decimal(0),
                total_refunded=_dec(usage.get("totalRefunded")) or Decimal(0),
                net_paid=_dec(usage.get("netPaid")) or Decimal(0),
            ),
            missions_total=int(missions.get("total", 0)),
            missions_by_status=dict(missions.get("byStatus") or {}),
            webhook_configured=bool((data.get("webhook") or {}).get("configured")),
            raw=dict(data),
        )


@dataclass(frozen=True)
class Transaction:
    """A payment booked on your key."""

    id: str
    amount: Decimal
    currency: str
    status: str
    mission_id: Optional[str]
    mission_status: Optional[Union[MissionStatus, str]]
    #: The mission was canceled, so the payment was given back.
    refunded: bool
    created_at: Optional[datetime]

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "Transaction":
        return cls(
            id=data["id"],
            amount=_dec(data["amount"]) or Decimal(0),
            currency=data.get("currency", "EUR"),
            status=data.get("status", ""),
            mission_id=data.get("missionId"),
            mission_status=_status(data["missionStatus"]) if data.get("missionStatus") else None,
            refunded=bool(data.get("refunded")),
            created_at=_dt(data.get("createdAt")),
        )


@dataclass(frozen=True)
class TransactionPage:
    total: int
    transactions: Tuple[Transaction, ...]

    def __iter__(self) -> Any:
        return iter(self.transactions)

    def __len__(self) -> int:
        return len(self.transactions)


# ── Webhooks ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WebhookDelivery:
    """One attempt series to deliver an event to your endpoint."""

    event_id: str
    type: str
    status: str  # "PENDING", "DELIVERED" or "FAILED"
    attempts: int
    mission_id: Optional[str] = None
    last_status_code: Optional[int] = None
    last_error: Optional[str] = None
    next_attempt_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "WebhookDelivery":
        return cls(
            event_id=data["eventId"],
            type=data.get("type", ""),
            status=data.get("status", ""),
            attempts=int(data.get("attempts") or 0),
            mission_id=data.get("missionId"),
            last_status_code=data.get("lastStatusCode"),
            last_error=data.get("lastError"),
            next_attempt_at=_dt(data.get("nextAttemptAt")),
            created_at=_dt(data.get("createdAt")),
            delivered_at=_dt(data.get("deliveredAt")),
        )


@dataclass(frozen=True)
class WebhookConfig:
    """The webhook of your key."""

    url: Optional[str]
    events: Tuple[str, ...] = ()
    #: Signing secret. Only returned when it was just issued: store it, it cannot be read back.
    secret: Optional[str] = field(default=None, repr=False)
    updated_at: Optional[datetime] = None
    recent_deliveries: Tuple[WebhookDelivery, ...] = ()


@dataclass(frozen=True)
class WebhookEvent:
    """A verified notification sent by AIWorkPay (see :func:`aiworkpay.construct_event`)."""

    id: str
    type: str
    created: datetime
    data: Mapping[str, Any]

    @property
    def mission(self) -> Optional[Mission]:
        """The mission the event is about (None for ``webhook.ping``)."""
        raw = self.data.get("mission")
        return Mission.from_api(raw) if isinstance(raw, Mapping) else None

    @property
    def reason(self) -> Optional[str]:
        """For ``mission.canceled``: ``agent_request``, ``admin``, ``checkout_expired`` or ``payment_failed``."""
        value = self.data.get("reason")
        return value if isinstance(value, str) else None
