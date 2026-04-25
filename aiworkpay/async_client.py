import asyncio
from typing import Any, Optional

import httpx

from .errors import TaskTimeoutError
from .types import ApproveResponse, CreateTaskInput, RejectResponse, Task
from .utils import async_with_retries, handle_response


class AsyncAIWorkPay:
    """Asynchronous client for the AIWorkPay API — optimised for AI agents."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.aiworkpay.com",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def _post(self, url: str, **kwargs: Any) -> Any:
        res = await self._client.post(url, **kwargs)
        return handle_response(res)

    async def _get(self, url: str) -> Any:
        res = await self._client.get(url)
        return handle_response(res)

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    async def create_task(self, task: CreateTaskInput) -> Task:
        """Submit a new human task and return its initial state."""
        return await async_with_retries(lambda: self._post(self._url("/tasks"), json=task))

    async def get_task(self, task_id: str) -> Task:
        """Fetch the current state of a task by ID."""
        return await async_with_retries(lambda: self._get(self._url(f"/tasks/{task_id}")))

    async def wait_for_result(
        self,
        task_id: str,
        interval: float = 2.0,
        timeout: Optional[float] = None,
    ) -> Task:
        """
        Poll asynchronously until the task is completed or failed.

        Args:
            task_id: ID of the task to poll.
            interval: Seconds between each poll.
            timeout: Maximum total seconds to wait. None means wait forever.

        Raises:
            TaskTimeoutError: If timeout is reached before completion.
        """
        loop = asyncio.get_event_loop()
        start = loop.time()
        while True:
            task = await self.get_task(task_id)
            if task["status"] in ("completed", "failed"):
                return task
            if timeout is not None and loop.time() - start >= timeout:
                raise TaskTimeoutError(
                    f"Task {task_id} did not complete within {timeout}s.",
                    status_code=None,
                )
            await asyncio.sleep(interval)

    async def approve_and_pay(self, task_id: str) -> ApproveResponse:
        """Approve and trigger payment for a completed task."""
        return await async_with_retries(
            lambda: self._post(self._url(f"/tasks/{task_id}/approve"))
        )

    async def reject_task(
        self, task_id: str, reason: Optional[str] = None
    ) -> RejectResponse:
        """Reject a completed task, optionally providing a reason."""
        return await async_with_retries(
            lambda: self._post(self._url(f"/tasks/{task_id}/reject"), json={"reason": reason})
        )

    # ------------------------------------------------------------------
    # Batch helpers — all tasks run concurrently via asyncio.gather
    # ------------------------------------------------------------------

    async def create_tasks(self, tasks: list[CreateTaskInput]) -> list[Task]:
        """Submit multiple tasks concurrently and return their initial states."""
        return list(await asyncio.gather(*[self.create_task(t) for t in tasks]))

    async def wait_for_results(
        self,
        task_ids: list[str],
        interval: float = 2.0,
        timeout: Optional[float] = None,
    ) -> list[Task]:
        """Wait for all task IDs concurrently until all are done."""
        return list(
            await asyncio.gather(
                *[
                    self.wait_for_result(tid, interval=interval, timeout=timeout)
                    for tid in task_ids
                ]
            )
        )

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncAIWorkPay":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()
