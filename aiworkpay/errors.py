from typing import Optional


class AIWorkPayError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class TaskTimeoutError(AIWorkPayError):
    """Raised when wait_for_result exceeds the specified timeout."""


class RateLimitError(AIWorkPayError):
    """Raised on HTTP 429 responses."""


class AuthenticationError(AIWorkPayError):
    """Raised on HTTP 401 responses."""


class NotFoundError(AIWorkPayError):
    """Raised on HTTP 404 responses."""
