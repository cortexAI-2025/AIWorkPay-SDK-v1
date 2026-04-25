import time
import asyncio
from typing import Any, Callable, TypeVar

import httpx

from .errors import AIWorkPayError, AuthenticationError, NotFoundError, RateLimitError

T = TypeVar("T")

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0


def handle_response(response: httpx.Response) -> Any:
    if response.status_code == 401:
        raise AuthenticationError("Invalid or missing API key.", status_code=401)
    if response.status_code == 404:
        raise NotFoundError(response.text, status_code=404)
    if response.status_code == 429:
        raise RateLimitError("Rate limit exceeded. Please slow down.", status_code=429)
    if response.status_code >= 400:
        raise AIWorkPayError(response.text, status_code=response.status_code)
    return response.json()


def with_retries(fn: Callable[[], T]) -> T:
    """Execute fn with exponential-backoff retries on transient errors."""
    last_exc: Exception = AIWorkPayError("Unreachable")
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn()
        except AIWorkPayError as exc:
            if exc.status_code not in _RETRYABLE_STATUS_CODES or attempt == _MAX_RETRIES:
                raise
            last_exc = exc
        except httpx.TransportError as exc:
            if attempt == _MAX_RETRIES:
                raise AIWorkPayError(str(exc)) from exc
            last_exc = exc  # type: ignore[assignment]
        delay = _RETRY_BASE_DELAY * (2**attempt)
        time.sleep(delay)
    raise last_exc  # type: ignore[misc]


async def async_with_retries(fn: Callable[[], Any]) -> Any:
    """Async version of with_retries."""
    last_exc: Exception = AIWorkPayError("Unreachable")
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return await fn()
        except AIWorkPayError as exc:
            if exc.status_code not in _RETRYABLE_STATUS_CODES or attempt == _MAX_RETRIES:
                raise
            last_exc = exc
        except httpx.TransportError as exc:
            if attempt == _MAX_RETRIES:
                raise AIWorkPayError(str(exc)) from exc
            last_exc = exc  # type: ignore[assignment]
        delay = _RETRY_BASE_DELAY * (2**attempt)
        await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
