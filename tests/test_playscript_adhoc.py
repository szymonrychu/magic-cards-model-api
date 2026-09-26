"""FD10 (a): mid-game ad-hoc scripts.

A seat facing a multi-step or repetitive sequence (a mana loop, most often -
the owner's own evidence: finding seq 191, about 25 manual MCP calls
hand-driving a Devoted Druid + Vizier of Remedies loop) writes a play-script
for it right there, rather than waiting for the post-game admit.
`records.stage_adhoc_script` is the server-side mechanics (validation exactly
`PUT .../scripts` applies, plus a conflict warning against the seat's own
corpus); `script_tools.stage`/`run(body=...)` are the client-side callers -
FD5's `POST .../scripts/adhoc` route does not exist yet (FD5 part 7, not
this task), so the `script_tools`-level tests here go through a fake seat,
never a real HTTP round trip to that route.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import add_card
from fastapi.testclient import TestClient

from magic_cards_model_api import audit, records, review
from magic_cards_model_api.playscripts import report, runtime, script_tools
from magic_cards_model_api.store import store

REASONING = "loop the mana rocks to reach my X target"


def flash_permanent_play(play_id: str, **over: Any) -> dict[str, Any]:
    """A minimal, valid play - casting a permanent at instant speed, the
    same worked example `playscript_testkit.flash_permanent_play` builds,
    reimplemented here so this file has no import-order dependency on which
    `card`/`FLASH_PERMANENT` fixture happens to be cached."""
    base = {
        "schema_version": 1,
        "id": play_id,
        "name": "Cast a permanent at instant speed",
        "intent": "Hold the permanent and commit it in the latest legal window.",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "theirs", "steps": ["end_step"], "stack": "empty"},
        "true": [],
        "false": [],
        "moves": [
            {
                "kind": "cast",
                "card": {
                    "zone": "hand",
                    "select": {"keywords_any": ["Flash"], "types_any": ["Creature"]},
                },
                "mana": "auto",
            }
        ],
        "next": [],
    }
    base.update(over)
    return base


def seat_uid(game_id: str, player: str) -> str:
    return next(uid for uid, name in store.seat_uids(game_id).items() if name == player)


def register(client: TestClient, game_id: str, uid: str, *scripts: dict[str, Any]) -> None:
    response = client.put(
        f"/api/games/{game_id}/seats/{uid}/scripts",
        params={"uid": uid},
        json={
            "deck_slug": "boros-aggro",
            "guides_revision": "rev0",
            "scripts": list(scripts),
            "inputs": [],
        },
    )
    assert response.status_code == 200, response.text


def staged_entries(game_id: str) -> list[dict[str, Any]]:
    return [
        entry for entry in audit.entries(game_id) if entry["action"] == records.SCRIPT_STAGED_ACTION
    ]


# -- records.stage_adhoc_script: the server-side mechanics ------------------


def test_stage_adhoc_script_stores_a_valid_play_and_returns_an_id(client, game):
    uid = seat_uid(game, "alice")
    add_card(client, game, "Mountain", "alice", "hand")
    result = records.stage_adhoc_script(
        game, uid, flash_permanent_play("whatever-i-called-it"), REASONING
    )
    assert result["id"].startswith(records.ADHOC_SCRIPT_ID_PREFIX)
    assert result["id"] == f"adhoc-1-{uid}"
    assert result["warnings"] == []

    corpus = store.scripts(game, uid)
    docs = corpus["scripts"]
    assert len(docs) == 1
    assert docs[0]["id"] == result["id"]
    # The stored doc is pure schema.Play - `runtime._normalised` re-validates
    # it the moment this seat runs or lists it, and `schema.Play` forbids
    # unknown keys, so the caller's own `id` was overwritten and nothing else
    # was added onto the doc itself.
    from magic_cards_model_api.playscripts.schema import Play

    Play.model_validate(docs[0])


def test_a_lint_error_refuses_it_with_its_codes(client, game):
    uid = seat_uid(game, "alice")
    bad = flash_permanent_play("tmp")
    bad["moves"] = [{"kind": "not-a-real-kind"}]
    with pytest.raises(records.InvalidScripts) as excinfo:
        records.stage_adhoc_script(game, uid, bad, REASONING)
    codes = {error["code"] for error in excinfo.value.errors}
    assert "forbidden-kind" in codes
    for error in excinfo.value.errors:
        assert {"code", "path", "message"} <= set(error)
    # Nothing was stored on a refusal.
    assert (store.scripts(game, uid) or {}).get("scripts") in (None, [])


def test_the_card_name_ban_applies(client, game):
    uid = seat_uid(game, "alice")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bad = flash_permanent_play("tmp")
    bad["moves"][0]["card"]["select"] = {
        "keywords_any": ["Flash"],
        "types_any": ["Creature"],
        "ability_text_has": "Lightning Bolt",
    }
    with pytest.raises(records.InvalidScripts) as excinfo:
        records.stage_adhoc_script(game, uid, bad, REASONING)
    codes = {error["code"] for error in excinfo.value.errors}
    assert "card-name-in-field" in codes


def test_the_id_prefix_and_the_cap(client, game):
    uid = seat_uid(game, "alice")
    for n in range(1, records.MAX_ADHOC_SCRIPTS + 1):
        result = records.stage_adhoc_script(game, uid, flash_permanent_play(f"try-{n}"), REASONING)
        assert result["id"] == f"adhoc-{n}-{uid}"
    with pytest.raises(records.InvalidScripts) as excinfo:
        records.stage_adhoc_script(game, uid, flash_permanent_play("one-too-many"), REASONING)
    codes = {error["code"] for error in excinfo.value.errors}
    assert "too-many-adhoc-scripts" in codes


def test_a_conflict_with_the_registered_corpus_comes_back_as_a_warning_not_a_refusal(client, game):
    uid = seat_uid(game, "alice")
    register(client, game, uid, flash_permanent_play("existing-1"))
    # The very same shape, staged ad-hoc: the two overlap on every dimension
    # `lint._conflict_warnings` reads and neither excludes the other, so
    # `plays check`'s own conflict ratchet would flag them - but staging
    # never refuses on it, since the seat is choosing whether to run this
    # script at all (the no-autofire rule).
    result = records.stage_adhoc_script(
        game, uid, flash_permanent_play("does-not-matter"), REASONING
    )
    assert result["warnings"], (
        "a foreseeable conflict with the registered corpus should be reported"
    )
    assert result["warnings"][0]["code"] == "play-conflict"
    assert result["id"] in result["warnings"][0]["plays"]
    assert "existing-1" in result["warnings"][0]["plays"]


def test_the_script_staged_row_carries_the_body_reasoning_and_warnings(client, game):
    uid = seat_uid(game, "alice")
    register(client, game, uid, flash_permanent_play("existing-1"))
    body = flash_permanent_play("does-not-matter")
    result = records.stage_adhoc_script(game, uid, body, REASONING)

    rows = staged_entries(game)
    assert len(rows) == 1
    detail = rows[0]["detail"]
    assert detail["seat"] == "alice"
    assert detail["player"] == "alice"
    assert detail["id"] == result["id"]
    assert detail["play"] == body
    assert rows[0]["reasoning"] == REASONING
    assert detail["warnings"] == result["warnings"]
    assert detail["warnings"], "this test's own corpus setup should have produced a conflict"


def test_a_refused_stage_writes_no_script_staged_row(client, game):
    uid = seat_uid(game, "alice")
    bad = flash_permanent_play("tmp")
    bad["moves"] = [{"kind": "not-a-real-kind"}]
    with pytest.raises(records.InvalidScripts):
        records.stage_adhoc_script(game, uid, bad, REASONING)
    assert staged_entries(game) == []


# -- review.adhoc_scripts_by_seat / playscripts.report -----------------------


def test_the_review_record_lists_the_ad_hoc_script(client, game):
    uid = seat_uid(game, "alice")
    result = records.stage_adhoc_script(game, uid, flash_permanent_play("whatever"), REASONING)
    audit.record(
        game,
        audit.SCRIPT_RUN_ACTION,
        # `seat`/`player` on a script-run row are always the seat's own
        # username (`script_tools.run`'s own `"seat": seat.player`), the
        # same convention `records.stage_adhoc_script`'s own `script_staged`
        # row now follows too - never the uid.
        player="alice",
        seat="alice",
        game_id=game,
        run_id="run-x",
        script_id=result["id"],
        exit="chain_complete",
        moves_done=1,
    )
    entries = audit.entries(game)
    body = review.build_review(store.archived(game), entries)
    listed = body["adhoc_scripts"]["alice"]
    assert len(listed) == 1
    assert listed[0]["id"] == result["id"]
    assert listed[0]["reasoning"] == REASONING
    assert listed[0]["play"]["id"] == "whatever"
    assert len(listed[0]["runs"]) == 1
    assert listed[0]["runs"][0]["exit"] == "chain_complete"


def test_report_makes_a_ran_adhoc_script_a_candidate_closed_by_no_change(client, game):
    uid = seat_uid(game, "alice")
    result = records.stage_adhoc_script(game, uid, flash_permanent_play("whatever"), REASONING)
    audit.record(
        game,
        audit.SCRIPT_RUN_ACTION,
        player="alice",
        seat="alice",
        game_id=game,
        run_id="run-x",
        script_id=result["id"],
        exit="chain_complete",
        moves_done=1,
    )
    entries = audit.entries(game)
    verdict = report.assess(entries, "alice")
    assert len(verdict.candidates) == 1
    assert not verdict.satisfied

    audit.record(
        game,
        audit.PLAYS_OUTCOME_ACTION,
        player="alice",
        admitted=[],
        no_change=[
            {
                "candidate": report.decision_candidate_id(verdict.candidates[0].seqs[0]),
                "reason": "fine",
            }
        ],
    )
    assert report.assess(audit.entries(game), "alice").satisfied


def test_report_makes_a_ran_adhoc_script_a_candidate_closed_by_an_admit(client, game):
    """FD10 (a): the spec's own words are "closed by an admit or a
    no-change" - `test_report_makes_a_ran_adhoc_script_a_candidate_closed_by_
    no_change`, right above, only ever exercises the no-change half. An
    ad-hoc script's candidate closes exactly like an ordinary decision
    candidate does: a `plays_outcome` row admitting a real `dsl_change`
    closes every open candidate for the seat (`report.Assessment`'s own
    docstring), the ad-hoc one included - no `no_change` entry naming its
    id is required at all."""
    uid = seat_uid(game, "alice")
    result = records.stage_adhoc_script(game, uid, flash_permanent_play("whatever"), REASONING)
    audit.record(
        game,
        audit.SCRIPT_RUN_ACTION,
        player="alice",
        seat="alice",
        game_id=game,
        run_id="run-x",
        script_id=result["id"],
        exit="chain_complete",
        moves_done=1,
    )
    entries = audit.entries(game)
    verdict = report.assess(entries, "alice")
    adhoc_candidate = next(c for c in verdict.candidates if c.action == "adhoc_script")
    assert adhoc_candidate.id in {c.id for c in verdict.open}
    assert not verdict.satisfied

    audit.record(
        game,
        audit.PLAYS_OUTCOME_ACTION,
        player="alice",
        admitted=[{"dsl_change": True, "added": ["new-play"], "updated": [], "reprioritized": []}],
        no_change=[],
    )
    verdict2 = report.assess(audit.entries(game), "alice")
    assert adhoc_candidate.id not in {c.id for c in verdict2.open}
    assert verdict2.satisfied


# -- script_tools.stage / run(body=...): the client-side glue, fake seat ----


class _FakeSeat:
    """The minimal shape `script_tools.stage`/`run` need off a `Seat` -
    `uid`, `game_id()`, `call_json`, `advise`, `player` and `_request_json`
    (`runtime._corpus`'s own live-GET fallback when nothing is cached)."""

    def __init__(self, uid: str = "p1-uid", game_id: str = "g1", player: str = "alice") -> None:
        self.uid = uid
        self._game_id = game_id
        self.player = player
        self.advise = False
        self.calls: list[tuple[str, str, Any]] = []
        self._answer: Any = None

    def game_id(self) -> str:
        return self._game_id

    def call_json(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path, body))
        if self._answer is None:
            raise AssertionError(f"unexpected call_json({method!r}, {path!r})")
        return self._answer

    def _request_json(self, method: str, path: str, body: Any) -> tuple[int, dict[str, Any]]:
        return 404, {"reason": "no corpus registered"}

    def fetch_state(self, **kwargs: Any) -> dict[str, Any]:
        return {"pending_decision": None}


def test_stage_returns_id_and_warnings_and_clears_the_cached_corpus():
    seat = _FakeSeat()
    seat._answer = {"id": "adhoc-1-p1-uid", "warnings": [{"code": "play-conflict"}]}
    corpora: dict[tuple[str, str], dict[str, Any]] = {("g1", "p1-uid"): {"scripts": []}}

    body = flash_permanent_play("tmp")
    result = script_tools.stage(seat, corpora, body=body, reasoning=REASONING)

    assert result == {"id": "adhoc-1-p1-uid", "warnings": [{"code": "play-conflict"}]}
    assert ("g1", "p1-uid") not in corpora
    method, path, sent = seat.calls[0]
    assert method == "POST"
    assert path == "/api/games/g1/seats/p1-uid/scripts/adhoc"
    assert sent == {"play": body, "reasoning": REASONING}


def test_stage_surfaces_schema_lint_errors_from_a_refusal():
    seat = _FakeSeat()
    seat._answer = {
        "allowed": False,
        "reason": "refused",
        "code": "invalid_scripts",
        "errors": [{"code": "card-name-in-field", "path": "play", "message": "no"}],
    }
    result = script_tools.stage(seat, {}, body=flash_permanent_play("tmp"), reasoning=REASONING)
    assert result == {"errors": [{"code": "card-name-in-field", "path": "play", "message": "no"}]}


def test_stage_surfaces_a_bare_refusal_string_when_the_server_names_no_errors():
    seat = _FakeSeat()
    seat._answer = {"allowed": False, "reason": "scripts are disabled on this deployment"}
    result = script_tools.stage(seat, {}, body=flash_permanent_play("tmp"), reasoning=REASONING)
    assert isinstance(result, str)
    assert "disabled" in result


def test_run_refuses_when_both_script_and_body_are_given():
    seat = _FakeSeat()
    answer = script_tools.run(
        seat, {}, script="some-id", body=flash_permanent_play("tmp"), reasoning=REASONING
    )
    assert isinstance(answer, str)
    assert "exactly one" in answer


def test_run_refuses_when_neither_script_nor_body_are_given():
    seat = _FakeSeat()
    answer = script_tools.run(seat, {}, reasoning=REASONING)
    assert isinstance(answer, str)
    assert "exactly one" in answer


def test_an_inline_run_stages_then_runs_the_assigned_id_against_a_fake_seat(monkeypatch):
    """FD10 (a): `run(body=...)` is stage-then-run in one call - the assigned
    id `stage` returns is exactly the `script_id` the runner is asked to run,
    never a second round trip the caller has to sequence itself. `runtime.
    run_script` is faked here (its own correctness, over a real engine, is
    `test_playscript_runtime.py`'s job, and `runtime.py` is not this task's
    file to touch) - what is new, and what this pins, is `run`'s own
    stage-then-run glue."""
    seat = _FakeSeat()
    corpora: dict[tuple[str, str], dict[str, Any]] = {}
    staged_with: list[dict[str, Any]] = []

    def fake_stage(seat_, corpora_, *, body, reasoning):
        assert seat_ is seat
        assert reasoning == REASONING
        staged_with.append(body)
        return {"id": "adhoc-1-p1-uid", "warnings": []}

    monkeypatch.setattr(script_tools, "stage", fake_stage)

    run_calls: list[str] = []

    def fake_run_script(seat_, *, script_id, **kwargs):
        run_calls.append(script_id)
        return runtime.RunResult(
            finished=True,
            script_exit=runtime.ScriptExit(
                exit=runtime.Exit.CHAIN_COMPLETE, script_id=script_id, moves_done=3, moves_total=3
            ),
            state={"pending_decision": None},
        )

    monkeypatch.setattr(runtime, "run_script", fake_run_script)

    body = flash_permanent_play("tmp")
    answer = script_tools.run(seat, corpora, body=body, reasoning=REASONING)

    assert staged_with == [body]
    assert run_calls == ["adhoc-1-p1-uid"]
    assert answer["script_exit"]["exit"] == "chain_complete"
    assert answer["script_exit"]["script"] == "adhoc-1-p1-uid"
    assert answer["script_exit"]["moves_done"] == 3
