"""The driver's own container output.

`/turn-done` carried the agent's `finalText` from the start and the driver
dropped it on the floor, so nothing the agent said about the game it was
playing ever reached `docker compose logs`. These tests drive the real driver
app over ASGI (the harness in `test_app.py`) and read what it logged.
"""

from __future__ import annotations

import logging

import pytest

from mtg_agent.driver.app import trim_final_text
from tests.driver.test_app import dispatch, world  # noqa: F401 - `world` is the fixture


@pytest.fixture
def driver_log(caplog):
    caplog.set_level(logging.INFO, logger="driver")
    return caplog


def test_a_finished_turn_logs_what_the_agent_said(world, driver_log):  # noqa: F811
    client, _, _, _ = world
    dispatch(client)
    driver_log.clear()

    client.post(
        "/turn-done",
        json={
            "turnId": "turn-1",
            "state": "complete",
            "stopReason": "end_turn",
            "finalText": "Played Psychic Frog, held Fatal Push for their Ragavan.",
        },
    )

    assert "Played Psychic Frog" in driver_log.text
    assert "turn turn-1 done" in driver_log.text


def test_a_long_account_is_trimmed_rather_than_dumped_whole():
    trimmed = trim_final_text("x" * 5000, limit=100)
    assert len(trimmed) < 200
    assert "more characters" in trimmed


def test_a_silent_turn_says_so_rather_than_logging_nothing():
    assert "without saying anything" in trim_final_text("   \n\n  ")


def test_trimming_keeps_the_agents_line_breaks_and_drops_the_blank_ones():
    trimmed = trim_final_text("first\n\n\nsecond")
    assert trimmed == "  first\n  second"


def test_a_failed_turn_is_logged_as_an_error(world, driver_log):  # noqa: F811
    client, _, _, _ = world
    dispatch(client)
    driver_log.clear()

    client.post(
        "/turn-done",
        json={"turnId": "turn-1", "state": "failed", "error": "claude exited with code 1"},
    )

    errors = [r for r in driver_log.records if r.levelno >= logging.ERROR]
    assert errors, driver_log.text
    assert "claude exited with code 1" in driver_log.text


def test_pushing_a_turn_is_logged(world, driver_log):  # noqa: F811
    client, _, _, _ = world
    driver_log.clear()
    dispatch(client)
    assert "turn turn-1 pushed to the agent" in driver_log.text


def test_dispatch_logs_the_deck_it_identified_and_how_well_it_matched(world, driver_log):  # noqa: F811
    client, _, _, _ = world
    driver_log.clear()
    dispatch(client)
    assert "deck=dimir-frog" in driver_log.text
    assert "match 1.00" in driver_log.text


def test_the_end_of_the_match_is_logged(world, driver_log):  # noqa: F811
    client, _, _, state = world
    dispatch(client)
    state["winner"] = "bob"
    driver_log.clear()

    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
    assert "over (bob won)" in driver_log.text

    client.post("/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "notes"})
    assert "retired" in driver_log.text
