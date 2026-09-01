"""FastAPI application factory and lifespan.

M0 exposes one endpoint. The shape here — settings injected, resources built
once in the lifespan, routers mounted under the versioned prefix — is what every
later milestone hangs off.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.auth.passwords import PasswordHasherService
from app.cache import create_redis
from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.observability import (
    METRICS,
    ObservabilityMiddleware,
    configure_logging,
    configure_sentry,
)
from app.routers import auth as auth_router
from app.routers import fields as fields_router
from app.routers import health as health_router
from app.routers import intake as intake_router
from app.routers import integrations as integrations_router
from app.routers import leads as leads_router
from app.routers import members as members_router
from app.routers import permission_templates as permission_templates_router
from app.routers import pipeline as pipeline_router
from app.routers import reports as reports_router
from app.routers import routing as routing_router
from app.routers import tally as tally_router
from app.routers import views as views_router
from app.routers import work as work_router
from app.routers import workspaces as workspaces_router
from app.services.health import HealthService
from app.services.lead_ownership import DatabaseLeadOwnership, set_lead_ownership
from app.storage import create_s3_client

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the shared clients once, tear them down on shutdown."""
    settings: Settings = app.state.settings

    engine = create_engine(settings)
    redis = create_redis(settings)
    s3 = create_s3_client(settings)

    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.redis = redis
    app.state.s3 = s3
    app.state.health_service = HealthService(
        settings=settings,
        engine=engine,
        redis=redis,
        s3=s3,
    )
    # Built once: the constructor computes a throwaway hash with the live
    # argon2 parameters, which is deliberately expensive.
    app.state.password_hasher = PasswordHasherService(settings)

    logger.info("api.startup", extra={"environment": settings.environment})
    try:
        yield
    finally:
        await redis.aclose()
        await engine.dispose()
        s3.close()
        logger.info("api.shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    configure_logging(resolved)
    sentry_on = configure_sentry(resolved)

    app = FastAPI(
        title=resolved.project_name,
        version=resolved.version,
        docs_url="/docs" if resolved.is_local else None,
        redoc_url=None,
        openapi_url="/openapi.json" if resolved.is_local else None,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.sentry_enabled = sentry_on

    # Outermost, so a request id exists before anything else can log — including
    # CORS rejections and validation errors, which are exactly the ones somebody
    # will later ask about by id.
    app.add_middleware(ObservabilityMiddleware)

    if resolved.metrics_enabled:

        @app.get("/metrics", include_in_schema=False)
        async def metrics() -> Response:
            """Prometheus exposition. Unauthenticated, hence opt-in.

            Metrics leak shape — request rates, tenant counts, queue depths —
            so this is off unless a deployment turns it on and puts it behind
            something that is not the public internet.
            """
            return Response(generate_latest(METRICS.registry), media_type=CONTENT_TYPE_LATEST)

    # `leads` exists from M5, so the member lifecycle gets the real open-lead
    # count instead of the M1 placeholder that always answered zero. Registered
    # here rather than in the lifespan: it is a stateless swap with no client to
    # tear down, and a test building the app without entering the lifespan must
    # still get the guarantee rather than the placeholder.
    set_lead_ownership(DatabaseLeadOwnership())

    if resolved.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Root mount for container / load-balancer probes. Kept out of the OpenAPI
    # schema so the generated client has exactly one health operation.
    app.include_router(health_router.router, include_in_schema=False)
    # Versioned mount for API clients.
    app.include_router(health_router.router, prefix=resolved.api_v1_prefix)

    # Unscoped: how a caller authenticates and finds the workspaces they may
    # then address.
    app.include_router(auth_router.router, prefix=resolved.api_v1_prefix)
    app.include_router(workspaces_router.router, prefix=resolved.api_v1_prefix)
    # Tenant data. Every route below this prefix resolves a workspace scope
    # before it touches the database.
    tenant_prefix = f"{resolved.api_v1_prefix}/workspaces/{{workspace_id}}"
    app.include_router(members_router.router, prefix=tenant_prefix)
    app.include_router(permission_templates_router.router, prefix=tenant_prefix)
    app.include_router(fields_router.router, prefix=tenant_prefix)
    app.include_router(pipeline_router.router, prefix=tenant_prefix)
    app.include_router(pipeline_router.custom_actions_router, prefix=tenant_prefix)
    app.include_router(leads_router.router, prefix=tenant_prefix)
    app.include_router(views_router.router, prefix=tenant_prefix)
    app.include_router(work_router.router, prefix=tenant_prefix)
    app.include_router(routing_router.router, prefix=tenant_prefix)
    app.include_router(integrations_router.router, prefix=tenant_prefix)
    app.include_router(reports_router.router, prefix=tenant_prefix)
    # Unscoped by path: the workspace comes from the API key, not the URL.
    app.include_router(intake_router.router, prefix=resolved.api_v1_prefix)
    # The Tally form webhook. A thin adapter in front of the same
    # `IntakeService` the line above uses — it adds a translation step and no
    # new write path, so every intake guarantee applies to it unchanged.
    app.include_router(tally_router.router, prefix=resolved.api_v1_prefix)

    return app


app = create_app()
