"""T36: the end-to-end proof that a game which never had a reflection turn
still files an AI review, and that the review page can read it.

**Why this file exists at all.** Defect D-7 was "the review loop may never
close": `post_ai_review` used to be spawned from exactly one place -
`turn_done`'s reflection branch - and the live-observed ending never reaches
that branch. The lobby releases the game, the driver hands over first
(`releasing game ... without a reflection turn`), and the in-flight
`/turn-done` that follows returns on the handover branch *before* the
reflection check is ever read. T34 funnelled every terminal path through
`consider_ai_review` and T35 made a failed review durable, but nothing so far
drove the whole chain - driver ordering, the real api routes, the real
wrapper's `/v1/review` - and then looked at what the review page would
actually render. There is still no naturally-concluded game in the live
deployment, so this file is the evidence that the loop closes.

**What is real here, and what is not.** The game server is the real
`magic_cards_model_api` app, with a real game played to a real terminal
state (alice concedes), so every gate the driver's review calls have to pass
is the production gate: post-game only, the seat's own uid, an AI seat.
`GET /api/games/{id}/review` is read back off that same app, and
`ui/review_model.js`'s own `aiReviewsOf` is run against the body under Node -
the page's reader, not a Python restatement of it. The review generation is
the real wrapper route (`mtg_agent.wrapper.api`'s `POST /v1/review`, whose
`ReviewIn` is `extra="forbid"`, so a driver that posted a stray field would
fail here) with its `claude` subprocess faked through the injected
`review_runner` - `tests/wrapper/test_api.py`'s `review_wired` fixture is the
prior art. No model call is ever made.

The one thing stood in for is the supervised session itself: `/v1/messages`,
`/v1/interrupt` and `/v1/session` are the small fakes `tests/driver/world.py`
already uses, because a real `Manager` over a `FakeProc` would deliver its
own turn callbacks over real HTTP to a driver that only exists in-process.
The turn callbacks are posted by the test instead, which is exactly what
makes the *ordering* under test (clause b) explicit rather than incidental.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from magic_cards_model_api import audit
from magic_cards_model_api.main import app as api_app
from mtg_agent.driver.app import build_app
from mtg_agent.driver.config import DriverSettings
from mtg_agent.driver.prompt import HANDOVER_OPENING
from mtg_agent.wrapper.api import build_public_app
from mtg_agent.wrapper.oneshot import ReviewFailed
from tests.ui_node import requires_node, run_module
from tests.wrapper.test_session import FakeProc, fast_settings, make

REVIEW_TEXT = (
    "bob held the removal a turn too long on turn 4 and the game was decided there; "
    "the release means there is no reflection to check that against"
)

UI_MODULE = "review_model.js"


def wait_for(predicate, timeout: float = 10.0):
    """Poll until the driver's background work lands. Bounded, so a
    regression fails the test instead of hanging the suite."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def _wrapper_app(review_runner=None, *, raw_review=None) -> tuple[FastAPI, list[dict], list[dict]]:
    """The agent container's HTTP surface: the supervised session faked, the
    post-game review route real - or, with `raw_review`, answered directly.

    `/v1/review` forwards the driver's own body, verbatim, into the real
    `mtg_agent.wrapper.api` app built with `review_runner` in place of
    `oneshot.run_review` - so the request the driver sends is judged by the
    real `ReviewIn` (`extra="forbid"`) and the answer the driver reads is the
    real route's, while no `claude` subprocess is ever spawned.

    `raw_review`, when given, answers `/v1/review` directly with a
    caller-built `Response` instead - for exercising exactly what the
    driver's `_wrapper_failure_reason` does with a wrapper response body
    `wrapper.api.post_review`'s own `str(ReviewFailed(...))` could never
    produce (a non-JSON body, an empty one), rather than only what that
    route happens to shape."""
    pushed: list[dict] = []
    reviewed: list[dict] = []
    real = None
    if raw_review is None:
        # An unstarted `Manager`: `POST /v1/review` is deliberately
        # independent of the supervised session (it never submits a turn,
        # never waits on one), so nothing here needs a running pty or its
        # threads.
        real = TestClient(
            build_public_app(make(FakeProc(b"ready")), fast_settings(), review_runner=review_runner)
        )

    app = FastAPI()

    @app.post("/v1/messages", status_code=202)
    def post_message(body: dict) -> dict:
        pushed.append(body)
        return {"turnId": f"turn-{len(pushed)}"}

    @app.post("/v1/interrupt", status_code=202)
    def post_interrupt() -> dict:
        return {"turnId": f"turn-{len(pushed)}"}

    @app.get("/v1/session")
    def get_session() -> dict:
        return {"model": "", "effort": "", "maxThinkingTokens": None, "activity": None}

    @app.post("/v1/session", status_code=202)
    def set_session(body: dict) -> dict:
        return {"restarting": True}

    @app.delete("/v1/session", status_code=202)
    def delete_session() -> dict:
        return {"stopping": True, "exiting": True}

    @app.post("/v1/review")
    def post_review(body: dict) -> Response:
        reviewed.append(body)
        if raw_review is not None:
            return raw_review(body)
        answer = real.post("/v1/review", json=body)
        return JSONResponse(status_code=answer.status_code, content=answer.json())

    return app, pushed, reviewed


