"""The Bolna HTTP client's error handling.

These are unit tests: no database, no network, no Bolna account. `httpx`'s
`AsyncClient` is swapped for a stub so a vendor response of any shape can be
put in front of the client and the resulting `BolnaCallResult` inspected.

The file exists because of a real incident. A production call failed with
`Bolna answered 400` and nothing else — the client discarded the response body
on the reasoning that an upstream echoing the request back could put the API
key in the database. That reasoning held, and it also left the one piece of
evidence that would have explained the failure unrecoverable. The body is now
kept, sanitised, and `test_the_api_key_can_never_reach_the_stored_error` is the
guard that keeps the original concern honoured.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.integrations.bolna import (
    VENDOR_DETAIL_LIMIT,
    BolnaCallRequest,
    BolnaSettings,
    HttpxBolnaClient,
    sanitise_vendor_error,
)

#: Recognisable, obviously fake, and shaped like the real thing so a leak in
#: any assertion below would be unmistakable.
FAKE_KEY = "bn-FAKEKEY00000000000000000000000"
FAKE_AGENT = "11111111-2222-3333-4444-555555555555"


def _settings() -> BolnaSettings:
    return BolnaSettings(
        api_key=FAKE_KEY, base_url="https://api.bolna.invalid", agent_id=FAKE_AGENT
    )


def _request() -> BolnaCallRequest:
    return BolnaCallRequest(
        agent_id=FAKE_AGENT,
        recipient_phone_number="+919999999999",
        user_data={"name": "Test", "crm_lead_id": "abc"},
    )


class _StubResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def json(self) -> Any:
        return json.loads(self.text)


def _install_stub(monkeypatch: pytest.MonkeyPatch, response: _StubResponse) -> list[dict]:
    """Swap `httpx.AsyncClient` for one that returns `response`.

    Returns the list the stub records requests into, so a test can assert on
    what would have been sent without anything leaving the process.
    """
    import httpx

    sent: list[dict] = []

    class _StubClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _StubClient:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def post(self, url: str, json: Any = None, headers: Any = None) -> _StubResponse:
            sent.append({"url": url, "json": json, "headers": headers})
            return response

    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    return sent


# --- the failure this file exists for ---------------------------------------


async def test_a_400_carries_the_vendor_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: a refusal must say *why*, not merely that it happened."""
    body = json.dumps({"message": "agent not found", "code": "invalid_agent"})
    _install_stub(monkeypatch, _StubResponse(400, body))

    result = await HttpxBolnaClient(_settings()).place_call(_request())

    assert result.ok is False
    assert result.status_code == 400
    assert result.execution_id is None
    assert result.error is not None
    # The status is still there — existing behaviour is preserved, not replaced.
    assert "Bolna answered 400" in result.error
    # And now the useful half.
    assert "agent not found" in result.error
    assert "invalid_agent" in result.error


async def test_the_api_key_can_never_reach_the_stored_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The original concern, now enforced rather than avoided.

    An upstream that echoes the credential back — in a named field, in an
    Authorization header, or bare in prose — must not put it into the error
    that gets written to `voice_call_executions.last_error`.
    """
    body = json.dumps(
        {
            "message": "unauthorised for this agent",
            "api_key": FAKE_KEY,
            "echoed_headers": {"Authorization": f"Bearer {FAKE_KEY}"},
            "debug": f"presented key {FAKE_KEY} was rejected",
        }
    )
    _install_stub(monkeypatch, _StubResponse(401, body))

    result = await HttpxBolnaClient(_settings()).place_call(_request())

    assert result.error is not None
    assert FAKE_KEY not in result.error
    assert "<redacted>" in result.error
    # The genuinely useful part survives the redaction.
    assert "unauthorised for this agent" in result.error


async def test_a_success_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing behaviour on the happy path must not have moved."""
    body = json.dumps({"execution_id": "exec-123", "status": "queued", "message": "done"})
    sent = _install_stub(monkeypatch, _StubResponse(200, body))

    result = await HttpxBolnaClient(_settings()).place_call(_request())

    assert result.ok is True
    assert result.execution_id == "exec-123"
    assert result.status == "queued"
    assert result.error is None
    assert result.status_code == 200

    # And the request itself is untouched by this change: same three fields,
    # no `from_phone_number` added.
    assert len(sent) == 1
    assert sent[0]["json"] == {
        "agent_id": FAKE_AGENT,
        "recipient_phone_number": "+919999999999",
        "user_data": {"name": "Test", "crm_lead_id": "abc"},
    }


async def test_a_non_json_error_body_is_still_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy's HTML error page is not JSON and must not raise."""
    _install_stub(monkeypatch, _StubResponse(502, "<html><body>Bad Gateway</body></html>"))

    result = await HttpxBolnaClient(_settings()).place_call(_request())

    assert result.ok is False
    assert result.error is not None
    assert "Bolna answered 502" in result.error
    assert "Bad Gateway" in result.error


async def test_an_empty_error_body_leaves_the_status_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No body means the message is exactly what it always was."""
    _install_stub(monkeypatch, _StubResponse(400, ""))

    result = await HttpxBolnaClient(_settings()).place_call(_request())

    assert result.error == "Bolna answered 400"


async def test_a_verbose_body_cannot_fill_the_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vendor that returns a megabyte must not be able to store one."""
    _install_stub(monkeypatch, _StubResponse(400, "x" * 20_000))

    result = await HttpxBolnaClient(_settings()).place_call(_request())

    assert result.error is not None
    # The detail is bounded; the short status prefix rides on top of it.
    assert len(result.error) <= VENDOR_DETAIL_LIMIT + 40


# --- the sanitiser on its own ------------------------------------------------


def test_sanitiser_redacts_a_credential_named_field() -> None:
    out = sanitise_vendor_error(f'{{"api_key": "{FAKE_KEY}", "message": "nope"}}')
    assert FAKE_KEY not in out
    assert "<redacted>" in out
    assert "nope" in out


def test_sanitiser_redacts_a_bearer_token_in_any_casing() -> None:
    for prefix in ("Bearer", "bearer", "BEARER"):
        out = sanitise_vendor_error(f"{prefix} {FAKE_KEY} was refused")
        assert FAKE_KEY not in out
        assert "was refused" in out


def test_sanitiser_redacts_the_configured_secret_anywhere() -> None:
    """Even bare in prose, with no field name and no Bearer prefix."""
    out = sanitise_vendor_error(f"the key {FAKE_KEY} is not valid", secret=FAKE_KEY)
    assert FAKE_KEY not in out
    assert "is not valid" in out


def test_sanitiser_keeps_an_ordinary_message_intact() -> None:
    """Over-redaction would defeat the purpose, so it must not happen."""
    out = sanitise_vendor_error('{"message": "country not provided"}', secret=FAKE_KEY)
    assert out == '{"message": "country not provided"}'


def test_sanitiser_collapses_whitespace_and_truncates() -> None:
    assert sanitise_vendor_error("line one\n\n   line two") == "line one line two"
    long = sanitise_vendor_error("y" * 900)
    assert len(long) == VENDOR_DETAIL_LIMIT
    assert long.endswith("…")


def test_sanitiser_handles_nothing_at_all() -> None:
    assert sanitise_vendor_error("") == ""
    assert sanitise_vendor_error(None) == ""
