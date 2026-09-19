"""Structured logging, error reporting and metrics (M11).

Three things the product had none of, and one idea holding them together: when
something goes wrong in a multi-tenant system, "what broke" is half an answer.
The other half is **whose** — and until now a stack trace could not say.

**Every log line carries a request id and, once resolved, a workspace id.**
Bound to a context variable rather than passed down, because the interesting
lines are emitted deep inside services that have no business taking a logging
parameter. A caller correlates a support report to a request; the workspace id
turns "the CRM is slow" into "this tenant's queries are slow".

**Sentry and Prometheus are both optional.** No DSN, no Sentry; nobody scraping,
nothing scraped. Neither may become a reason the API will not start — an
observability dependency that can take the product down has inverted its own
purpose.

**The five metrics are the ones named in the milestone**, and four of them
measure the asynchronous machinery M8 and M10 built. Request duration is the
obvious one; outbox depth, intake rate, index-build queue and scheduler lag are
the ones nobody can see by looking at the app, which is precisely why they are
the ones worth exporting.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings

__all__ = [
    "METRICS",
    "ObservabilityMiddleware",
    "bind_workspace",
    "configure_logging",
    "configure_sentry",
    "current_request_id",
]

#: Bound per request, read by the log processor. A context variable rather than
#: a parameter because the lines worth correlating are emitted deep in services
#: that should not have to accept a logger argument to be traceable.
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_workspace_id: ContextVar[str | None] = ContextVar("workspace_id", default=None)


def current_request_id() -> str | None:
    return _request_id.get()


def bind_workspace(workspace_id: uuid.UUID | str | None) -> None:
    """Attach a workspace to every subsequent log line in this request.

    Called by the scoping dependency, which is the first place the workspace is
    known — before it, a request genuinely has no tenant.
    """
    _workspace_id.set(str(workspace_id) if workspace_id else None)


def _add_context(
    _logger: Any, _name: str, event: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    if (request_id := _request_id.get()) is not None:
        event["request_id"] = request_id
    if (workspace_id := _workspace_id.get()) is not None:
        event["workspace_id"] = workspace_id
    return event


#: URL paths that carry a credential as a path segment. Bolna's agent config
#: accepts only a bare `webhook_url`, so its webhook authenticates with a CRM
#: API key *in the path* (`routers/voice.py::receive_bolna_webhook`). Anything
#: that logs a path must pass it through `redact_path` first.
_SECRET_PATH_RE = re.compile(r"(/voice/bolna/)[^/?#\s]+")
REDACTED = "<redacted>"


def redact_path(path: Any) -> Any:
    """`path` with any credential-bearing segment replaced. Non-str untouched."""
    if not isinstance(path, str):
        return path
    return _SECRET_PATH_RE.sub(lambda match: match.group(1) + REDACTED, path)


class _RedactAccessLog(logging.Filter):
    """Keep the webhook key out of uvicorn's access log.

    Uvicorn logs `"%s - "%s %s HTTP/%s" %d"` with the full path as the third
    argument — which, for the Bolna webhook, is the API key.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact_path(arg) for arg in record.args)
        return True