def _driver(
    tmp_path: Path, wrapper: FastAPI, *, game_client_factory=None, **settings_overrides
) -> TestClient:
    """`game_client_factory`, when given, replaces the plain
    `TestClient(api_app, ...)` the game side normally gets - FD6 (4)'s
    review-record retry tests use it to inject a scripted sequence of
    timeouts/409s onto the review-record GET specifically, while every
    other game-api call still reaches the real app unchanged.
    `settings_overrides` reach `DriverSettings` directly, so the same tests
    can shrink the retry budget/backoff to keep themselves fast."""

    def client_factory(base_url: str) -> httpx.Client:
        if "game" not in base_url:
            return TestClient(wrapper, base_url=base_url)
        if game_client_factory is not None:
            return game_client_factory(base_url)
        return TestClient(api_app, base_url=base_url)

    settings = DriverSettings(
        game_api="http://game:8000",
        wrapper_api="http://agent:8080",
        wrapper_secret="",
        guides_root=tmp_path / "guides",
        memory_root=tmp_path / "memory",
        workspace=tmp_path / "workspace",
        self_url="http://driver:8090",
        http_addr=":8090",
        agent_boot_wait=1.0,
        agent_retry_interval=0.01,
        **settings_overrides,
    )
    return TestClient(build_app(settings, client_factory=client_factory))


