"""What a person watching `docker compose logs` actually sees.

Two things were invisible in the game container's output: a seat's problem
report (the console line printed the action but never the text), and every
real request, drowned under a healthcheck line every five seconds. Both are
tested here against the real app and the real `logging` machinery - the access
lines are logged through uvicorn's own logger, in uvicorn's own shape.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from magic_cards_model_api import access_log, audit
from magic_cards_model_api.main import app


class Capture(logging.Handler):
    """A real handler on a real logger: what the container would print."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def text(self) -> str:
        return "\n".join(record.getMessage() for record in self.records)


@contextmanager
def capturing(logger_name: str) -> Iterator[Capture]:
    logger = logging.getLogger(logger_name)
    handler = Capture()
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


# ---- problem reports -------------------------------------------------------


def test_a_problem_report_prints_its_text_to_the_console(client, game):
    """The whole value of `report_problem` is the text. It used to be dropped:
    `_console_line` prints only card/target, so the console said
    `alice report_problem` and nothing more."""
    with capturing("mcm.audit") as printed:
        response = client.post(
            f"/api/games/{game}/report",
            json={"player": "alice", "text": "Bolt dealt 2 damage\nits text says 3"},
        )
    assert response.json() == {"recorded": True}

    assert "Bolt dealt 2 damage" in printed.text
    assert "its text says 3" in printed.text, "every line of the report is printed"
    assert "PROBLEM REPORTED by alice" in printed.text


def test_a_problem_report_is_louder_than_an_ordinary_action(client, game):
    """WARNING, not INFO, and banner-delimited: it must be noticeable in a log
    that carries a line for every single action of the game."""
    with capturing("mcm.audit") as printed:
        client.post(f"/api/games/{game}/report", json={"player": "bob", "text": "wrong"})

    (record,) = [r for r in printed.records if "PROBLEM REPORTED" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("=" * 72)


def test_ordinary_actions_stay_at_info(client, game):
    with capturing("mcm.audit") as printed:
        client.post(
            f"/api/games/{game}/cards",
            json={"card": "Forest", "player": "alice", "zone": "battlefield"},
        )
    assert printed.records, "adding a card is still logged"
    assert {r.levelno for r in printed.records} == {logging.INFO}


def test_a_report_with_no_text_still_prints_a_banner():
    """A report is never swallowed, however empty."""
    entry = {"seq": 3, "turn": 1, "step": "upkeep", "detail": {"player": "alice", "text": "  "}}
    block = audit._problem_block(entry)
    assert "PROBLEM REPORTED by alice" in block
    assert "(no text)" in block


# ---- the healthcheck's access log ------------------------------------------


def access_line(path: str, status: int, method: str = "GET") -> None:
    """Log one line exactly as uvicorn's access logger does: its format string
    and its five-tuple `(client, method, full_path, http_version, status)`."""
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "172.18.0.1:53000", method, path, "1.1", status
    )


def test_the_health_route_the_probe_hits_is_real():
    """The filter is only sound because the probe keeps hitting a real route."""
    with TestClient(app) as probe:
        assert probe.get(access_log.HEALTH_PATH).status_code == 200


def test_a_successful_health_probe_is_not_printed():
    access_log.install()
    with capturing("uvicorn.access") as printed:
        access_line("/api/health", 200)
    assert printed.records == []


def test_real_traffic_is_still_printed():
    access_log.install()
    with capturing("uvicorn.access") as printed:
        access_line("/api/games/abc/actions/cast", 200, method="POST")
        access_line("/api/games/abc", 200)
        access_line("/play/", 200)
    assert len(printed.records) == 3, printed.text


def test_a_failing_health_probe_is_still_printed():
    """A healthcheck that stopped working is the one worth reading."""
    access_log.install()
    with capturing("uvicorn.access") as printed:
        access_line("/api/health", 500)
        access_line("/api/health", 404)
    assert len(printed.records) == 2, printed.text


def test_the_filter_is_installed_once_however_often_install_runs():
    first = access_log.install()
    assert access_log.install() is first
    assert [f for f in logging.getLogger("uvicorn.access").filters if f is first] == [first]


def test_a_record_that_is_not_an_access_line_passes_through():
    """Never the reason a log line goes missing."""
    access_log.install()
    with capturing("uvicorn.access") as printed:
        printed_logger_message = "something entirely different"
        logging.getLogger("uvicorn.access").info(printed_logger_message)
        logging.getLogger("uvicorn.access").info("two %s of %s", "args", "mine")
    assert len(printed.records) == 2
