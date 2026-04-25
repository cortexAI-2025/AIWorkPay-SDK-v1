import time
from typing import Any, Optional

import httpx

from .errors import TaskTimeoutError
from .types import ApproveResponse, CreateTaskInput, RejectResponse, Task
from .utils import handle_response, with_retries


class AIWorkPay:
    """Synchronous client for the AIWorkPay API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.aiworkpay.com",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    def create_task(self, task: CreateTaskInput) -> Task:
        """Submit a new human task and return its initial state."""
        return with_retries(
            lambda: handle_response(self._client.post(self._url("/tasks"), json=task))
        )

    def get_task(self, task_id: str) -> Task:
        """Fetch the current state of a task by ID."""
        return with_retries(
            lambda: handle_response(self._client.get(self._url(f"/tasks/{task_id}")))
        )

    def wait_for_result(
        self,
        task_id: str,
        interval: float = 2.0,
        timeout: Optional[float] = None,
    ) -> Task:
        """
        Poll until the task is completed or failed.

        Args:
            task_id: ID of the task to poll.
            interval: Seconds between each poll.
            timeout: Maximum total seconds to wait. None means wait forever.

        Raises:
            TaskTimeoutError: If timeout is reached before completion.
        """
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            task = self.get_task(task_id)
            if task["status"] in ("completed", "failed"):
                return task
            if deadline is not None and time.monotonic() >= deadline:
                raise TaskTimeoutError(
                    f"Task {task_id} did not complete within {timeout}s.",
                    status_code=None,
                )
            time.sleep(interval)

    def approve_and_pay(self, task_id: str) -> ApproveResponse:
        """Approve and trigger payment for a completed task."""
        return with_retries(
            lambda: handle_response(
                self._client.post(self._url(f"/tasks/{task_id}/approve"))
            )
        )

    def reject_task(
        self, task_id: str, reason: Optional[str] = None
    ) -> RejectResponse:
        """Reject a completed task, optionally providing a reason."""
        return with_retries(
            lambda: handle_response(
                self._client.post(
                    self._url(f"/tasks/{task_id}/reject"),
                    json={"reason": reason},
                )
            )
        )

    # ------------------------------------------------------------------
    # Batch helpers
    # ------------------------------------------------------------------

    def create_tasks(self, tasks: list[CreateTaskInput]) -> list[Task]:
        """Submit multiple tasks and return their initial states."""
        return [self.create_task(t) for t in tasks]

    def wait_for_results(
        self,
        task_ids: list[str],
        interval: float = 2.0,
        timeout: Optional[float] = None,
    ) -> list[Task]:
        """Poll all task IDs sequentially until all are done."""
        return [self.wait_for_result(tid, interval=interval, timeout=timeout) for tid in task_ids]

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AIWorkPay":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