def _agent_game(api_client: TestClient) -> dict[str, Any]:
    """A real game with `bob` registered as an AI seat - the only kind of
    seat `declare_ai_review` will ever accept a review for."""
    response = api_client.post(
        "/api/games",
        json={
            "players": [
                {"username": "bob", "is_agent": True},
                {"username": "alice", "is_agent": False},
            ],
            "active_player": "bob",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _concede(api_client: TestClient, game_id: str, player: str = "alice") -> None:
    conceded = api_client.post(
        f"/api/games/{game_id}/actions/concede",
        json={"player": player, "reasoning": "the race is lost"},
    )
    assert conceded.status_code == 200, conceded.text


def _dispatch(driver: TestClient, info: dict[str, Any]) -> httpx.Response:
    response = driver.post(
        "/dispatch",
        json={
            "uid": info["seat_uids"]["bob"],
            "game_id": info["game_id"],
            "player": "bob",
            "api_base": "http://game:8000",
        },
    )
    assert response.status_code == 202, response.text
    return response


def _complete_handover(driver: TestClient, pushed: list[dict]) -> str:
    ids = wait_for(
        lambda: [
            f"turn-{index + 1}"
            for index, turn in enumerate(pushed)
            if turn["text"].startswith(HANDOVER_OPENING)
        ]
    )
    driver.post(
        "/turn-done", json={"turnId": ids[0], "state": "complete", "finalText": "handed over"}
    )
    return ids[0]


def _review_body(api_client: TestClient, info: dict[str, Any]) -> dict[str, Any]:
    response = api_client.get(
        f"/api/games/{info['game_id']}/review", params={"key": info["admin_key"]}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _rows(body: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [entry for entry in body["entries"] if entry["action"] == action]


def released_without_a_reflection(
    tmp_path: Path,
    review_runner=None,
    raw_review=None,
    *,
    game_client_factory=None,
    **settings_overrides,
) -> SimpleNamespace:
    """Drive one whole game down the live-observed path and hand back what
    the surfaces say afterwards.

    The ordering is the real one, not a convenient one: the game ends, the
    LOBBY releases the seat, the driver hands over first, and the turn that
    was in flight reports back *during* that handover - so `turn_done`
    returns on its handover branch and the reflection check below it is never
    reached. Nothing here calls `post_ai_review`, or any other driver
    internal, by hand.

    `game_client_factory`/`settings_overrides` reach `_driver` unchanged -
    FD6 (4)'s review-record retry tests use them to inject scripted
    failures and to shrink the retry backoff so the test stays fast.
    """
    runner = review_runner or (lambda settings, prompt, model, effort, **_kwargs: REVIEW_TEXT)
    wrapper, pushed, reviewed = _wrapper_app(runner, raw_review=raw_review)
    with TestClient(api_app) as api_client:
        info = _agent_game(api_client)
        driver = _driver(
            tmp_path, wrapper, game_client_factory=game_client_factory, **settings_overrides
        )
        try:
            _dispatch(driver, info)
            # The game genuinely ends - a real concession through the real
            # engine, so every post-game gate the review has to pass is the
            # production one.
            _concede(api_client, info["game_id"])

            released = driver.post(
                "/release", json={"uid": info["seat_uids"]["bob"], "reason": "lobby reset"}
            ).json()
            # The turn the agent was in the middle of lands now, during the
            # handover. This is the branch that returns early.
            turn_done = driver.post(
                "/turn-done",
                json={"turnId": "turn-1", "state": "complete", "finalText": "I attack"},
            ).json()
            _complete_handover(driver, pushed)
            wait_for(lambda: driver.get("/status").json()["gameId"] is None)
            body = _review_body(api_client, info)
        finally:
            api_client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})
    return SimpleNamespace(
        info=info,
        released=released,
        turn_done=turn_done,
        review=body,
        pushed=pushed,
        reviewed=reviewed,
    )


# -- (a) + (b): the review is filed, and the page can read it ---------------


def test_a_released_game_that_never_reflected_files_a_review_the_page_can_read(tmp_path):
    """The whole of D-7, closed: a game ends, the lobby releases the seat, no
    reflection turn is ever pushed - and `GET /api/games/{id}/review` carries
    an `ai_review` row, in the shape `aiReviewsOf` reads."""
    run = released_without_a_reflection(tmp_path)

    # (b) The real ordering: the release handed over first, and the turn that
    # came back landed on the handover branch - `turn_done` answered with the
    # handover status and never read the reflection check below it.
    assert run.released["reflected"] is False
    assert set(run.turn_done) == {"handover"}
    assert run.turn_done["handover"]["outcome"] == "released"
    # No reflection was pushed to the agent, and none reached the ledger:
    # the agent was given turn zero and then, straight away, the handover.
    assert len(run.pushed) == 2
    assert run.pushed[1]["text"].startswith(HANDOVER_OPENING)
    assert _rows(run.review, audit.REFLECTION_ACTION) == []

    # (a) The review itself, on the surface the UI reads.
    reviews = _rows(run.review, audit.AI_REVIEW_ACTION)
    assert len(reviews) == 1, run.review["entries"]
    entry = reviews[0]
    assert entry["detail"]["player"] == "bob"
    # DR3 (3): the filed text is the reviewer's own words plus the appended
    # "Play statistics" block - never the reviewer's text alone any more.
    assert entry["detail"]["text"].startswith(REVIEW_TEXT)
    assert "Play statistics:" in entry["detail"]["text"]
    assert isinstance(entry["seq"], int)
    assert entry["ts"]
    # And nothing claimed the review failed.
    assert run.review["ai_review_failures"] == []
    # The generation really went through the wrapper's own route.
    assert len(run.reviewed) == 1
    assert sorted(run.reviewed[0]) == ["decision_count", "effort", "model", "prompt"]
    assert "(the seat left no reflection)" in run.reviewed[0]["prompt"]


def test_a_reviews_turning_points_are_stored_on_the_real_review_record(tmp_path):
    """DR3 owner addon 2, end to end through the real production schema
    (`AiReviewIn`, `extra="forbid"`, `declare_ai_review`): the reviewer's own
    `turning_points: {...}` closing line lands as structured data on the
    `ai_review` row's own `detail`, not only inside `detail["text"]`'s
    prose."""
    text_with_turning_points = (
        REVIEW_TEXT + "\n\n"
        'turning_points: {"bob": [{"turn": 6, "action": "attacked into open mana", '
        '"why": "walked into the sweeper"}]}'
    )

    def runner(settings, prompt, model, effort, **_kwargs):
        return text_with_turning_points

    run = released_without_a_reflection(tmp_path, review_runner=runner)

    entry = _rows(run.review, audit.AI_REVIEW_ACTION)[0]
    assert entry["detail"]["turning_points"] == {
        "bob": [
            {
                "turn": 6,
                "action": "attacked into open mana",
                "why": "walked into the sweeper",
            }
        ]
    }
    # Still shown in the filed review's own prose too, not replaced by it.
    assert "turning_points:" in entry["detail"]["text"]


@requires_node
def test_the_review_page_reads_that_filed_review_out_of_the_real_body(tmp_path):
    """The join, under Node: `ui/review_model.js`'s own `aiReviewsOf` - the
    function `ui/render/review.js` builds every `section.ai-review` from -
    run against the real `GET .../review` body this path produces. A key or a
    shape that drifted on either side shows up here as an empty list, which
    is exactly the symptom the live deployment had."""
    run = released_without_a_reflection(tmp_path)

    found = run_module(UI_MODULE, "m.aiReviewsOf(rev)", rev=run.review)
    assert len(found) == 1, found
    # DR3 (3): the appended "Play statistics" block, same as above.
    assert found[0]["text"].startswith(REVIEW_TEXT)
    assert "Play statistics:" in found[0]["text"]
    assert found[0]["player"] == "bob"
    # The page never mistakes it for the seat's own write-up: this game has
    # no reflection at all, and the reflection reader says so.
    assert run_module(UI_MODULE, "m.reflectionOf(rev)", rev=run.review) is None


# -- (c) the reflection path still files exactly one review -----------------


def test_the_reflection_path_still_files_exactly_one_review(tmp_path):
    """The ordinary ending, through the same real surfaces. It reaches
    `consider_ai_review` three times - the reflection branch, the handover it
    starts, and the `retire` funnel at the end of that handover - plus a
    retried `/turn-done` delivery on top, which `webhook.Deliverer` really
    does send. Exactly one `ai_review` row, and exactly one reflection."""
    wrapper, pushed, reviewed = _wrapper_app(
        lambda settings, prompt, model, effort, **_kwargs: REVIEW_TEXT
    )
    with TestClient(api_app) as api_client:
        info = _agent_game(api_client)
        driver = _driver(tmp_path, wrapper)
        try:
            _dispatch(driver, info)
            _concede(api_client, info["game_id"])
            # The turn that was in flight when the game ended: the driver
            # reads the result off the game server and pushes the reflection.
            ended = driver.post(
                "/turn-done",
                json={"turnId": "turn-1", "state": "complete", "finalText": "gg"},
            ).json()
            assert ended["outcome"] == "bob won"
            assert ended["reflectionTurnId"] == "turn-2"
            reflected = driver.post(
                "/turn-done",
                json={"turnId": "turn-2", "state": "complete", "finalText": "wrote up the game"},
            ).json()
            assert reflected["reflected"] is True
            # The same callback delivered twice more, as the webhook does.
            driver.post(
                "/turn-done",
                json={"turnId": "turn-2", "state": "complete", "finalText": "wrote up the game"},
            )
            driver.post(
                "/turn-done",
                json={"turnId": "turn-2", "state": "complete", "finalText": "wrote up the game"},
            )
            _complete_handover(driver, pushed)
            wait_for(lambda: driver.get("/status").json()["gameId"] is None)
            # A late preStop handover, after retirement, must not file another.
            driver.post("/handover", json={"outcome": "bob won"})
            time.sleep(0.2)
            body = _review_body(api_client, info)
        finally:
            api_client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})

    assert len(_rows(body, audit.AI_REVIEW_ACTION)) == 1, body["entries"]
    assert len(_rows(body, audit.REFLECTION_ACTION)) == 1
    assert len(reviewed) == 1, "the reviewer was asked more than once"
    # This one HAS the seat's own words to review, unlike the released path.
    assert "wrote up the game" in reviewed[0]["prompt"]
    assert body["ai_review_failures"] == []


