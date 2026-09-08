"""Alembic's `env.py` must not disable the app's own loggers.

`logging.config.fileConfig` defaults to `disable_existing_loggers=True`, which
switches off every logger the ini does not name - including `mcm.audit`, the
one that prints the game log and every action outcome.

Two postgres-only tests run alembic in-process, so when they ran before
`test_paused_console_line.py` the audit logger was already dead and it captured
no records at all: `assert lines, printed.text` failed on an empty list, and
only ever under `mise run test:pg`.

In production `alembic upgrade head` is a one-shot in its own process (see
docker-compose's `migrate` service), so this cost nothing there - but the same
silencing would apply to anything that ever ran a migration in-process.
"""

from __future__ import annotations

import logging
from pathlib import Path

ENV_PY = Path(__file__).resolve().parents[1] / "alembic" / "env.py"


def test_alembic_env_does_not_disable_existing_loggers() -> None:
    source = ENV_PY.read_text()
    assert "fileConfig(" in source, "env.py no longer calls fileConfig - update this test"
    assert "disable_existing_loggers=False" in source, (
        "alembic/env.py calls fileConfig without disable_existing_loggers=False, "
        "which silences mcm.audit for the rest of the process"
    )


def test_running_alembics_logging_config_leaves_the_audit_logger_alive() -> None:
    """The behavioural half: drive the real ini through fileConfig the way
    `env.py` does, and confirm `mcm.audit` still emits afterwards."""
    from logging.config import fileConfig

    audit = logging.getLogger("mcm.audit")
    ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    fileConfig(str(ini), disable_existing_loggers=False)
    assert audit.disabled is False, "mcm.audit was disabled by alembic's logging config"
