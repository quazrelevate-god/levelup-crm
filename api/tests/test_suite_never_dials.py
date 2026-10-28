"""The test suite can never place a real phone call.

On 2026-10-24 a developer's real Bolna key in `api/.env` reached the suite
(pydantic-settings reads that file), and one test that had not installed the
fake client dialled a fixture number for real. Two layers now stop that, and
this module checks both.
"""

from __future__ import annotations

import os

import pytest

from app.config import get_settings
from app.integrations.bolna import BolnaCallRequest, BolnaSettings, HttpxBolnaClient


def test_no_real_credential_reaches_the_suite() -> None:
    """Layer one: credentials are forced empty, whatever `api/.env` holds."""
    for name in ("BOLNA_API_KEY", "BOLNA_AGENT_ID", "SMTP_HOST", "SMTP_PASSWORD"):
        assert os.environ.get(name) == "", name
    settings = get_settings()
    assert not settings.bolna_api_key
    assert not settings.smtp_host


async def test_the_real_client_refuses_to_dial_a_real_host() -> None:
    """Layer two: even with a key in hand, the real client will not dial."""
    client = HttpxBolnaClient(
        BolnaSettings(api_key="bn-not-a-real-key", base_url="https://api.bolna.ai", agent_id="a")
    )
    with pytest.raises(AssertionError, match="real Bolna call"):
        await client.place_call(
            BolnaCallRequest(agent_id="a", recipient_phone_number="+919000000000", user_data={})
        )