@requires_node
def test_the_page_shows_one_review_and_the_reflection_beside_it(tmp_path):
    """The dedupe is only worth as much as what the page ends up drawing:
    `aiReviewsOf` returns one entry per player, so a second filed row would
    surface here as a changed text rather than as a visible duplicate."""
    wrapper, pushed, _ = _wrapper_app(
        lambda settings, prompt, model, effort, **_kwargs: REVIEW_TEXT
    )
    with TestClient(api_app) as api_client:
        info = _agent_game(api_client)
        driver = _driver(tmp_path, wrapper)
        try:
            _dispatch(driver, info)
            _concede(api_client, info["game_id"])
            driver.post(
                "/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"}
            )
            driver.post(
                "/turn-done",
                json={"turnId": "turn-2", "state": "complete", "finalText": "wrote up the game"},
            )
            _complete_handover(driver, pushed)
            wait_for(lambda: driver.get("/status").json()["gameId"] is None)
            body = _review_body(api_client, info)
        finally:
            api_client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})

    found = run_module(UI_MODULE, "m.aiReviewsOf(rev)", rev=body)
    assert len(found) == 1, found
    # DR3 (3): the appended "Play statistics" block, same as above.
    assert found[0]["text"].startswith(REVIEW_TEXT)
    assert "Play statistics:" in found[0]["text"]
    reflection = run_module(UI_MODULE, "m.reflectionOf(rev)", rev=body)
    assert reflection["text"] == "wrote up the game"