def configure_logging(settings: Settings) -> None:
    """JSON in deployment, human-readable locally.

    A developer reading a terminal and a log aggregator parsing a stream want
    opposite things, and picking one for both makes somebody's life worse.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=level, force=True)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _RedactAccessLog) for f in access.filters):
        access.addFilter(_RedactAccessLog())

    renderer: Any = (
        structlog.dev.ConsoleRenderer()
        if settings.is_local
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def configure_sentry(settings: Settings) -> bool:
    """Wire Sentry if a DSN is configured. Returns whether it was.

    Silent when unset: a developer without a DSN should not see a warning on
    every start, and CI should not need one to be green.
    """
    if not settings.sentry_dsn:
        return False

    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        release=settings.version,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        # Bodies can carry a customer's lead data, and an error report is not a
        # place for it. The request id is enough to find the rest.
        send_default_pii=False,
        before_send=_redact_sentry_event,
    )
    return True


def _redact_sentry_event(event: Any, hint: Any) -> Any:
    """Sentry records the request URL even without PII — redact the key in it."""
    request = event.get("request") if isinstance(event, dict) else None
    if isinstance(request, dict) and "url" in request:
        request["url"] = redact_path(request["url"])
    return event


class _Metrics:
    """The five the milestone names, on a registry this app owns.

    Its own registry rather than the process-global default: the default is
    module state, so a second `create_app()` in a test run would raise on
    duplicate collectors.
    """

    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        self.request_duration = Histogram(
            "crm_request_duration_seconds",
            "HTTP request duration.",
            labelnames=("method", "route", "status"),
            registry=self.registry,
        )
        #: How far behind delivery is. The number that says a consumer is down
        #: before anybody files a ticket about it.
        self.outbox_depth = Gauge(
            "crm_outbox_depth",
            "Outbound events not yet delivered, by status.",
            labelnames=("status",),
            registry=self.registry,
        )
        self.intake_requests = Counter(
            "crm_intake_requests_total",
            "Intake API requests, by outcome.",
            labelnames=("endpoint", "outcome"),
            registry=self.registry,
        )
        #: A queue that only grows means the indexing worker has stopped, and
        #: the symptom a customer reports is "sorting is broken".
        self.index_build_queue = Gauge(
            "crm_index_build_queue",
            "Indexed-field declarations not yet built.",
            registry=self.registry,
        )
        #: Seconds between a schedule being due and being sent. Rising lag is
        #: the early warning for reports arriving late.
        self.scheduler_lag = Gauge(
            "crm_scheduler_lag_seconds",
            "How late the most overdue active schedule is.",
            registry=self.registry,
        )


METRICS = _Metrics()


def _route_template(scope: Scope) -> str:
    """A low-cardinality label for the path.

    **Never the raw path.** Labelling by path mints a new time series per lead
    id, and a metrics store melted by uuid cardinality does not un-melt.

    Reconstructed from the matched path params rather than read off
    `scope["route"].path`, because how much of the template that attribute
    carries depends on the FastAPI version — on this one it is the *router
    relative* path, so `/api/v1/workspaces/{workspace_id}/members` arrives as
    `/members`, losing both the prefix and the distinction from any other
    router's `/members`. Substituting the params back is version-independent
    and yields the template in full.
    """
    path = str(scope.get("path", "unknown"))
    for name, value in (scope.get("path_params") or {}).items():
        path = path.replace(str(value), "{" + name + "}")
    return path


class ObservabilityMiddleware:
    """Assigns a request id, times the request, and logs the outcome.

    Pure ASGI rather than `BaseHTTPMiddleware`, which wraps the response in a
    task group and breaks context variables set downstream — the exact mechanism
    this relies on to attach a workspace id.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        import time

        headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
        # Honour an inbound id so a trace survives a proxy, but never trust its
        # shape — it ends up in logs, and a header is attacker-supplied.
        incoming = headers.get("x-request-id", "")
        request_id = incoming[:64] if incoming.isprintable() and incoming else uuid.uuid4().hex

        token = _request_id.set(request_id)
        workspace_token = _workspace_id.set(None)
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                message.setdefault("headers", [])
                message["headers"].append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            template = _route_template(scope)

            METRICS.request_duration.labels(
                method=scope.get("method", "?"),
                route=template,
                status=str(status_code),
            ).observe(elapsed)

            structlog.get_logger("api.request").info(
                "request",
                method=scope.get("method"),
                path=redact_path(scope.get("path")),
                route=template,
                status=status_code,
                duration_ms=round(elapsed * 1000, 2),
            )
            _request_id.reset(token)
            _workspace_id.reset(workspace_token)
