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


def test_an_overruled_pregame_decision_is_just_as_loud(client, game):
    """The other thing a person watching the log must not miss: the server
    took a decision away from a seat, because a backstop had already made that
    seat's pregame move when its own arrived (`ai_pregame.py`). A compact INFO
    line among a hundred routine ones is not how that surfaces."""
    with capturing("mcm.audit") as printed:
        audit.record(
            game,
            audit.OVERRULE_ACTION,
            player="claude",
            allowed=False,
            reason="the backstop had already made this pregame move for this seat",
            kind="mulligan",
            text="this CR 103.5 declaration was NOT applied",
        )

    (record,) = [r for r in printed.records if "OVERRULED" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("=" * 72)
    assert "PREGAME DECISION OVERRULED for claude" in record.getMessage()
    assert "this CR 103.5 declaration was NOT applied" in record.getMessage()


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
    entry = {
        "seq": 3,
        "turn": 1,
        "step": "upkeep",
        "action": audit.PROBLEM_ACTION,
        "detail": {"player": "alice", "text": "  "},
    }
    block = audit._loud_block(entry)
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


#: Every read-only route the stack polls in a loop, across all three
#: containers. `docker compose up` used to be almost nothing but these.
POLLED = [
    "/api/health",
    "/healthz",
    "/readyz",
    "/status",
    "/api/games/f8e61463c97842a2b5ff6790834b36e6",
    "/api/games/f8e61463c97842a2b5ff6790834b36e6?uid=beefcafe",
    "/api/ui/games/f8e61463c97842a2b5ff6790834b36e6/ai-seat/status",
    "/api/ui/games/f8e61463c97842a2b5ff6790834b36e6/ai-seat/status?uid=beefcafe",
    "/api/ui/games",
    "/api/ui/games?uids=beefcafe",
]


def test_every_polled_read_is_silenced():
    access_log.install()
    with capturing("uvicorn.access") as printed:
        for path in POLLED:
            access_line(path, 200)
    assert printed.records == [], printed.text


def test_a_poll_that_fails_is_still_printed():
    """A 404 or a 500 on a polled route is exactly what someone opens the log
    for; only the *successful* poll is noise."""
    access_log.install()
    with capturing("uvicorn.access") as printed:
        for path in POLLED:
            access_line(path, 500)
        access_line("/api/health", 404)
        access_line("/status", 502)
    assert len(printed.records) == len(POLLED) + 2, printed.text


def test_a_mutation_is_never_silenced():
    """Not even one aimed at a path on the quiet list. A POST is a decision
    somebody made."""
    access_log.install()
    with capturing("uvicorn.access") as printed:
        access_line("/api/games/abc", 200, method="POST")
        access_line("/api/games/abc", 200, method="DELETE")
        access_line("/status", 200, method="POST")
    assert len(printed.records) == 3, printed.text


def test_real_traffic_is_still_printed():
    access_log.install()
    with capturing("uvicorn.access") as printed:
        access_line("/api/games/abc/actions/cast", 200, method="POST")
        access_line("/api/games/abc/actions", 200)
        access_line("/api/games", 200)
        access_line("/play/", 200)
        access_line("/api/games/abc/log", 200)
    assert len(printed.records) == 5, printed.text


def test_the_silenced_game_routes_are_the_ones_the_app_serves(client, game):
    """The filter is only sound if its patterns match routes that exist. These
    are the ones the browser polls; all are answered here for real, and the
    path each request actually took is the one that gets silenced."""
    for response in (
        client.get(f"/api/games/{game}"),
        client.get(f"/api/ui/games/{game}/ai-seat/status", params={"uid": "nobody"}),
        client.get("/api/ui/games"),
    ):
        assert response.status_code == 200, response.text
        assert access_log.is_quiet_path(response.request.url.path), response.request.url


def test_the_quiet_paths_are_anchored_whole():
    """A prefix match would have swallowed half the API - `/api/games/{id}` is
    silent, everything under it is not. Same for `/api/ui/games`: the bare
    list is the lobby's poll and is silent, but a specific game under it
    (`.../ai-seat/status` aside, which has its own pattern) is a new question,
    not the loop."""
    assert access_log.is_quiet_path("/api/games/abc")
    assert not access_log.is_quiet_path("/api/games")
    assert not access_log.is_quiet_path("/api/games/abc/seat")
    assert not access_log.is_quiet_path("/status/detail")
    assert not access_log.is_quiet_path("/api/healthy")
    assert access_log.is_quiet_path("/api/ui/games")
    assert not access_log.is_quiet_path("/api/ui/games/abc")


def test_the_env_var_gives_the_whole_access_log_back():
    """`ACCESS_LOG_QUIET=0` is the escape hatch, and it is the last word
    however many times the quiet default installed first."""
    access_log.install()
    assert access_log.install({"ACCESS_LOG_QUIET": "0"}) is None
    with capturing("uvicorn.access") as printed:
        access_line("/api/health", 200)
        access_line("/api/games/abc", 200)
    assert len(printed.records) == 2, printed.text
    access_log.install({})  # back to quiet for the rest of the suite


def test_quiet_is_the_default_and_off_needs_saying():
    assert access_log.quiet_enabled({})
    assert access_log.quiet_enabled({"ACCESS_LOG_QUIET": "1"})
    assert not access_log.quiet_enabled({"ACCESS_LOG_QUIET": "0"})
    assert not access_log.quiet_enabled({"ACCESS_LOG_QUIET": "false"})


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