def test_driver_settings_review_record_retry_defaults_and_env() -> None:
    defaults = DriverSettings.from_env({})
    assert defaults.review_record_timeout == 120.0
    assert defaults.review_record_timeout > 30.0, "well above the old fixed 30s"
    assert defaults.review_record_retry_budget == 300.0
    assert defaults.review_record_retry_start == 2.0
    assert defaults.review_record_retry_max_delay == 30.0

    overridden = DriverSettings.from_env(
        {
            "REVIEW_RECORD_TIMEOUT_SECONDS": "90",
            "REVIEW_RECORD_RETRY_BUDGET_SECONDS": "60",
            "REVIEW_RECORD_RETRY_START_SECONDS": "1",
            "REVIEW_RECORD_RETRY_MAX_DELAY_SECONDS": "5",
        }
    )
    assert overridden.review_record_timeout == 90.0
    assert overridden.review_record_retry_budget == 60.0
    assert overridden.review_record_retry_start == 1.0
    assert overridden.review_record_retry_max_delay == 5.0


# -- FD6 (4): the review-record GET retries on a timeout or a 409 ----------


class _FlakyReviewRecordClient:
    """Wraps a real `TestClient(api_app, ...)`, injecting a scripted
    sequence of failures onto the review-record GET specifically (`.../
    review`) - every other game-api call the driver makes (dispatch,
    turn-done, plays-outcome, ...) still reaches the real app unchanged.
    Each popped failure is either the string `"timeout"` (raises
    `httpx.TimeoutException`) or an int status code (a bare `httpx.Response`
    with that status, body-less - `post_ai_review` only ever reads
    `response.status_code`/`.json()` on success, never on a non-2xx)."""

    def __init__(self, real: httpx.Client, failures: list) -> None:
        self._real = real
        self._failures = failures

    def __enter__(self) -> _FlakyReviewRecordClient:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def get(self, url: str, params: dict | None = None, timeout: float | None = None):
        if url.endswith("/review") and self._failures:
            failure = self._failures.pop(0)
            if failure == "timeout":
                raise httpx.TimeoutException("simulated timeout")
            return httpx.Response(failure, request=httpx.Request("GET", url))
        return self._real.get(url, params=params, timeout=timeout)

    def post(self, *args, **kwargs):
        return self._real.post(*args, **kwargs)

    def delete(self, *args, **kwargs):
        return self._real.delete(*args, **kwargs)


def _flaky_game_client_factory(failures: list):
    def factory(base_url: str) -> httpx.Client:
        real = TestClient(api_app, base_url=base_url)
        return _FlakyReviewRecordClient(real, failures)

    return factory


#: Small enough that a whole retry sequence finishes well inside a test's own
#: timeout, large enough that the retry loop genuinely sleeps between tries.
_FAST_RETRY_SETTINGS = {
    "review_record_retry_start": 0.01,
    "review_record_retry_max_delay": 0.02,
    "review_record_retry_budget": 2.0,
}


def test_a_409_then_a_timeout_then_success_still_files_the_review(tmp_path):
    """The game-not-yet-released 409 and a transport timeout are both
    retried, with the review record read successfully on the next try -
    exactly the two live-observed prod failures (fb3-parity.md items 1/6)
    this task exists to close."""
    run = released_without_a_reflection(
        tmp_path,
        game_client_factory=_flaky_game_client_factory([409, "timeout", 409]),
        **_FAST_RETRY_SETTINGS,
    )
    assert run.review["ai_review_failures"] == []
    reviews = _rows(run.review, audit.AI_REVIEW_ACTION)
    assert len(reviews) == 1, run.review["entries"]
    assert reviews[0]["detail"]["text"].startswith(REVIEW_TEXT)


