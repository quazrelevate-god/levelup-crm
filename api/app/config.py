"""Application configuration.

Everything is read from the environment through pydantic-settings. There are no
hardcoded URLs or secrets, and no business configuration here: a workspace's
country code, timezone, currency and taxonomy are database rows, not env vars.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]


class Settings(BaseSettings):
    """Runtime settings, sourced from environment variables or a local `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Runtime -----------------------------------------------------------
    environment: Environment = "local"
    debug: bool = False
    log_level: str = "INFO"

    project_name: str = "Configurable CRM API"
    version: str = "0.1.0"
    api_v1_prefix: str = "/api/v1"

    cors_origins: list[str] = Field(default_factory=list)

    # --- Postgres ----------------------------------------------------------
    # Async DSN, e.g. postgresql+asyncpg://user:pass@host:5432/db
    database_url: str
    database_echo: bool = False
    database_pool_size: int = 10
    database_max_overflow: int = 10
    database_pool_timeout_seconds: int = 10

    # --- Redis -------------------------------------------------------------
    redis_url: str

    # --- Object storage (S3-compatible; MinIO locally) ---------------------
    s3_endpoint_url: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_bucket: str
    s3_region: str = "us-east-1"

    # --- Auth --------------------------------------------------------------
    # No default: a deployment without an explicitly configured signing key
    # must fail to boot rather than sign tokens with something guessable.
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 30

    # Argon2id parameters. Defaults follow the OWASP "second recommended"
    # configuration (46 MiB, t=1, p=1); raise memory_cost on bigger hosts.
    argon2_time_cost: int = 1
    argon2_memory_cost_kib: int = 47104
    argon2_parallelism: int = 1

    # Login rate limiting, enforced in Redis on two keys: the submitted email
    # and the client IP. Either tripping refuses the attempt.
    login_rate_limit_attempts: int = 10
    login_rate_limit_window_seconds: int = 300

    # Refresh rate limiting (02-api-contract.md: "/auth/refresh — Rotates.
    # Rate-limited."). Two budgets, because the two keys guard different things
    # and a single number cannot serve both:
    #
    # - Per token: a refresh token is legitimately presented exactly once, so
    #   any repeat is a client stuck in a loop or a stolen token being replayed.
    #   A tight budget costs a well-behaved client nothing.
    # - Per IP: a DoS backstop only. It must stay generous because an office
    #   behind one NAT address shares it — at a 30-minute access-token life,
    #   120 per 5 minutes still covers several hundred concurrent sessions.
    refresh_rate_limit_attempts: int = 10
    refresh_rate_limit_ip_attempts: int = 120
    refresh_rate_limit_window_seconds: int = 300

    # --- Health ------------------------------------------------------------
    health_check_timeout_seconds: float = 5.0

    #: Where the set-password link points. Not derived from the request host:
    #: a link built from an attacker-supplied Host header is a password-reset
    #: link that arrives in the right inbox and points at the wrong site.
    app_base_url: str = "http://localhost:5173"

    # --- Observability (M11) -----------------------------------------------
    #: Unset means Sentry is off. Neither reporting nor metrics may become a
    #: reason the API will not start — an observability dependency that can take
    #: the product down has inverted its own purpose.
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.0
    #: `/metrics` is unauthenticated by design, so it must not be reachable from
    #: the internet. Off unless a deployment turns it on and puts it behind
    #: something.
    metrics_enabled: bool = False

    # --- Outbound email (M8) -----------------------------------------------
    # Scheduled reports and recurring-date greetings both need a transport.
    # With no host configured the sender is a no-op that records what it would
    # have sent: local development must not be able to mail a real customer,
    # and a test suite that silently needed an SMTP server would be worse.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    smtp_from_address: str = "no-reply@example.invalid"
    smtp_timeout_seconds: float = 15.0

    # --- Bolna voice integration (Phase 2) ---------------------------------
    # A deployment credential, so it lives here rather than in a workspace row:
    # `api_keys` is for machine callers authenticating *into* the CRM, which is
    # the opposite direction. Unset means the voice endpoints answer 422
    # `voice_not_configured` rather than failing to boot — a deployment that
    # does not use voice must not be required to hold a Bolna key.
    bolna_api_key: str | None = None
    bolna_base_url: str = "https://api.bolna.ai"
    #: The agent a call is placed through. Overridable per call by the trigger.
    bolna_agent_id: str | None = None
    bolna_request_timeout_seconds: float = 15.0
    #: Which Bolna disposition carries the call summary, if the agent uses one.
    #: Deliberately unset: a disposition name is the *customer's* vocabulary
    #: (CLAUDE.md, "Known traps"), so the product cannot ship a guess at it.
    #: Unset means the receiver reads the payload's own `summary` key.
    bolna_summary_disposition: str | None = None
    #: Bolna's agent config accepts a bare `webhook_url` and nothing else — no
    #: custom headers, no signing secret. So the completion webhook authenticates
    #: with a CRM API key carried in the URL path. These two knobs harden that as
    #: far as the vendor allows.
    #:
    #: Bolna delivers webhooks from one documented source IP. Set this and any
    #: other source is refused before the key is even looked at. Empty means the
    #: check is off, which is correct behind a proxy that rewrites the peer
    #: address and wrong on a directly exposed host.
    bolna_webhook_allowed_ips: list[str] = Field(default_factory=list)
    #: Match an incoming call to a lead by its phone number when the payload
    #: carries no `crm_lead_id` and the CRM did not place the call. Required for
    #: inbound calls and for any call started from Bolna's own dashboard.
    bolna_match_by_phone: bool = True
    #: Create a lead when a call's number matches nothing. Goes through the same
    #: find-or-create the intake API uses, so it cannot produce a duplicate.
    bolna_create_missing_leads: bool = True

    # --- Tally form intake -------------------------------------------------
    #: Tally's optional webhook signing secret. Unset means the signature is
    #: not checked, which is the correct default: the API key is the actual
    #: credential, and requiring a secret nobody has configured would refuse
    #: every real submission. Set it and `POST /intake/tally` additionally
    #: verifies `tally-signature` over the raw request body.
    tally_signing_secret: str | None = None

    # --- Scheduler (M8) ----------------------------------------------------
    #: How often the cron tick runs. A schedule is due if its cron matched at
    #: any point since the last tick, so this is a resolution knob rather than
    #: a correctness one.
    scheduler_tick_seconds: int = 60
    #: A schedule that has not run in this long is caught up once, not once per
    #: missed occurrence — nobody wants 300 backdated reports after an outage.
    scheduler_max_catchup_hours: int = 24

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        """The engine is async; a sync DSN would fail confusingly at first query."""
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg:// driver")
        return value

    @field_validator("jwt_secret_key")
    @classmethod
    def _require_strong_secret(cls, value: str) -> str:
        """A short signing key is a forgeable signing key."""
        if len(value) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 characters")
        return value

    @property
    def is_local(self) -> bool:
        return self.environment in ("local", "test")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Call `get_settings.cache_clear()` in tests."""
    # Required values are supplied by the environment, not by call sites.
    return Settings()
