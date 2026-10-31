"""The `arq` worker's entry point (M8/M10).

These assert the *shape* `arq` requires, not behaviour — `tick` and `dispatch`
have their own tests, and calling them directly is exactly why this file has to
exist.

`WorkerSettings.redis_settings` shipped as a `@staticmethod`. It type-checked,
imported cleanly, and passed every test in the suite; the container then died on
the first line of the real worker with `'staticmethod' object has no attribute
'host'`, because `arq` reads it as an attribute and immediately asks it for
`.host`. The only thing that caught it was running `docker compose up`.

So: cheap assertions about the contract with the framework, which is the part
unit tests of our own functions can never reach.
"""

from __future__ import annotations

import pytest
from arq.connections import RedisSettings

from app.workers.scheduler import (
    HEARTBEAT_KEY,
    HEARTBEAT_TTL_SECONDS,
    WorkerSettings,
    dispatch,
    heartbeat,
    settle_numbers,
    tick,
)


def test_the_arq_entry_point_matches_what_arq_expects() -> None:
    """`redis_settings` is an attribute holding a `RedisSettings`.

    A method here passes mypy and every unit test, and fails 100% of the time
    in production.
    """
    settings = WorkerSettings.redis_settings
    assert isinstance(settings, RedisSettings), (
        f"arq reads this as an attribute and asks it for .host; got {type(settings).__name__}"
    )
    assert settings.host
    assert settings.port


def test_every_cron_job_is_registered() -> None:
    """The scheduler tick, the outbox dispatcher, the heartbeat and rent.

    Losing one is silent: schedules simply stop arriving, or webhooks stop
    being delivered, with nothing in any log to say a job was never registered.
    """
    registered = {job.coroutine for job in WorkerSettings.cron_jobs}
    assert registered == {tick, dispatch, heartbeat, settle_numbers}, {
        job.name for job in WorkerSettings.cron_jobs
    }


@pytest.mark.parametrize("hook", ["on_startup", "on_shutdown"])
def test_the_lifecycle_hooks_are_callable(hook: str) -> None:
    """`on_startup` builds the engine, session factory and transports.

    Without it every job raises `KeyError` on the first `ctx[...]` lookup.
    """
    assert callable(getattr(WorkerSettings, hook))


async def test_the_heartbeat_tells_the_console_the_worker_is_alive() -> None:
    """The console's "Background worker: running" light reads this key."""
    stored: dict[str, tuple[str, int | None]] = {}

    class FakeRedis:
        async def set(self, key: str, value: str, ex: int | None = None) -> None:
            stored[key] = (value, ex)

    moment = await heartbeat({"redis": FakeRedis()})

    assert stored[HEARTBEAT_KEY][0] == moment
    assert stored[HEARTBEAT_KEY][1] == HEARTBEAT_TTL_SECONDS