def test_review_record_failures_beyond_the_bound_file_the_final_reason(tmp_path):
    """Once the retry budget is spent, `ai_review_failed` is filed with the
    LAST failure's own reason - never the first, and never silently nothing,
    which is what a caller reading the review page needs to act on. The
    failure sequence outlasts the budget on purpose (a game server stuck
    answering 409 forever), and the budget itself is small enough that only
    a couple of retries fit before it gives up."""
    run = released_without_a_reflection(
        tmp_path,
        game_client_factory=_flaky_game_client_factory([409] * 50),
        review_record_retry_start=0.02,
        review_record_retry_max_delay=0.02,
        review_record_retry_budget=0.05,
    )
    assert _rows(run.review, audit.AI_REVIEW_ACTION) == []
    failure = run.review["ai_review_failures"][0]
    assert failure["stage"] == "review_record"
    assert failure["invoked"] is False
    assert "409" in failure["reason"]


# -- FD6 (4): the review posts its own reviewer plays_outcome row ----------


class _ReviewerPlaysOutcomeCapturingClient:
    """Wraps a real `TestClient(api_app, ...)`, capturing every
    `role: "reviewer"` `/plays-outcome` POST and acknowledging it directly
    rather than forwarding it - the game api's own `declare_plays_outcome`
    (`magic_cards_model_api/app.py`, FD5's file) does not yet accept
    `role="reviewer"` for a seat uid, so this proves what THIS task owns -
    the driver sends the right row, with the right shape - without coupling
    to that separate, not-yet-landed server-side change. Every other call
    (dispatch, turn-done, the `ai-review` post itself, ...) reaches the real
    app unchanged."""

    def __init__(self, real: httpx.Client, captured: list) -> None:
        self._real = real
        self._captured = captured

    def __enter__(self) -> _ReviewerPlaysOutcomeCapturingClient:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def get(self, *args, **kwargs):
        return self._real.get(*args, **kwargs)

    def post(self, url: str, json: dict | None = None, **kwargs):
        if url.endswith("/plays-outcome") and (json or {}).get("role") == "reviewer":
            self._captured.append(json)
            return httpx.Response(200, json={"recorded": True}, request=httpx.Request("POST", url))
        return self._real.post(url, json=json, **kwargs)

    def delete(self, *args, **kwargs):
        return self._real.delete(*args, **kwargs)


def _reviewer_plays_outcome_capturing_factory(captured: list):
    def factory(base_url: str) -> httpx.Client:
        real = TestClient(api_app, base_url=base_url)
        return _ReviewerPlaysOutcomeCapturingClient(real, captured)

    return factory


def test_the_review_files_its_own_reviewer_plays_outcome_row(tmp_path):
    """After the review is filed, the driver posts a `plays_outcome` row
    with `role: 'reviewer'` from the reviewer's own journal - FD7's
    `play_stats` counts it there, separately from the seat's own handover
    row, instead of the reviewer's admits going uncounted (fb3-gen.md gap
    4). Needs a real journal line: an EMPTY reviewer journal posts nothing
    at all (see the test right below this one) - there is nothing here for
    FD7 to count, and posting an empty row would only race the handover's
    own `plays_outcome` POST for readers that assume there is exactly one."""
    captured: list[dict] = []
    wrapper, pushed, _ = _wrapper_app(
        lambda settings, prompt, model, effort, **_kwargs: REVIEW_TEXT
    )
    with TestClient(api_app) as api_client:
        info = _agent_game(api_client)
        _write_reviewer_admit_journal(tmp_path, info["game_id"], added=["p1"], updated=["p2"])
        driver = _driver(
            tmp_path,
            wrapper,
            game_client_factory=_reviewer_plays_outcome_capturing_factory(captured),
        )
        try:
            _dispatch(driver, info)
            _concede(api_client, info["game_id"])
            driver.post(
                "/release", json={"uid": info["seat_uids"]["bob"], "reason": "lobby reset"}
            )
            driver.post(
                "/turn-done",
                json={"turnId": "turn-1", "state": "complete", "finalText": "I attack"},
            )
            _complete_handover(driver, pushed)
            wait_for(lambda: driver.get("/status").json()["gameId"] is None)
        finally:
            api_client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})

    assert len(captured) == 1, captured
    row = captured[0]
    assert row["role"] == "reviewer"
    assert row["player"] == "bob"
    assert row["uid"] == info["seat_uids"]["bob"]
    assert row["admitted"] == [
        {"sha": None, "dsl_change": True, "added": ["p1"], "updated": ["p2"], "reprioritized": []}
    ]
    assert row["state"] == "complete"


