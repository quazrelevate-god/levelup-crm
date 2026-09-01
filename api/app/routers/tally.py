"""The Tally form webhook (`POST /api/v1/intake/tally`).

A thin adapter in front of the existing intake path. It parses Tally's
envelope, translates it (`app/services/tally.py`), and hands the result to
`IntakeService.ingest_lead` — the same method `/intake/leads` calls. Every
guarantee that path already makes is inherited rather than reimplemented:
identity dedupe, assignment rules, the changeset, the intake log, unknown
keys accepted-and-warned, and `dedupe: update` never blanking a value with an
empty one.

**Two routes, one implementation.** Tally's webhook UI cannot always send a
custom header, so the key may have to travel in the URL. That is the same
problem `voice.py` solved for Bolna and it is solved the same way here: both
routes funnel into `_ingest`, so a second entry point cannot quietly become a
second, weaker security boundary. The URL form carries the same caveat — a
path secret can be captured by proxy logs or a Referer — so use a key created
only for this, and revoke it in one click if it leaks.

**Always 2xx for a delivery that was understood.** Tally retries on non-2xx,
and retrying a submission the CRM has already stored helps nobody. A payload
that mapped nothing still answers 200 and says so in the body; only a bad key
or a bad signature is an error status.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_keys import ApiKeyScope, require_api_key, resolve_api_key
from app.dependencies import get_session
from app.errors import api_error
from app.events.intake import IntakeService
from app.schemas.tally import TallyIntakeResponse, TallyWebhook
from app.services.tally import TallyTranslator

router = APIRouter(prefix="/intake", tags=["intake"])

#: The only event this endpoint acts on. Tally sends others; they are
#: acknowledged and ignored rather than retried forever.
FORM_RESPONSE = "FORM_RESPONSE"

#: Requests a minute per key, matching `/intake/leads`. A form submission is
#: low-volume by nature; this is a runaway-integration backstop, not a quota.
RATE_LIMIT_PER_MINUTE = 100


def _verify_signature(request: Request, body: bytes, secret: str | None) -> None:
    """Check Tally's `tally-signature` header, when a secret is configured.

    Unset secret means unchecked — the same posture `BOLNA_WEBHOOK_ALLOWED_IPS`
    takes. The API key is the actual credential either way; this is defence in
    depth, and making it mandatory would break every deployment that has not
    set one.

    Tally signs the raw request body with HMAC-SHA256 and base64-encodes the
    digest, so the comparison is done over the exact bytes received rather than
    a re-serialisation of the parsed model — a signature over a round-tripped
    body is one that fails intermittently for reasons nobody enjoys finding.

    Kept in one function precisely because the encoding is the part most likely
    to need correcting against a real delivery: hex versus base64, and whether
    a `sha256=` prefix is present, are the two things vendors differ on.
    """
    if not secret:
        return

    presented = request.headers.get("tally-signature") or request.headers.get("Tally-Signature")
    if not presented:
        raise api_error(401, "invalid_signature", "Missing tally-signature header")

    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    import base64

    expected_b64 = base64.b64encode(digest).decode("ascii")
    expected_hex = digest.hex()
    candidate = presented.strip()
    if candidate.startswith("sha256="):
        candidate = candidate[len("sha256=") :]

    if not (
        hmac.compare_digest(candidate, expected_b64) or hmac.compare_digest(candidate, expected_hex)
    ):
        raise api_error(401, "invalid_signature", "That signature is not valid")


async def _rate_limit(request: Request, scope: ApiKeyScope) -> None:
    """Per-key, Redis-backed, and a no-op when Redis is unavailable.

    Same trade as the intake router makes: losing a limit for a minute is
    cheaper than losing leads because the limiter was down.
    """
    redis = getattr(request.app.state, "redis", None)
    if redis is None:  # pragma: no cover - wired in create_app
        return
    key = f"intake:tally:{scope.api_key.id}"
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, 60)
    except Exception:
        return
    if count > RATE_LIMIT_PER_MINUTE:
        raise api_error(
            429,
            "rate_limited",
            f"At most {RATE_LIMIT_PER_MINUTE} Tally submissions a minute per key",
        )


async def _ingest(request: Request, scope: ApiKeyScope, body: bytes) -> TallyIntakeResponse:
    """One implementation, whichever way the key arrived."""
    settings = request.app.state.settings
    _verify_signature(request, body, getattr(settings, "tally_signing_secret", None))
    await _rate_limit(request, scope)

    try:
        payload = TallyWebhook.model_validate_json(body)
    except ValidationError:
        # Malformed JSON or a shape Tally has never sent. 422 rather than 500,
        # and no lead is invented from a body we could not read.
        raise api_error(422, "invalid_payload", "That is not a Tally webhook body") from None

    if (payload.eventType or "").upper() != FORM_RESPONSE:
        # Acknowledged, deliberately unprocessed. A non-2xx here would have
        # Tally retrying an event type this endpoint will never act on.
        return TallyIntakeResponse(outcome="IGNORED", warnings=[], mapped=0)

    translator = TallyTranslator(scope.session)
    translated = await translator.translate(payload)

    if not translated.values:
        return TallyIntakeResponse(
            outcome="SKIPPED",
            warnings=[
                *translated.warnings,
                "The submission carried no answers this workspace could store",
            ],
            mapped=0,
        )

    service = IntakeService(scope)
    intake_body = {"values": translated.values, "dedupe": "update"}
    result = await service.ingest_lead(intake_body)
    # Logged exactly like every other intake request, so a Tally submission is
    # answerable from Settings → Integrations → intake log alongside the rest.
    await service.log(endpoint="tally", body=intake_body, result=result)
    await scope.session.commit()

    return TallyIntakeResponse(
        outcome=result.outcome.value,
        lead_id=str(result.lead_id) if result.lead_id else None,
        warnings=translated.warnings + result.warnings,
        mapped=translated.mapped,
    )


@router.post(
    "/tally",
    response_model=TallyIntakeResponse,
    summary="Tally form-response webhook (X-API-Key header)",
)
async def tally_webhook(
    request: Request,
    scope: Annotated[ApiKeyScope, Depends(require_api_key)],
) -> TallyIntakeResponse:
    """The preferred route: the key travels in a header, not the URL."""
    return await _ingest(request, scope, await request.body())


@router.post(
    "/tally/{webhook_key}",
    response_model=TallyIntakeResponse,
    summary="Tally form-response webhook (key in the URL)",
)
async def tally_webhook_by_key(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    webhook_key: Annotated[str, Path(min_length=8, max_length=200)],
) -> TallyIntakeResponse:
    """The fallback, for a Tally plan that cannot add a custom header.

    Weaker than the header route — a URL secret can be captured by proxy access
    logs, browser history or a Referer — and mitigated the same way the Bolna
    URL route is: it is an ordinary CRM API key, stored Argon2-hashed, carrying
    a narrow permission template, revocable in one click. Use a key created
    only for this, and always over HTTPS.
    """
    scope = await resolve_api_key(request, session, webhook_key)
    return await _ingest(request, scope, await request.body())
