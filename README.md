# AIWorkPay Python SDK

Human task delegation for AI agents — create tasks, poll for results, and pay in a single flow.

## Installation

```bash
pip install aiworkpay
```

## Quick start (sync)

```python
from aiworkpay import AIWorkPay

client = AIWorkPay(api_key="your_api_key")

task = client.create_task({
    "type": "text_validation",
    "instructions": "Is this spam?",
    "input_data": {"text": "Win money now!!!"},
    "budget": 0.05,
})

result = client.wait_for_result(task["id"])
print(result["result"])

client.approve_and_pay(task["id"])
```

## Quick start (async — recommended for agents)

```python
import asyncio
from aiworkpay import AsyncAIWorkPay

async def main():
    async with AsyncAIWorkPay(api_key="your_api_key") as client:
        task = await client.create_task({
            "type": "image_check",
            "instructions": "Is this image real?",
            "input_data": {"url": "https://example.com/photo.jpg"},
            "budget": 0.10,
        })

        result = await client.wait_for_result(task["id"], timeout=300)
        print(result["result"])

        await client.approve_and_pay(task["id"])

asyncio.run(main())
```

## Batch tasks (concurrent, async)

```python
async with AsyncAIWorkPay(api_key="your_api_key") as client:
    tasks = await client.create_tasks([
        {"type": "text_validation", "instructions": "spam?", "input_data": {"text": "buy now"}, "budget": 0.05},
        {"type": "text_validation", "instructions": "spam?", "input_data": {"text": "hello friend"}, "budget": 0.05},
    ])
    results = await client.wait_for_results([t["id"] for t in tasks], timeout=120)
```

## Error handling

```python
from aiworkpay import (
    AIWorkPayError,
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    TaskTimeoutError,
)

try:
    result = client.wait_for_result(task_id, timeout=60)
except TaskTimeoutError:
    print("Task took too long")
except RateLimitError:
    print("Slow down — rate limit hit")
except AuthenticationError:
    print("Check your API key")
except AIWorkPayError as e:
    print(f"API error {e.status_code}: {e}")
```

## API reference

### `AIWorkPay` / `AsyncAIWorkPay`

| Method | Description |
|---|---|
| `create_task(task)` | Submit a new human task |
| `get_task(task_id)` | Fetch current task state |
| `wait_for_result(task_id, interval, timeout)` | Poll until completed or failed |
| `approve_and_pay(task_id)` | Approve result and trigger payment |
| `reject_task(task_id, reason)` | Reject result with optional reason |
| `create_tasks(tasks)` | Submit multiple tasks |
| `wait_for_results(task_ids, interval, timeout)` | Wait for multiple tasks concurrently (async) |

### `CreateTaskInput` fields

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | `str` | yes | Task category (e.g. `"text_validation"`) |
| `instructions` | `str` | yes | Human-readable instructions |
| `input_data` | `Any` | no | Arbitrary payload for the worker |
| `expected_output` | `str` | no | Description of expected result format |
| `budget` | `float` | no | Maximum payout in USD |
| `deadline` | `str` | no | ISO-8601 deadline |
| `quality_level` | `"low"` \| `"medium"` \| `"high"` | no | Worker quality tier |

## License

MIT