def test_an_empty_reviewer_journal_posts_no_plays_outcome_row_at_all(tmp_path):
    """The regression this task's own review path could otherwise cause:
    every fake reviewer in this whole suite writes no journal at all (it
    just returns text), and posting an empty `role: 'reviewer'` row for
    every one of them would race the handover's own `plays_outcome` POST -
    several existing tests read `plays_outcomes[0]` assuming the handover's
    is the only, or at least the first, row this game ever posts."""
    captured: list[dict] = []
    run = released_without_a_reflection(
        tmp_path, game_client_factory=_reviewer_plays_outcome_capturing_factory(captured)
    )
    assert run.review["ai_review_failures"] == []
    assert captured == []


# -- (d) a review that failed is distinguishable on the same surface --------


def test_a_review_that_could_not_be_generated_reaches_the_same_body(tmp_path):
    """T35's durable signal, through the same path and read off the same
    route. The reviewer is asked and fails (the real wrapper route answers
    502 for a `ReviewFailed` runner), so the ledger carries an
    `ai_review_failed` row saying the reviewer WAS invoked - and no
    `ai_review` row, which is what makes the two outcomes tellable apart by
    a reader of `GET .../review` rather than of a pod log."""

    def failing(settings, prompt, model, effort, **_kwargs):
        raise ReviewFailed("claude exited 1")

    run = released_without_a_reflection(tmp_path, review_runner=failing)

    assert _rows(run.review, audit.AI_REVIEW_ACTION) == []
    failures = run.review["ai_review_failures"]
    assert len(failures) == 1, run.review["entries"]
    assert failures[0]["player"] == "bob"
    assert failures[0]["stage"] == "generate"
    assert failures[0]["invoked"] is True
    assert "502" in failures[0]["reason"]
    assert _rows(run.review, audit.AI_REVIEW_FAILED_ACTION)[0]["detail"]["stage"] == "generate"


def test_a_generate_failure_with_a_json_detail_is_recorded_verbatim(tmp_path, caplog):
    """FB1 (4): the wrapper's own `detail` - the JSON `HTTPException` body
    shape every real 502 from `wrapper.api.post_review` actually carries -
    rides straight into the durable reason and the driver's own warning
    line, not just the bare status code."""

    def raw(_body: dict) -> Response:
        return JSONResponse(status_code=502, content={"detail": "boom detail"})

    with caplog.at_level("WARNING"):
        run = released_without_a_reflection(tmp_path, raw_review=raw)

    failures = run.review["ai_review_failures"]
    assert len(failures) == 1, run.review["entries"]
    assert failures[0]["reason"] == "the wrapper answered 502: boom detail"
    assert any("boom detail" in r.getMessage() for r in caplog.records)


def test_a_generate_failure_with_a_non_json_body_records_its_text(tmp_path):
    """A body that never parses as JSON at all - an upstream error page, a
    bare string - must still reach the operator: `response.text` stands in
    for the missing `detail`, whitespace-collapsed."""

    def raw(_body: dict) -> Response:
        return Response(
            content="  Bad Gateway\nfrom  nginx  ", status_code=502, media_type="text/plain"
        )

    run = released_without_a_reflection(tmp_path, raw_review=raw)

    failures = run.review["ai_review_failures"]
    assert len(failures) == 1, run.review["entries"]
    assert failures[0]["reason"] == "the wrapper answered 502: Bad Gateway from nginx"


def test_a_generate_failure_with_an_empty_body_keeps_the_bare_status(tmp_path):
    """No body at all leaves the bare `the wrapper answered <status>` -
    exactly as before FB1 - rather than a trailing colon with nothing after
    it."""

    def raw(_body: dict) -> Response:
        return Response(content=b"", status_code=502)

    run = released_without_a_reflection(tmp_path, raw_review=raw)

    failures = run.review["ai_review_failures"]
    assert len(failures) == 1, run.review["entries"]
    assert failures[0]["reason"] == "the wrapper answered 502"


