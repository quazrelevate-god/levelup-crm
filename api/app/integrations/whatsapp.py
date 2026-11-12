"""Sending WhatsApp template messages through Meta's WhatsApp Cloud API (docs/22).

The official API every WhatsApp Business provider sits on. A business message
that starts a conversation must be an **approved template** (Meta rule), so a
send names a template, its language, and the values for its `{{1}}`, `{{2}}`
placeholders. The company connects its own WhatsApp number (its phone number
ID and an access token from Meta's WhatsApp Manager) in Apps → WhatsApp.

Providers such as Interakt or AiSensy are reached through a workflow's
"Call an API" block instead; this module is only Meta's own API.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import httpx

__all__ = ["API_VERSION", "SendResult", "send_template", "whatsapp_number"]

API_VERSION = "v21.0"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def whatsapp_number(raw: str, country_code: str) -> str:
    """Digits only, with the country code — what WhatsApp's API wants."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        digits = f"{country_code}{digits}"
    return digits if 8 <= len(digits) <= 15 else ""


class SendResult:
    def __init__(self, ok: bool, status: int, error: str | None, message_id: str | None) -> None:
        self.ok = ok
        self.status = status
        self.error = error
        self.message_id = message_id


def _error_text(response: httpx.Response) -> str:
    try:
        body: Any = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("error_user_msg") or error.get("message") or error)[:200]
    return str(body)[:200]


async def send_template(
    *,
    phone_number_id: str,
    token: str,
    to: str,
    template: str,
    language: str,
    params: Sequence[str] = (),
    transport: httpx.AsyncBaseTransport | None = None,
) -> SendResult:
    body: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {"name": template, "language": {"code": language}},
    }
    if params:
        body["template"]["components"] = [
            {"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}
        ]
    url = f"https://graph.facebook.com/{API_VERSION}/{phone_number_id}/messages"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=transport) as client:
            response = await client.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError:
        return SendResult(False, 0, "Could not reach WhatsApp", None)
    if response.status_code >= 300:
        return SendResult(False, response.status_code, _error_text(response), None)
    try:
        messages = response.json().get("messages") or [{}]
        message_id = str(messages[0].get("id") or "") or None
    except (ValueError, AttributeError, IndexError):
        message_id = None
    return SendResult(True, response.status_code, None, message_id)
