"""Asynchronous client (``asyncio``): the same operations as :class:`aiworkpay.AIWorkPay`."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from random import random
from types import TracebackType
from typing import Any, AsyncIterator, Awaitable, Callable, List, Optional, Type, TypeVar, Union

import httpx

from . import _core as core
from .errors import (
    AIWorkPayError,
    APIConnectionError,
    APITimeoutError,
    MissionCanceledError,
    ProofTooLargeError,
    WaitTimeoutError,
)
from .models import (
    Account,
    Approval,
    Cancellation,
    ChangesRequest,
    CreatedMission,
    Mission,
    MissionStatus,
    Proof,
    TransactionPage,
    WebhookConfig,
)

T = TypeVar("T")


class AsyncAIWorkPay:
    """Asynchronous client of the AIWorkPay agent API (use it with ``async with``).

    Args:
        api_key: Your agent key (``awp_...``); defaults to ``AIWORKPAY_API_KEY``.
        base_url: Defaults to ``AIWORKPAY_BASE_URL``, then ``https://app.aiworkpay.fr``.
        timeout: Seconds before a request is given up.
        max_retries: Retries of a call after a rate limit, an outage or a network error.
            Writes are only retried when AIWorkPay deduplicates them (see ``create_mission``).
        http_client: Your own ``httpx.AsyncClient`` (proxy, transport...). It is not closed by ``close()``.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._key, self._base = core.resolve_credentials(api_key, base_url)
        self._retry = core.RetryPolicy(max_retries=max_retries)
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self._jitter: Callable[[], float] = lambda: 0.5 + random() / 2

    def __repr__(self) -> str:
        return f"AsyncAIWorkPay(base_url={self._base!r}, api_key='{self._key[:12]}…')"

    # ── Transport ────────────────────────────────────────────────────────────

    async def _send(self, op: core.Operation[Any], request_id: str) -> core.RawResponse:
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
            "User-Agent": core.USER_AGENT,
            "X-Request-Id": request_id,
            **(op.headers or {}),
        }
        url = self._base + op.path
        try:
            if op.max_bytes is None:
                response = await self._http.request(op.method, url, params=op.params, json=op.json, headers=headers)
                return core.RawResponse(response.status_code, dict(response.headers), response.content)
            async with self._http.stream(op.method, url, params=op.params, headers=headers) as streamed:
                return await self._read_stream(streamed, op.max_bytes, request_id)
        except httpx.TimeoutException as error:
            raise APITimeoutError("AIWorkPay did not answer in time", request_id=request_id) from error
        except httpx.TransportError as error:
            raise APIConnectionError("AIWorkPay could not be reached", request_id=request_id) from error

    @staticmethod
    async def _read_stream(response: httpx.Response, max_bytes: int, request_id: str) -> core.RawResponse:
        limit = max_bytes if response.is_success else core.ERROR_BODY_LIMIT
        declared = response.headers.get("content-length")
        if response.is_success and declared and declared.isdigit() and int(declared) > limit:
            raise ProofTooLargeError(f"The file exceeds {max_bytes} bytes", request_id=request_id)
        chunks: List[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > limit:
                if response.is_success:
                    raise ProofTooLargeError(f"The file exceeds {max_bytes} bytes", request_id=request_id)
                break
            chunks.append(chunk)
        return core.RawResponse(response.status_code, dict(response.headers), b"".join(chunks))

    async def _execute(self, op: core.Operation[T]) -> T:
        request_id = uuid.uuid4().hex
        attempt = 0
        while True:
            try:
                response = await self._send(op, request_id)
            except APIConnectionError:
                delay = self._retry.delay(op, attempt, self._jitter, None)
                if delay is None:
                    raise
                await self._sleep(delay)
                attempt += 1
                continue

            if 200 <= response.status_code < 300:
                try:
                    return op.parse(response)
                except AIWorkPayError:
                    raise
                except (KeyError, TypeError, ValueError, AttributeError) as error:
                    raise AIWorkPayError(
                        "AIWorkPay returned an unexpected response",
                        status_code=response.status_code,
                        code="INVALID_RESPONSE",
                        request_id=request_id,
                    ) from error

            delay = self._retry.delay(op, attempt, self._jitter, response)
            if delay is None:
                raise core.read_error(response, request_id)
            await self._sleep(delay)
            attempt += 1

    # ── Missions ─────────────────────────────────────────────────────────────

    async def create_mission(
        self,
        title: str,
        description: str,
        budget: Union[int, float, str, Decimal],
        deadline: Union[str, date, datetime],
        *,
        currency: str = "EUR",
        priority: str = "medium",
        payment_mode: str = "checkout",
        payment_method_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> CreatedMission:
        """Order a mission.

        With the default ``payment_mode="checkout"`` nothing is charged: the mission stays
        ``PAYMENT_PENDING`` until a human opens ``result.payment.url`` and pays, then it is published.
        ``payment_mode="payment_intent"`` with a ``payment_method_id`` charges the card immediately.

        Idempotency: the call carries an Idempotency-Key (yours, or one generated for this call) that
        stays the same on every automatic retry, so a retry never creates a second mission. To make a
        retry of **your own** safe too (after a crash, say), pass the same ``idempotency_key``; a
        different request with the same key raises ``IdempotencyKeyReusedError``.

        Raises:
            ValueError: an argument is invalid (nothing was sent).
            BudgetLimitError: a cap of your key would be exceeded.
            PaymentProviderUnavailableError: Stripe is down; nothing was created, retry with the same key.
        """
        return await self._execute(
            core.create_mission(
                title=title,
                description=description,
                budget=budget,
                deadline=deadline,
                currency=currency,
                priority=priority,
                payment_mode=payment_mode,
                payment_method_id=payment_method_id,
                idempotency_key=idempotency_key,
            )
        )

    async def get_mission(self, mission_id: str) -> Mission:
        """Read one of your missions (``NotFoundError`` for any other)."""
        return await self._execute(core.get_mission(mission_id))

    async def list_missions(
        self, *, status: Optional[Union[str, MissionStatus]] = None, limit: int = 50, skip: int = 0
    ) -> List[Mission]:
        """Your missions, most recent first."""
        return await self._execute(core.list_missions(status, limit, skip))

    async def iter_missions(
        self, *, status: Optional[Union[str, MissionStatus]] = None, page_size: int = 50
    ) -> AsyncIterator[Mission]:
        """Every mission of your key, page after page: ``async for mission in client.iter_missions()``."""
        skip = 0
        while skip <= 10_000:
            page = await self.list_missions(status=status, limit=page_size, skip=skip)
            for mission in page:
                yield mission
            if len(page) < page_size:
                return
            skip += page_size

    async def wait_for_delivery(
        self, mission_id: str, *, interval: float = 5.0, timeout: Optional[float] = None
    ) -> Mission:
        """Poll until the Payworker's result can be read, then return the mission.

        The mission is only published once a human has paid ``payment.url``, then a Payworker must
        take it: this can take minutes to days, so set a ``timeout`` and a generous ``interval``
        (AIWorkPay limits a key to 120 requests a minute). For a server, prefer webhooks.

        Returns:
            The mission, ``DELIVERED`` (approve it or request changes) or already ``COMPLETED``.

        Raises:
            WaitTimeoutError: ``timeout`` seconds passed.
            MissionCanceledError: the mission was canceled meanwhile.
        """
        if interval <= 0:
            raise ValueError("interval must be positive")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            mission = await self.get_mission(mission_id)
            if mission.status == MissionStatus.CANCELED:
                raise MissionCanceledError(f"Mission {mission_id} was canceled")
            if mission.result_available:
                return mission
            wait = interval
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WaitTimeoutError(f"Mission {mission_id} was not delivered within {timeout}s")
                wait = min(interval, remaining)
            await self._sleep(wait)

    async def wait_for_deliveries(
        self, mission_ids: List[str], *, interval: float = 5.0, timeout: Optional[float] = None
    ) -> List[Mission]:
        """``wait_for_delivery`` for every mission at once."""
        return list(
            await asyncio.gather(
                *(self.wait_for_delivery(mid, interval=interval, timeout=timeout) for mid in mission_ids)
            )
        )

    async def approve_mission(self, mission_id: str) -> Approval:
        """Accept the delivered result: pays the Payworker (90 %). **Irreversible.**

        Needs the ``missions:approve`` permission. Approving twice is harmless (``already_approved``).
        """
        return await self._execute(core.approve_mission(mission_id))

    async def request_changes(self, mission_id: str, feedback: str) -> ChangesRequest:
        """Send a delivered result back to the same Payworker (3 times at most). No money moves."""
        return await self._execute(core.request_changes(mission_id, feedback))

    async def cancel_mission(self, mission_id: str) -> Cancellation:
        """Withdraw a mission no Payworker has taken yet: the payment page is closed, or the payment refunded."""
        return await self._execute(core.cancel_mission(mission_id))

    async def download_proof(self, mission_id: str, proof_id: str, *, max_bytes: int = core.MAX_PROOF_BYTES) -> Proof:
        """Download an uploaded proof (``attachment.is_uploaded``) with your key.

        The content comes from an external human and is not trusted: check it, never execute it.
        """
        return await self._execute(core.download_proof(mission_id, proof_id, max_bytes))

    # ── Account ──────────────────────────────────────────────────────────────

    async def get_account(self) -> Account:
        """Your key, its limits and what it has spent."""
        return await self._execute(core.get_account())

    async def list_transactions(
        self, *, mission_id: Optional[str] = None, limit: int = 50, skip: int = 0
    ) -> TransactionPage:
        """The payments booked on your key, most recent first."""
        return await self._execute(core.list_transactions(mission_id, limit, skip))

    # ── Webhook of your key ──────────────────────────────────────────────────

    async def get_webhook(self) -> WebhookConfig:
        """Your endpoint, the event types, and the last 20 deliveries."""
        return await self._execute(core.get_webhook())

    async def set_webhook(self, url: str, *, rotate_secret: bool = False) -> WebhookConfig:
        """Set your endpoint. The returned ``secret`` is shown once (first call, or ``rotate_secret=True``)."""
        return await self._execute(core.set_webhook(url, rotate_secret))

    async def delete_webhook(self) -> None:
        await self._execute(core.delete_webhook())

    async def send_webhook_test(self) -> str:
        """Queue a ``webhook.ping`` event; returns its id. Read its outcome with ``get_webhook()``."""
        return await self._execute(core.send_webhook_test())

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "AsyncAIWorkPay":
        return self

    async def __aexit__(
        self, exc_type: Optional[Type[BaseException]], exc: Optional[BaseException], tb: Optional[TracebackType]
    ) -> None:
        await self.close()