@requires_node
def test_the_page_reads_no_review_from_a_game_whose_review_failed(tmp_path):
    """The other half of "distinguishable": the same body the failure row
    lands in must make `aiReviewsOf` return nothing, so the page draws no
    `section.ai-review` at all rather than an empty or half-filled one."""

    def failing(settings, prompt, model, effort, **_kwargs):
        raise ReviewFailed("claude exited 1")

    run = released_without_a_reflection(tmp_path, review_runner=failing)

    assert run_module(UI_MODULE, "m.aiReviewsOf(rev)", rev=run.review) == []
    assert run.review["ai_review_failures"][0]["stage"] == "generate"


@pytest.mark.parametrize(
    ("runner", "expected_stage", "expected_invoked"),
    [
        (lambda settings, prompt, model, effort, **_kwargs: "", "empty", True),
        (lambda settings, prompt, model, effort, **_kwargs: "   ", "empty", True),
    ],
)
def test_a_review_that_came_back_empty_says_so_on_the_same_surface(
    tmp_path, runner, expected_stage, expected_invoked
):
    """The reviewer ran and produced nothing usable - a different fact again
    from "it could not be reached", and one the same body has to carry, or
    an operator is back to guessing which of the two happened."""
    run = released_without_a_reflection(tmp_path, review_runner=runner)

    assert _rows(run.review, audit.AI_REVIEW_ACTION) == []
    failure = run.review["ai_review_failures"][0]
    assert failure["stage"] == expected_stage
    assert failure["invoked"] is expected_invoked


# -- DR3 (3): a fixed "Play statistics" block is appended to the filed text -


def _write_reviewer_admit_journal(tmp_path: Path, game_id: str, *, added=(), updated=()) -> None:
    """One `plays admit` line, written the way the real `mcp-seat` process
    would (`playscripts.gitwork._JOURNALED_KEYS`, read back by
    `handover.read_journal(guides_root, game_id, "reviewer")`), directly
    under the `/guides` checkout `_driver` points this test's driver at -
    `handover.journal_path` falls back to `<guides_root>/.git/<name>` when
    `.git` is not a linked-worktree file, which a bare `mkdir` here
    satisfies without a real git checkout."""
    git_dir = tmp_path / "guides" / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    line = {
        "game_id": game_id,
        "role": "reviewer",
        "command": "plays admit",
        "result": "admitted",
        "dsl_change": True,
        "added": list(added),
        "updated": list(updated),
        "reprioritized": [],
    }
    with (git_dir / "mcm-results.jsonl").open("a") as handle:
        handle.write(json.dumps(line) + "\n")


def test_the_filed_review_carries_a_play_statistics_block_with_zeros_when_nothing_admitted(
    tmp_path,
):
    """DR3 (3): the block is appended even when the game's journal has no
    admits at all - zeros for every count, never a missing block."""
    run = released_without_a_reflection(tmp_path)
    text = _rows(run.review, audit.AI_REVIEW_ACTION)[0]["detail"]["text"]
    assert REVIEW_TEXT in text
    assert "Play statistics:" in text
    for line in (
        "- plays in deck: 0",
        "- added: 0",
        "- updated: 0",
        "- used: 0",
        "- denied: 0",
        "- passed over: 0",
    ):
        assert line in text, text


def test_the_filed_review_carries_the_reviews_own_admits_from_the_journal(tmp_path):
    """The journal is read AFTER the review one-shot returns
    (`role="reviewer"`, this game's own `plays admit` line), and its
    `added`/`updated` play ids are folded into the appended block - proving
    the statistics reflect what THIS review admitted, not merely whatever
    `GET /review` already knew when `post_ai_review` started."""
    wrapper, pushed, reviewed = _wrapper_app(
        lambda settings, prompt, model, effort, **_kwargs: REVIEW_TEXT
    )
    with TestClient(api_app) as api_client:
        info = _agent_game(api_client)
        _write_reviewer_admit_journal(tmp_path, info["game_id"], added=["p1", "p2"], updated=["p3"])
        driver = _driver(tmp_path, wrapper)
        try:
            _dispatch(driver, info)
            _concede(api_client, info["game_id"])
            driver.post("/release", json={"uid": info["seat_uids"]["bob"], "reason": "lobby reset"})
            driver.post(
                "/turn-done",
                json={"turnId": "turn-1", "state": "complete", "finalText": "I attack"},
            )
            _complete_handover(driver, pushed)
            wait_for(lambda: driver.get("/status").json()["gameId"] is None)
            body = _review_body(api_client, info)
        finally:
            api_client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})

    text = _rows(body, audit.AI_REVIEW_ACTION)[0]["detail"]["text"]
    assert "Play statistics:" in text
    assert "- added: 2" in text
    assert "- updated: 1" in text
