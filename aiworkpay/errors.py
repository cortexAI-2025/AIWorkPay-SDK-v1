"""Exceptions raised by the SDK.

Every error that comes from the API carries the stable ``code`` of the answer
(``MISSION_NOT_FOUND``, ``BUDGET_LIMIT_EXCEEDED``...), the HTTP ``status_code``,
the ``request_id`` to quote to support, and any ``details``.
"""

from __future__ import annotations

from typing import Any, Optional


class AIWorkPayError(Exception):
    """Base class of every error raised by the SDK."""

    #: Retrying the same call later may succeed (rate limit, outage, network).
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
        details: Any = None,
        request_id: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.details = details
        self.request_id = request_id
        self.retry_after = retry_after

    def __str__(self) -> str:
        parts = [self.message]
        if self.code:
            parts.append(f"[{self.code}]")
        if self.request_id:
            parts.append(f"(request {self.request_id})")
        return " ".join(parts)


# ── Connection ───────────────────────────────────────────────────────────────


class APIConnectionError(AIWorkPayError):
    """AIWorkPay could not be reached."""

    retryable = True


class APITimeoutError(APIConnectionError):
    """AIWorkPay did not answer in time."""


# ── Answers of the API ───────────────────────────────────────────────────────


class AuthenticationError(AIWorkPayError):
    """The API key is missing, invalid, expired or revoked (401, or 403 ``INVALID_API_KEY``)."""


class InsufficientScopeError(AIWorkPayError):
    """The key lacks the permission the call needs (403 ``INSUFFICIENT_SCOPE``)."""

    @property
    def required_scope(self) -> Optional[str]:
        return self.details.get("requiredScope") if isinstance(self.details, dict) else None


class BudgetLimitError(AIWorkPayError):
    """A spending cap of the key would be exceeded (``BUDGET_LIMIT_EXCEEDED``, ``MONTHLY_BUDGET_EXCEEDED``)."""


class ValidationError(AIWorkPayError):
    """The request was refused as invalid (400)."""


class NotFoundError(AIWorkPayError):
    """The mission or route does not exist for this key (404)."""


class ConflictError(AIWorkPayError):
    """The mission is not in a state that allows this call (409).

    ``code`` tells which: ``MISSION_NOT_DELIVERED``, ``MISSION_NOT_CANCELABLE``,
    ``REVISION_LIMIT_REACHED``, ``MISSION_CHANGED``.
    """


class IdempotencyKeyReusedError(AIWorkPayError):
    """The Idempotency-Key was already used for a different request (422)."""


class PaymentError(AIWorkPayError):
    """The payment was refused (402 ``PAYMENT_FAILED``, 422 ``PAYMENT_REJECTED``)."""


class PaymentProviderUnavailableError(AIWorkPayError):
    """Stripe could not be reached (502). Nothing was charged; retrying with the same key is safe."""

    retryable = True


class RateLimitError(AIWorkPayError):
    """Too many requests (429). ``retry_after`` is the number of seconds to wait."""

    retryable = True


class ServerError(AIWorkPayError):
    """AIWorkPay failed (5xx)."""

    retryable = True


# ── Raised by the helpers ────────────────────────────────────────────────────


class WaitTimeoutError(AIWorkPayError):
    """``wait_for_delivery`` gave up before the mission was delivered."""


class MissionCanceledError(AIWorkPayError):
    """The mission was canceled while waiting for its result."""


class ProofTooLargeError(AIWorkPayError):
    """A proof file is larger than the allowed size."""


class SignatureVerificationError(AIWorkPayError):
    """A webhook signature is missing, malformed, stale or wrong."""


_CONFLICT_CODES = {"MISSION_NOT_DELIVERED", "MISSION_NOT_CANCELABLE", "REVISION_LIMIT_REACHED", "MISSION_CHANGED"}


def error_from_response(
    status: int,
    body: Any,
    *,
    request_id: Optional[str],
    retry_after: Optional[float],
) -> AIWorkPayError:
    """Turn a non-2xx answer into the matching exception."""
    payload = body if isinstance(body, dict) else {}
    raw_code, raw_message = payload.get("code"), payload.get("error")
    code = raw_code if isinstance(raw_code, str) else None
    message = raw_message if isinstance(raw_message, str) else f"AIWorkPay answered HTTP {status}"
    kwargs: dict[str, Any] = {
        "status_code": status,
        "code": code,
        "details": payload.get("details"),
        "request_id": request_id,
        "retry_after": retry_after,
    }

    cls: type[AIWorkPayError]
    if status == 401 or code == "INVALID_API_KEY":
        cls = AuthenticationError
    elif code == "INSUFFICIENT_SCOPE":
        cls = InsufficientScopeError
    elif code in ("BUDGET_LIMIT_EXCEEDED", "MONTHLY_BUDGET_EXCEEDED"):
        cls = BudgetLimitError
    elif status == 429 or code == "RATE_LIMITED":
        cls = RateLimitError
    elif code == "IDEMPOTENCY_KEY_REUSED":
        cls = IdempotencyKeyReusedError
    elif code == "PAYMENT_PROVIDER_UNAVAILABLE":
        cls = PaymentProviderUnavailableError
    elif code in ("PAYMENT_FAILED", "PAYMENT_REJECTED") or status == 402:
        cls = PaymentError
    elif status == 404:
        cls = NotFoundError
    elif status == 409 or code in _CONFLICT_CODES:
        cls = ConflictError
    elif status == 400:
        cls = ValidationError
    elif status >= 500:
        cls = ServerError
    else:
        cls = AIWorkPayError
    return cls(message, **kwargs)
