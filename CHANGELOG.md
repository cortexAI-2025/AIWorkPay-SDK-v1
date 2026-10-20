# Changelog

## 2.0.0 (not yet published)

Rewrite for the AIWorkPay agent API: sync and async clients, typed models, retries only where replaying is safe,
an `Idempotency-Key` on every call, `wait_for_delivery`, proof download, account and transactions, webhook
configuration and `construct_event` signature verification. See the README.

Verified so far:
- 98 unit tests (Python 3.9 to 3.13 in CI), ruff, mypy.
- `tests/test_e2e.py`: the "opening hours and front photo" scenario, refusals (bad key, missing scope, budget cap,
  another agent's mission, invalid argument), cancellation and the async client, against the real AIWorkPay server with
  a Stripe simulator (Aiworkpay-v2 `npm run e2e:harness`). Not run against real Stripe test mode.
