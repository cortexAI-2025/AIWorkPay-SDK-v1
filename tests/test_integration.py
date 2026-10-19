"""Against a running AIWorkPay. Skipped unless AIWORKPAY_TEST_BASE_URL and AIWORKPAY_TEST_API_KEY are set.

The key should belong to a test database. Nothing here creates a paid mission:
it reads the account and checks how errors come back.
"""

from __future__ import annotations

import os

import pytest

from aiworkpay import AIWorkPay, AuthenticationError, NotFoundError

BASE_URL = os.environ.get("AIWORKPAY_TEST_BASE_URL")
API_KEY = os.environ.get("AIWORKPAY_TEST_API_KEY")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (BASE_URL and API_KEY), reason="AIWORKPAY_TEST_BASE_URL / AIWORKPAY_TEST_API_KEY not set"),
]


def test_account_missions_and_errors() -> None:
    with AIWorkPay(API_KEY, base_url=BASE_URL) as sdk:
        account = sdk.get_account()
        assert account.key.active and account.key.scopes
        assert isinstance(sdk.list_missions(limit=5), list)
        assert sdk.list_transactions(limit=5).total >= 0
        with pytest.raises(NotFoundError) as missing:
            sdk.get_mission("does-not-exist")
        assert missing.value.code == "MISSION_NOT_FOUND"
        assert sdk.get_webhook().events

    with AIWorkPay("awp_" + "0" * 40, base_url=BASE_URL) as stranger:
        with pytest.raises(AuthenticationError) as invalid:
            stranger.list_missions()
        assert invalid.value.code == "INVALID_API_KEY"
